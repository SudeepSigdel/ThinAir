# Thinair: working notes for Claude

Open-source, self-hostable platform: run coding-agent CLIs in Docker sandboxes, control from a phone,
and profile the minimum CPU/RAM a project needs to pass its tests.

## Hard constraints
- Agent-agnostic: Thinair never calls an LLM API. It drives agent CLIs as black boxes.
- Never proxy/store subscription credentials. Each agent logs in inside its own Docker volume
  (`thinair-auth-<agent>`, per user `thinair-auth-<agent>.<user>`) that Thinair never reads.
- Self-hostable on a 1-4 GB ARM VM: `docker compose up`.
- Secure by default: sandboxes non-root, cap-drop ALL, resource-limited, no Docker socket. API needs auth.

## Decisions (approved)
- Python 3.12, FastAPI, SQLModel (+Alembic), SQLite. Docker access via docker-socket-proxy (never raw socket).
- SSE (not WebSockets) for logs. UI = static HTML + one plain JS file calling the JSON API, PWA. No Node build step.
  (Changed in step 10 from Jinja + HTMX: zero new deps, nothing vendored, and a strict CSP with no inline code.)
- Agent commands are argv lists (no shell); `{task}` is one argv element.
- License MIT. GitHub only; private repos via an optional token (phase 2, step 13).
- **Idle cost must be near zero**: one uvicorn process, workers block on an asyncio queue (no polling),
  janitor runs at startup + rarely, sandboxes exist only during jobs, mem_limit on every service.
- **Mobile access**: needs HTTPS for PWA install: Caddy compose profile `https`, or a tunnel (Tailscale/Cloudflare).

## Git
- `.gitattributes` forces LF everywhere (Dockerfiles/scripts run in Linux containers).
- **Never add a `Co-Authored-By` trailer (or any Claude/AI attribution line) to commits or PR descriptions.**
  This is the owner's explicit instruction and overrides any default attribution behavior.
- Only commit when asked. Conventional-commit style messages.

## Commands
- Setup: `uv sync --extra dev`
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Test: `uv run pytest -m "not docker"` (Docker tests: `uv run pytest -m docker`)
- Run: `uv run thinair serve` or `docker compose up -d`

## Conventions
- Add a dependency only with a comment in pyproject saying why.
- Every roadmap step ends with tests + ruff passing and a short "verify manually" note.
- Verify external CLI flags against official docs; record findings in docs/agents.md.

## Database
- Default SQLite (WAL). `THINAIR_DATABASE_URL` (SecretStr) switches to Postgres; driver is the optional `postgres`
  extra (`psycopg[binary]`), in Docker via build arg `EXTRAS` <- `THINAIR_EXTRAS`. `make_engine` uses pool +
  pre_ping for non-SQLite. Enums are stored by *name*; on Postgres they are real enum types, so a new enum member
  needs `ALTER TYPE ... ADD VALUE IF NOT EXISTS` in its migration (see 0002). `test_postgres_docker.py` runs
  migrations up/down/up, the model/migration diff and the job API against postgres:17-alpine (CI installs the extra).
- Models in `src/thinair/db/models.py`; migrations in `src/thinair/db/migrations/versions/`, applied at app startup.
- After changing a model, generate a migration (a test fails if you forget). From the repo root:
  `uv run python -c "from alembic import command; from thinair.db.session import make_engine; from thinair.db.migrate import _config; e=make_engine('sqlite:///tmp_gen.db'); command.revision(_config(e), message='msg', autogenerate=True)"`
  then delete `tmp_gen.db`, run `ruff format`, and review the file.

## Auth
- Tokens: `thinair_` + 256-bit random; only SHA-256 stored (`src/thinair/auth.py`). First start prints a generated
  admin token once, or use `THINAIR_ADMIN_TOKEN`. Manage with `thinair token create|list|revoke`
  (in Docker: `docker compose exec thinair thinair token create --name phone`).
- API accepts `Authorization: Bearer` or the HttpOnly/SameSite=Strict `thinair_session` cookie (needed for SSE).
  Protect routes with `Depends(require_auth)`. `/healthz` is the only open route. Wrong tokens are throttled
  (10/min per IP, IPv6 per /64, table capped at 10k keys); requests with *no* token are a plain 401 and never count
  (else reloading the logged-out UI locks you out). While throttled, tokens >= 32 chars (all generated ones) are
  still checked and accepted: behind Caddy everyone shares one IP, so strangers could otherwise lock the owner out.
  Shorter tokens get 429 unchecked. Test gotcha: TestClient's host is "testclient" (not an IP), so IP parsing
  bugs only show up against a live server (that is how an IPv4 crash was caught).
- `session_scope()` uses `expire_on_commit=False` so rows stay readable after the session closes.

## Users (`src/thinair/users.py`, phase 3 step 19)
- Owner = tokens/jobs with `user_id NULL` (no data migration; `THINAIR_ADMIN_TOKEN` and the bootstrap token are the
  owner's; bootstrap ignores members' tokens). `User` rows: name `^[a-z][a-z0-9_-]{0,31}$` (no dots), `admin`
  reserved, `is_admin`, `disabled_at` (disable also revokes tokens; `principal_for` refuses disabled users anyway).
- `require_auth` returns a `Principal` (token + user). Routes: `_job_and_repo(s, id, who)` 404s jobs the caller
  can't see (members: own jobs only; admins: all); repos visible to members only via their jobs. `require_admin`
  guards `/api/users*` (403 `admin_only`). Retry / PR keep the job's owner (`Job.user_id`).
- Jobs act as their owner: login volume `auth_volume_name(agent, user)` (`thinair-auth-<a>` / `thinair-auth-<a>.<u>`),
  `env_passthrough` only for the owner (`host_env={}` otherwise), GitHub token only if `github_token_allowed(user)`
  (owner/admin, or `THINAIR_GITHUB_TOKEN_FOR_MEMBERS`) -> `prepare_workspace(github_auth=...)`; PR refused 403
  `github_not_allowed` at submit and again in `pr_executor`. CLI: `thinair user add|list|disable`,
  `token create --user`, `auth <agent> --user`.
- Job limit: members may have `job_limit(user)` unfinished jobs (`User.max_active_jobs`, None -> THINAIR_MEMBER_JOB_LIMIT=5,
  0 = none; owner/admins never). Checked in `JobService.submit` (PrepareError) and `submit_pr` (PRError), both
  `too_many_jobs` -> HTTP 429. `POST /api/users/{name}/limit`, `thinair user limit NAME N|default`.
- Pairing (`src/thinair/pairing.py`, table `PairingCode`, migration 0007): `thinair qr [--user]` prints (segno,
  terminal, compact) `<THINAIR_PUBLIC_URL>/#pair=thinair_pair_...`; one-time, 10 min, SHA-256 stored. Open
  `POST /api/pair` (throttled like login) creates a NEW token and sets the session cookie (`_set_session`, shared
  with login). app.js `pairFromLink()` runs before `route()` and `history.replaceState`s the code away. Verified:
  OpenCV decodes the terminal rendering (dark-terminal colours), Edge smoke 7/7, redeem through Caddy HTTPS.
- Testing gotcha: API tests acting as several users must send every request through the ONE lifespan TestClient
  (per-request headers, see `As` in test_users.py). A second TestClient runs on another event loop and never wakes
  the job workers, so jobs stay queued.

## Sandbox (`src/thinair/sandbox/`)
- `SandboxSpec` (spec.py) -> `SandboxManager.run()` (manager.py). Always: uid 10001, cap-drop ALL, no-new-privileges,
  read-only rootfs + /tmp tmpfs, `--cpus/--memory/--pids-limit`, memswap == memory (so OOM isn't hidden), network off
  unless the phase opts in. Mounts are **named volumes only** (host paths/docker.sock are unrepresentable).
- Every container/volume is labelled `thinair.managed=true`, `thinair.job=<id>`. `janitor.sweep()` removes leftovers
  (call at startup; volumes only after 6 h).
- Cancelling the `run()` task kills + removes the container. Output is line-split, secrets in `spec.redact` -> `***`,
  total capped at `max_output_bytes`.
- Gotcha (cost us a broken clone): Docker re-applies the ownership of an image's mount dir to an *empty* named volume
  every time a container is **created** (started or not). So EVERY image mounted at /workspace (python, node, git) must
  have `/workspace` owned by 10001, and a pre-"init" container does not help. Regression tests cover all three images.
- `SandboxManager.fit()` caps `cpus` at the host's `NCPU` (Docker refuses `--cpus` above it, which broke every job
  on 1-CPU VMs). The profiler instead *skips* tiers above the host and notes it (capping would mislabel results).
- Docker tests: `uv run pytest -m docker` (pulls python:3.12-alpine once). They run in the default `pytest` too and skip
  if the daemon is unreachable.

## Projects (`src/thinair/project/`)
- `config.py`: `thinair.yaml` schema (unknown keys are errors; env values coerced to strings). `detect.py`: stack from
  top-level filenames + overrides -> `ResolvedProject`. `prepare.py`: volume -> clone (in a sandbox) or tar upload
  -> detect -> install. GitHub HTTPS URLs only.
- Private repos (`src/thinair/github.py`): `THINAIR_GITHUB_TOKEN` or `_FILE`, read per call, format-checked
  as OPAQUE (`[\x21-\x7e]{20,4096}`: no whitespace/CR/LF = no header injection; GitHub App installation
  tokens are stateless `ghs_<id>_<JWT>` since 2026-04-27, ~520 chars with dots, so never assume a length
  or alphabet). `_stream_logs` masks secrets BEFORE cutting a line to MAX_LINE_CHARS (else a long token
  across the cut leaked its start). Only `clone_spec()` gets it, as `GIT_CONFIG_COUNT/KEY_0/VALUE_0`
  = `http.https://github.com/.extraHeader: Authorization: Basic b64(x-access-token:TOKEN)`; token and b64 are
  both in `redact`. Never in argv, .git/config, DB, install/test/agent containers. A blank env value falls back to
  the file (`.env.example` has `THINAIR_GITHUB_TOKEN=`). Docker tests use a fake token: GitHub rejects it -> `clone_failed`.
- Dependencies are installed into the workspace volume (`/workspace/.venv`, `node_modules`) so network-less test
  containers can use them. Sandbox base images are built from `sandbox/images/*.Dockerfile` on first use.
- Fixtures in `tests/fixtures/` hold ~150 MB in their tests; expected profiler minimum is the 256m tier.
- Dependency cache: `prepare_workspace(cache_scope=<user name>)` (executors pass `cache_scope(owner)`, owner =
  "admin"; runners and the CLI pass none) mounts volume `thinair-cache-<sha(scope url)>` (label `thinair.cache`,
  NOT managed, so the janitor keeps it) on /cache for the INSTALL step only, with PIP_/UV_CACHE_DIR, npm_config_cache,
  YARN_CACHE_FOLDER. Per user because install commands are repo code and can write anything there. Sandbox images
  are `:2` (they create /cache owned by 10001; same empty-volume ownership gotcha). The python image must NOT set
  PIP_NO_CACHE_DIR: pip treats any value, even "0"/"false", as "no cache". `thinair cache list|clear`.
  Measured (psf/requests, fast mirror): downloads 9 -> 0, 36 s -> 32 s; the win grows with slow links / big deps.

## Dev environment gotcha (diagnosed 2026-11-04, Windows + Docker Desktop)
- On this network IPv4 to Debian/Fastly CDNs crawls (~10 B/s from the host too) while IPv6 is fast. Containers are
  IPv4-only by default, so `apt-get` (9.7 MB package list) stalls and `pip install` from pypi.org is flaky.
  Do NOT force IPv4 in Dockerfiles (that made it worse). Sandbox images therefore never use apt; clone runs in
  a local `thinair-sandbox-git` image built FROM `alpine/git` (Docker Hub works). For pip use `THINAIR_PIP_INDEX_URL` (e.g. a mirror that
  answered over IPv4: https://mirrors.aliyun.com/pypi/simple/). Docker Desktop IPv6 would also fix it.
- Diagnosing tip: pipes buffer output when a command is killed by `timeout`; redirect to a file instead.
  Stray containers survive a killed `docker run` client: check `docker ps` afterwards.

## Profiler (`src/thinair/profiler/`)
- `search.py` is Docker-free: `ProfileSearch(run, cpus, memory, ...)` takes an async `run(cpus, mem_bytes, timeout_s) -> RunRecord`.
  Assumptions (stated in every report): more memory never hurts; less CPU never reduces needed memory.
  Highest CPU: binary search over memory tiers. Lower CPUs: test previous level's minimum first (1 run), else search above it.
  A level that fails even at max memory is `infeasible`; lower levels are `skipped`. Boundary is re-run `confirm_runs` times;
  a failure steps up one tier and marks the level flaky. `max_runs` (default 40) gives an `incomplete` partial result.
- `classify.py`: timeout > oom flag > pass > startup (126/127/None) > exit 137 (oom) > memory markers in output > test_failed.
  Gotcha: a regex/escape written through a shell heredoc can silently lose backslashes (we once got a backspace char);
  use the Write/Edit tools for code containing backslashes and read files with `encoding="utf-8"` on Windows.
- Recommended config = max(min, observed peak) x headroom rounded up to a usual size, with CPU one tier above the minimum.
- `report.py`: `to_json`, `to_markdown`. `runner.py`: `run_profile` / `profile_source` plug the real sandbox into the search;
  CLI: `thinair profile <dir|github-url> [--md f] [--json f]`.
- Metrics (`metrics.py`): test command is wrapped so the container prints its own cgroup `memory.peak` and `cpu.stat`
  on a `[thinair-metrics]` marker line (stripped from output). Docker's ~1 Hz stats sampler is only a fallback: it
  reported 4 MiB / 0.0 s for sub-second runs. Peak includes file cache. Needs cgroup v2.
- CLI output must be UTF-8 safe (`_utf8_output`): reports contain emoji and Windows consoles default to cp1252.

## Jobs (`src/thinair/jobs/`)
- `service.JobService`: in-memory `asyncio.Queue` + N workers (`max_concurrent_jobs`), blocked on `queue.get()` (no polling).
  Jobs are DB rows; on start, RUNNING -> failed `interrupted`, QUEUED -> re-queued; leftover sandboxes are swept
  (`THINAIR_SWEEP_ON_START`, off in tests). `submit()` validates the URL (GitHub only) and raises `PrepareError`.
  `cancel()` works for queued and running jobs; whole-job limit `THINAIR_JOB_TIMEOUT_S`.
  Errors become `error_code` + a user-safe `error_message`; unexpected exceptions are logged server-side only.
- Executors are plugged in (`dict[JobKind, Executor]`) so the machinery is tested with fakes; `executors.py` holds the
  real profile and agent executors (persist ProfileRun/ProfileReport and JobResult).
- `events.EventStore`: LOG events emitted inside the event loop are buffered and written in one transaction every
  0.2 s / 200 lines (was one commit + fsync per line, blocking the loop: 1.1 ms -> 0.07 ms per line); any other event
  type flushes the buffer first, so order holds and statuses are stored before `emit` returns. `flush()` on stop.
  Streams wait per job (`_waiters[job_id]`), so a busy job no longer wakes every open stream. Index
  `(job_id, seq)` (migration 0005) serves every read; the old job_id-only index sorted up to 50k rows per fetch.
  SQLite runs `synchronous=NORMAL` (safe with WAL).
- Retention (`src/thinair/retention.py`): `THINAIR_LOG_RETENTION_DAYS` (30, 0 = off). Startup + daily task deletes only
  LOG events of finished jobs older than that and sets `Job.logs_removed_at` (migration 0006). Never delete STATUS
  events: the web UI loads a finished job's report/result only when the stream replays its final status event.
- `events.EventStore`: per-job `seq` (= SSE `id:`), stored in SQLite, `stream(job_id, after)` replays then follows live,
  keep-alive every 15 s, ends when the job is finished. Order matters: emit the final STATUS event *before* writing the
  terminal job status; streams read "finished" *before* fetching. Log events are capped per job; lifecycle events never are.
- Gotcha (cost us a hang): never `await task` on a child task that swallows cancellation; the parent's cancel is forwarded
  into the child and the parent never sees `CancelledError`. Use `asyncio.wait([task])` and handle cancel explicitly.

## API (`src/thinair/api/`)
- All `/api/*` routes sit behind `Depends(require_auth)` (router-level); only `/healthz` and `/api/login|logout` are open.
  Errors: `{"detail": {"code", "message"}}` with phone-readable messages.
- Routes: `/repos` (list/add/get), `/jobs` (create/list/get, `POST /jobs/{id}/cancel|retry`), `GET /jobs/{id}/events`
  (SSE; honours `Last-Event-ID` or `?after=`), `GET /jobs/{id}/profile` (JSON) and `/profile.md`, `GET /jobs/{id}/result`
  (agent summary + diff), `/agents`. `require_auth` declares `HTTPBearer(auto_error=False)` so /docs has Authorize.
- `create_app(executors=...)` injects fake executors so API tests need no Docker (`tests/test_api.py`).
- `thinair init` writes a `.env` with a fresh admin token. `thinair profile` runs a profile locally without the server.
- Testing lesson: loose assertions hid a bug (the clone test only checked the generic error code). Assert the *reason*,
  and smoke-test the real server (`thinair serve` + curl) before calling an API step done.

## Web UI (`src/thinair/web/static/`, served by `api/web.py`)
- `index.html` + `app.js` + `style.css`, hash routes `#/` and `#/job/<id>`. Served without auth (no data in it);
  data comes from `/api` with the session cookie. `/sw.js` and `/manifest.webmanifest` are served at the root.
- Security: CSP `script-src 'self'` with no inline scripts/styles (docs pages exempt), nosniff, DENY framing.
  **Never build HTML from strings in app.js** (a test bans innerHTML/outerHTML/insertAdjacentHTML/eval):
  use the `el()` helper / `textContent`. Log lines with HTML in them must render as text.
- Live view: `EventSource` on `/api/jobs/{id}/events`; close it on the final status event (else the browser
  reconnects forever). Ask for `/profile` only when the job succeeded or ended `baseline_failed`.
- The home list refreshes every 4 s only while a job is unfinished and the page is visible (idle = no requests).
- Icons are PNGs drawn by a stdlib-only script (192/512, maskable) plus `icon.svg`.
- Browser smoke test (not in CI, needs Node + Edge/Chrome): run the app with fake executors and drive it with
  `playwright-core` (`channel: "msedge"`), phone viewport. Playwright's `waitForFunction` is blocked by our CSP;
  use locators. 38 checks pass, incl. XSS-as-text, cancel/retry, agent tab + diff, PR, projects + defaults,
  reload, logout, dark mode, SW.
- Projects (routes `#/`, `#/repo/<id>`, `#/job/<id>`): home = projects (`GET /api/repos`: most recently used first,
  `jobs`/`active_jobs`/`last_job` counted over jobs the caller can see) + add-project form + recent jobs. The project
  page holds the job form (agent tab first; no URL field), its jobs (`/api/jobs?repo_id=`) and, for admins, defaults
  (`PATCH /api/repos/{id}`: `default_agent_id`, `default_ref`, migration 0008; `create_job` fills left-out
  agent/ref from them, also when the job names the repo by URL). Views that load data show `#loading` until filled
  (else the previous project's name flashes, and tests race).

## Agents (`src/thinair/agents/`, docs: `docs/agents.md`)
- `config.py`: one YAML per agent (`builtin/claude.yaml` + `$THINAIR_DATA_DIR/agents/*.yaml`, user wins by id).
  `command` is argv with `"{task}"` exactly once as its own item; tasks starting with `-` get a leading space.
- `cli_adapter.py`: `CLIAdapter.prepare()` builds `thinair-agent-<id>-<stack>:<hash>` FROM the stack image
  (Node copied from `node:22-slim` into Python images), ensures the login volume `thinair-auth-<id>` (NOT labelled
  managed, so the janitor never deletes it), records a git baseline commit after install, runs the login check.
  `run(task)` streams `AgentEvent`s; `result()` gives success/summary/diff/cost. Diff = index vs baseline,
  excluding `.venv` and `node_modules`.
- The agent image creates the login dir owned by 10001 (same Docker ownership gotcha as /workspace).
- Tokens in `auth.env_passthrough` skip the login check, are masked in output, and are never put in `thinair auth`.
- Claude Code 2.1.311 pinned; flags verified from the docs (see docs/agents.md). Headless needs
  `--permission-mode bypassPermissions` (refused as root; fine as uid 10001).
- Tests: `test_agents_unit.py` (config + stream parser), `test_agents_docker.py` (fake shell agent, real containers),
  `test_claude_image_docker.py` (real CLI, uses id `claude-test` so it never touches a real login volume).
- `thinair auth <id>` (interactive `docker run -it`, or `--print-command` for Compose hosts), `--check`; `thinair agents`.
- Never log into a real account from tests or automation: logging in is the owner's job.
- Codex 0.166.0 + Gemini CLI 0.67.0 (phase 2 step 16): flags verified from each pinned CLI's `--help` run in a
  container, the docs (Gemini's are bundled in its npm package), and the event schemas at the matching source tags
  (see docs/agents.md). `StreamParser(fmt).feed()/finish()` handles `stream-json` (Claude, stateless `parse_line`),
  `codex-json` and `gemini-stream-json` (joins `delta` chunks; summary = last assistant text).
- Gotcha: Codex's native binary needs a system CA bundle; node:22-slim has none (`UnknownIssuer`). Agent option
  `needs_ca_certs` copies `/etc/ssl/certs/ca-certificates.crt` from python:3.12-slim (no apt) + sets SSL_CERT_FILE.
- Gemini: no login-status command (check_command null); logged-out runs exit 41 "Please set an Auth method" ->
  auth_error_patterns. Needs `--skip-trust` headless. Google replaced Gemini CLI by Antigravity CLI for unpaid/Google
  One users (2026-06-18); API key or paid plan still works.
- `test_more_agents_docker.py`: real CLIs, `-test` ids, no account, codex exec run with network off.

## ACP (`src/thinair/agents/acp_driver.js`, phase 3 step 20)
- `protocol: acp` agents: `argv(task)` = `node -e <ACP_DRIVER> -- <task> <command...>` (node stops option parsing
  at `--`). The driver runs INSIDE the sandbox (no stdin plumbing through Docker needed), speaks ACP v1 to the
  agent over stdio, prints `{"acp": kind, ...}` lines; `StreamParser("acp")` joins text per messageId. Exit codes:
  0 end_turn, 1 other stopReason, 2 protocol/crash, 3 auth. No fs/terminal capabilities; permissions auto-allowed
  (allow_once first), none -> cancelled; other agent requests -> -32601. Keep the driver stdlib-only.
- Config rules (model validator): acp needs `needs_node`, no `{task}` anywhere, output forced to `acp`; `output:
  acp` without `protocol: acp` is an error. `auth.shares_login_with` -> `cfg.login_id` -> volume
  `thinair-auth-<login_id>` (per user: `auth_volume_name(cfg.login_id, user)`).
- DANGER in tests: a `-test` copy of an ACP config must also clear `shares_login_with`, or its volume is the REAL
  `thinair-auth-claude`/`-gemini`, which the fixtures delete (`copy_cfg` asserts this).
- Built-ins `claude-acp` (@agentclientprotocol/claude-agent-acp 0.95.0 + claude-code for login/check) and
  `gemini-acp` (`gemini --acp`). Tests: `test_acp.py` (host Node + `fixtures/fake_acp_agent.py`; skips without
  Node), `test_acp_docker.py` (fake agent through CLIAdapter), real CLIs in `test_more_agents_docker.py`.

## ACP registry agents (`src/thinair/agents/registry.py`)
- `thinair agent search|add|remove`: reads https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json
  (`{"agents": [...]}`; schema: id, name, version, distribution {npx|binary|uvx}; NO auth info). `config_for(entry)`:
  npx -> `install: npm install -g pkg@ver` + command = the package's npm `bin` (registry.npmjs.org/<pkg>/<ver>; one
  entry, or the unscoped name, or the shortest name when all point to one file); binary -> `download:` for
  linux-x86_64/aarch64 (musl and macOS ignored), command `/opt/thinair-agent/<cmd>`, or `thinair-agent-cmd` (a
  symlink) when the program's name differs per arch; uvx unsupported. Always `env.HOME=/thinair-auth` (the agent's
  own login/config dir lands in its volume), login_command = the agent itself, check_command none. Refuses ids of
  built-ins unless --force. HTTP helpers are looked up at call time (`get=None`) so tests can monkeypatch them.
- `AgentConfig.download` (+ `install` now optional; one of them required): a build stage FROM python:3.12-slim runs
  a base64-embedded fetch script (urllib, sha256 check when given, tarfile `filter="data"` / zipfile, exec bits
  kept), then `COPY --from=thinair-fetch /opt/thinair-agent`. No download tool needed in the agent image.
- All 43 Linux-installable registry agents generate valid configs (2026-11-07). Live: Copilot (auth hint OK) and
  OpenCode (download path; answers with no login via its free models) verified; others unchecked, see
  docs/agents.md.

## Agent conversations (phase 4)
- `Job.interactive` / `Job.ask_permissions` (migration 0009, server_default false). Only ACP agents (`/api/agents`
  `conversational`); others 422 `not_conversational`. Retry keeps both flags.
- Driver (acp_driver.js) with `THINAIR_ACP_INTERACTIVE=1`: after each turn prints `{"acp":"waiting"}` and reads JSON
  lines from ITS stdin: message (queued while busy), permission {request, option|null}, stop (session/cancel), end.
  `THINAIR_ACP_APPROVAL=ask` forwards session/request_permission as `permission_request` {request, options} and
  replies when answered (`permission` {by: you|timeout|auto}); unanswered/idle after `THINAIR_ACP_IDLE_S`
  (`agent_idle_s`, 600) -> cancelled / conversation ends. stopReason `cancelled` counts as OK (you stopped it).
- Sandbox stdin: `SandboxSpec.stdin` -> `stdin_open`; `StdinPipe` is connected with `container.attach_socket(stdin,
  stream)` BEFORE start (nothing lost). Verified through tecnativa/docker-socket-proxy with only CONTAINERS + POST
  (no new proxy permissions). `CLIAdapter.run(task, interactive=, ask=)` + `send(cmd)`; executor sets
  `ctx.on_input = adapter.send`; `JobService._contexts` + `send_input(job_id, cmd)`.
- API `POST /api/jobs/{id}/input` {type, text?, request?, option?}: 403 `not_your_job` unless the caller IS the
  job's user (admins can see but not answer: it runs with that user's login), 409 not_conversational / finished /
  not_ready (no handler yet), 422 for empty text etc.
- UI: project form checkboxes `#opt-chat` (only enabled for conversational agents; the choice is remembered only
  when it was enabled) and `#opt-ask`; job page `#reply-form` (Send / Stop this step / Finish), permission cards
  (`li.ask`, closed by the matching `permission` event, so SSE replay restores them), user bubbles (`li.user`).
- A waiting conversation keeps its sandbox and one job slot (`max_concurrent_jobs`, default 1).
- Tests: test_acp.py (driver over real stdin with the fake agent's `chat` mode), test_conversations.py (API + fake
  executor), test_acp_docker.py (stdin through a real container), browser smoke 49 checks.

## Install on fresh machines / Oracle (`install.sh`, docs/oracle.md)
- Missing Docker -> `install_docker` (get.docker.com, consent via prompt or THINAIR_INSTALL_DOCKER=yes; no tty = no),
  adds the user to the docker group; when docker needs root this run, a `docker()` shell function wraps `sudo docker`
  (shellcheck SC2032/2033 disabled on purpose). Tailscale missing -> `install_tailscale` (tailscale.com/install.sh),
  `sudo tailscale up` prints the login link; `tailscale serve` retried through sudo (needs root/operator).
- Oracle detection: `/sys/class/dmi/id/chassis_asset_tag` == OracleCloud.com (what cloud-init uses). Domain mode on
  Oracle explains the console security list (+ offers firewalld on Oracle Linux; iptables is not needed, see
  "Phone reachability"). get.docker.com does not support Oracle Linux: docs say Ubuntu.
- `thinair doctor` "Version" check: anonymous GET api.github.com/repos/SudeepSigdel/ThinAir/releases/latest; warns
  when newer; update = re-run the installer. Untested on a real Oracle VM (owner will test).

## Remote runners (`src/thinair/runners.py`, `api/runner_api.py`, `runner_client.py`, phase 3 step 21)
- Owner's defaults (accepted with "continue"): the runner connects OUT (long-poll), one secret per runner
  (`thinair_runner_` + 256 bit, SHA-256 in table `Runner`), profile jobs only, shared by everyone. Docs: docs/runners.md.
- Server: `RunnerHub` (in memory) holds pending job ids + `Lease`s. `JobService(remote_executor=, goes_remote=)` puts
  routed jobs on a second queue with their own cheap workers (`remote_workers`, default 8; they only wait, so they
  don't block local jobs). `remote_executor` offers the job, waits for a claim (auto: `runner_claim_timeout_s`, then
  returns status QUEUED -> `_requeue_locally`), then for `lease.done`; silent > `runner_lease_s` -> `runner_lost`.
  Placement `auto|runners|local` (auto = remote only if `hub.has_idle_runner()` at enqueue time).
- Protocol v1 (`/runner/v1`, `require_runner`; user tokens never accepted there, runner secrets never on /api):
  `claim?wait=` (<=25 s, 204 = nothing) -> {id, kind, repo_url, ref, github_auth}; `jobs/{id}/events` (log /
  profile_run, also the heartbeat) -> `{cancel}`; `jobs/{id}/finish` (profiled + ProfileResult, or failed + code).
  Bodies read with a 4 MB cap, then pydantic-validated. Unknown/foreign lease -> cancel:true / accepted:false.
  The report is saved by `executors.save_profile_result` (shared with the local profile executor).
- Runner: `thinair runner start` (`RunnerClient`, `requests`), secret from `THINAIR_RUNNER_TOKEN`/`--token-file`,
  https required except localhost (`--allow-http`). Re-validates the payload with `GitSource.validate()`. Uses its
  OWN `THINAIR_GITHUB_TOKEN` only when `github_auth` is true. No janitor sweep (it could share a Docker host).
  `docker-compose.runner.yml` (socket proxy without NETWORKS/EXEC).
- Tests: `test_runners.py` (API protocol via one TestClient, JobService fallback/lost, and the real client against a
  live uvicorn on a random port with a fake profiler).

## Pull requests (`src/thinair/jobs/pr.py`, phase 2 step 14)
- Agent job end: `CLIAdapter._collect_patch` writes `git diff --cached --binary --full-index` to `.git/thinair.patch`
  and reads it back with `SandboxManager.get_file` (Docker archive API; captured output is line-based and would
  mangle CRLF/long lines). For clones the patch is against `commit_sha`, restricted (`:(literal)` pathspecs) to paths
  changed vs the post-install baseline, so edits to install-created files still apply. Cap 5 MB -> `patch_too_large`.
  Stored in `JobResult.patch`; workspace volume is still deleted.
- `POST /api/jobs/{id}/pr` -> `JobService.submit_pr` (checks token, success, patch, no open/active PR) -> `pr` job
  (`Job.parent_job_id`). Executor: fresh volume, `fetch --depth 1 origin <sha>` (token env), put patch, `apply --index`,
  commit as `THINAIR_GIT_AUTHOR_NAME/EMAIL` (no trailers), `push --force HEAD:refs/heads/thinair/<agent-job-id>`,
  then REST `POST /repos/{o}/{r}/pulls` from the server (`GitHubClient`, `requests`); 422 "already exists" reuses the
  open PR. `pr_url` saved on both results. `GET /jobs/{id}/patch` downloads the patch. Retry of a pr job = new pr job.
- UI: agent result shows Create PR / Download patch / PR link (only `https://github.com/` URLs become links).
- Tests: `test_pr.py` (fake HTTP + fake executors), `test_pr_docker.py` (byte-exact round trip incl. CRLF/binary/
  deletion/install-file edit; patch conflict; push with a fake token -> `push_failed`). Real GitHub push/PR is
  untested (needs the owner's token). Browser smoke now 28 checks.

## Notifications (`src/thinair/notify.py`, phase 2 step 15)
- `JobService(notifier=...)`: after `_execute` records the final status, `_announce` fires a background task (kept in
  a set) so a slow or broken notifier never delays/fails a job; skipped while stopping. `notifier(job_id)` reads the DB.
- `TelegramNotifier`: plain-text `sendMessage` (no parse_mode), outbound only. The bot token is in the URL, so request
  exceptions are reduced to their type name (their text contains the URL). Formats validated (`BOT_TOKEN_RE`,
  `CHAT_ID_RE`); malformed settings log "notifications disabled" at startup instead of crashing.
- Messages: repo, kind, status, task first line, error (<=300) or result (files changed / PR URL / minimum config),
  link via `THINAIR_PUBLIC_URL`. Never logs, diffs or summaries. Cancelled jobs are silent. CLI: `thinair notify-test`.

## Egress allowlist (`src/thinair/sandbox/egress*.py`, phase 3 step 17)
- `egress_proxy.py`: stdlib asyncio CONNECT proxy, sent as `python -u -c <source>` (keep it self-contained). Only
  `CONNECT name:443` with `host_allowed` (exact or `*.dom`, no IP literals); prints `[egress] allowed|blocked <host>`.
- `EgressGateway`: internal network `thinair-net-<job>` (labelled; janitor sweeps networks) + proxy container
  `thinair-egress-<job>` via `SandboxManager.start_service(spec, name, also_on=["bridge"])` (same hardening as runs,
  64 MiB). Agent spec gets `network_name` + `HTTPS_PROXY`/`http_proxy`... `SandboxSpec.network_name` only accepts
  `thinair-net-*`. Compose's socket proxy needs `NETWORKS: 1`.
- `AgentConfig.egress` (None = open). Claude: api.anthropic.com, claude.ai, platform.claude.com (verified with the
  real CLI + fake key). Codex honours the proxy (tried api.openai.com, chatgpt.com, api.github.com) but login hosts
  are undocumented -> open. Gemini: undocumented -> open. Settings `THINAIR_AGENT_EGRESS=allowlist|open`,
  `THINAIR_AGENT_EGRESS_EXTRA_HOSTS`. Tests: `test_egress.py` (in-process proxy), `test_egress_docker.py`.

## Releases and install (`.github/workflows/release.yml`, `install.sh`)
- Workflow pushes multi-arch (amd64 + arm64, QEMU) server images with `EXTRAS=postgres` to
  `ghcr.io/<owner>/thinair` (metadata-action lowercases): tag `vX.Y.Z` -> `X.Y.Z`, `X.Y`, `latest`; main -> `edge`.
  Action majors verified 2026-11-06: checkout v7, setup-qemu v4, setup-buildx v4, login v4, metadata v6, build-push v7.
  `latest` exists only after the first `v*` tag.
- Compose files have BOTH `image: ${THINAIR_IMAGE}:${THINAIR_VERSION:-latest}` and `build:`. Verified: with a
  missing image, `docker compose up` BUILDS (needs the source) instead of pulling, so always `docker compose pull`
  first (the installer does). `up --build` from a checkout still builds locally.
- `install.sh` (POSIX sh, shellcheck-clean, keep it so): body in `main()` called on the last line (a truncated
  `curl | sh` runs nothing). Server: ~/thinair, writes `.env` once (`thinair_` + 32 urandom bytes, umask 077), pull
  (falls back to a local image), up, waits on `/healthz` via `compose exec`, prints URL + token. `--runner`:
  ~/thinair-runner, `.env.runner` from env or prompts on /dev/tty, https required. `THINAIR_RAW_URL=file:///repo`
  for offline installs and tests. Tested in `docker:cli` (busybox ash) with the repo mounted read-only, against a
  locally tagged image: fresh install, re-run/upgrade, runner mode, input checks, a real profile job.
  Phone step (first install, or whenever `THINAIR_PHONE=auto|public|tailscale|funnel|domain|skip` is given; default
  auto, see "Phone reachability" below). Tailscale = `tailscale serve|funnel --bg --https=443 http://127.0.0.1:<port>`
  (docs: port forwarding needs no root; funnel only proxies to http://127.0.0.1); URL from `tailscale status --json`
  `CertDomains[0]`, EMPTY while the tailnet has HTTPS certificates off (verified in ipnstate.go). serve/funnel are now
  run BEFORE reading CertDomains because per Tailscale's docs they show a consent link that enables certificates
  (unverified with a real tailnet). Domain = THINAIR_DOMAIN + THINAIR_PUBLIC_URL + `COMPOSE_PROFILES=https` in .env
  (so plain `up` keeps Caddy). Re-runs never touch phone setup except following a changed IP of an sslip.io name.
  After startup it starts `thinair images` DETACHED (`compose exec -d`, so the QR comes right away), runs
  `thinair doctor` and prints its report only when it exits 1, then the final screen: URL, `thinair qr --ttl 30`
  (also after an address change), the "public on the internet" line when Caddy serves it. The admin token is printed
  only when there is no phone URL. `has_subcommand` guards every command older pinned images may lack.
- `thinair doctor` (`src/thinair/doctor.py`): read-only checks -> `Check(status, name, detail, fix)`, exit 1 on any
  fail. Must not create the SQLite file (`_db_missing`). Network calls (all skipped by `--offline`): GitHub
  `GET /rate_limit` with the token, the anonymous release check, the public-IP lookup (sslip.io names only, and not
  when THINAIR_PUBLIC_IP is set), and an HTTPS probe of its own address. Tests use a fake Docker client.

## Phone reachability (`src/thinair/netinfo.py`, onboarding v2 phase 1)
- Default: a public IPv4 -> Caddy serves `https://<a-b-c-d>.sslip.io` (Let's Encrypt). `thinair net detect` prints
  KIND/PUBLIC_IP/LOCAL_IP/CLOUD/HOST lines for the installer (parsed with sed, never eval'd). Kinds: direct (public IP
  on the machine: wait 180 s), cloud (Oracle/GCP/Azure 1:1 NAT: wait 15 min with a tty, 5 without), nat (anything else,
  incl. AWS: wait 90 s, then offer "keep waiting" or revert and ask Funnel/Tailscale/domain/skip), none (CGNAT 100.64/10,
  no IPv4: ask). The local IP comes from `ip -4 route get 1.1.1.1` (sends nothing); DMI strings are cloud-init's
  ds-identify ones (AWS needs root-only product_uuid, so it is not recognised). Public IP: THINAIR_PUBLIC_IP, else one
  GET to https://ipv4.icanhazip.com (answers must parse as a public IPv4).
- Readiness = `thinair net https-check HOST --via caddy:443` inside the server container: a TLS handshake that VERIFIES
  the certificate for HOST (SNI) and GETs /healthz. No hairpin routing needed, and a valid public cert proves Let's
  Encrypt reached the machine from outside. Never restart Caddy to "hurry" ACME: Let's Encrypt limits failed
  validations; Caddy retries with backoff itself (docs: LE -> ZeroSSL -> exponential backoff up to 1 day).
- IP changes: doctor `Public address` (fail + fix) and a one-shot background check at server start
  (`api/app.py check_origin`, result in `app.state.origin_warning` + a log warning; no polling). Re-running the installer
  moves .env to the new name, waits for the cert and prints a 30-minute QR (re-pair = one scan; the old origin's cookie
  can't follow). A hand-set THINAIR_PUBLIC_IP is saved in .env and then trusted instead of a lookup.
- Public exposure hardening: uvicorn `proxy_headers` with `forwarded_allow_ips=THINAIR_TRUSTED_PROXIES` (default
  loopback + RFC1918 + fc00::/7; uvicorn 0.54 takes the right-most untrusted X-Forwarded-For entry; Caddy discards
  client-sent X-Forwarded-*). `cli.main.uvicorn_options()` is shared by `serve` and the live-server test. HSTS
  `max-age=31536000` on https responses, NEVER includeSubDomains (sslip.io siblings belong to others).
  `THINAIR_API_DOCS=auth` (default) serves /docs, /redoc, /openapi.json behind `require_auth` (FastAPI's own routes off).
- Testing the installer without ACME: in `docker:cli`, a COPY of docker-compose.yml with Caddy `--internal-certs` on
  high ports; for the success path that copy also makes Caddy copy its root.crt into a shared volume and points the
  server's `SSL_CERT_FILE` at it (uid 10001 can't read Caddy's pki dir). Pass THINAIR_VERSION to every later manual
  `compose up` or Compose silently starts an older local `latest` image.
- Docker-published ports bypass ufw (Docker docs: packets are diverted before ufw's INPUT chain), so the installer does
  not touch ufw. Same for Oracle's iptables `INPUT ... REJECT`: tested on a real Oracle Ubuntu VM (2026-11-14), a
  Docker-published port answers from outside with it untouched, so the installer only explains the console security
  list (firewalld on Oracle Linux, untested, is still offered).
- `tailscale serve`/`funnel` listen on the tailnet address:443, which makes Docker's 0.0.0.0:443 publish fail. Switching
  a Tailscale install to public (`THINAIR_PHONE=public`) turns that handler off first (`release_tailscale_443`) and
  restores it if public mode fails (found on the Oracle VM).

## Agent sign-in from the phone (`src/thinair/agents/login.py`, `api/login_api.py`, onboarding v2 phase 2)
- `auth.login_flow` (agents/config.py `LoginFlow`: command, url, url_hosts, code_out, code_in, help) on claude and
  codex. `FlowMachine` (pure) cleans terminal output (OSC 8 links, CSI colours, CR) and moves preparing -> link ->
  verifying -> done|failed|expired|cancelled. A match counts only when followed by whitespace (never half a link cut
  between reads). A code is typed (+ "\r") only while the `code_in` prompt is unanswered; a new prompt after a code =
  refused -> back to link. Exit 0 -> `check_command` decides done/failed.
- `LoginHub` (`app.state.logins`, in memory only; `create_app(login_backend=)` for fakes): one session per user
  (409 `login_running` + its id), `MAX_SESSIONS`=3 (429), 10 min after the container starts, finished sessions kept
  2 min. Sessions are 404 for everyone but their starter (admins too). Never write them to EventStore/DB; the log gets
  state names only (a test asserts no link/code in logs and no JobEvent rows). `known[volume]` feeds
  `GET /api/agents` `login: {in_app, state}` (never runs a container there).
- `DockerLoginBackend`: agent image for the node stack, `SandboxManager.open_terminal(spec)` (tty+stdin, attach
  BEFORE start, same call as StdinPipe, so no new socket-proxy permission: verified on the Oracle ARM VM through
  tecnativa/docker-socket-proxy), egress gateway when the agent has `egress` (ensure the python sandbox image first:
  sign-ins skip workspace preparation, which normally builds it). Job label `login-<sid>` (janitor sweeps).
- `Terminal` reads in a daemon thread into a queue: Docker Desktop's Windows named pipe ignores socket timeouts
  (a `recv` with a timeout hung forever). `run()` refuses tty specs.
- Real CLI output (2026-11-14) is recorded in docs/agents.md "In-app login"; Claude's link host is `claude.com`
  (not claude.ai), PKCE present; Codex device URL https://auth.openai.com/codex/device, code `XXXX-XXXXX`.
  Tests: test_agent_login.py (canned bytes at every cut, API with FakeBackend), test_agent_login_docker.py (fake login
  program through a real tty, real CLIs up to the link with `-test` ids). Never complete a real login in tests.
- UI: header link Agents (`#/agents`: state, Sign in / Check, terminal hint for others) and `#/agent/<id>/login`
  (O3: link button, paste box or big device code + Copy, status). SSE events carry the same payload as GET
  (`LoginSession.public()`; a smoke test caught "undefined is signed in" when they differed). A job failed with
  `agent_not_authenticated` links to the sign-in screen. Browser smoke (Edge, phone size): 19 checks.

## Connect GitHub (`src/thinair/github_app.py`, `github.py`, `api/github_api.py`, onboarding v2 phase 3, docs/github.md)
- Per-server GitHub App via the manifest flow. Credentials (app id, slug, client id/secret, PEM) live ONLY in
  `$THINAIR_DATA_DIR/secrets/github-app.json` (`secrets.py`: dir 0700, file 0600, temp+rename). Never DB/env/logs;
  `AppCredentials.__repr__` is `***`. `VolumeMount` refuses the server's data volume (`thinair-data`, `*_thinair-data`,
  `THINAIR_DATA_VOLUME`). Doctor `GitHub App` check is local only (file mode, installations).
- `GitHubAccess.token_for(url, need)` (github.py): app installation key if one covers the repo (`GET /repos/o/r/installation`
  with the app JWT), else the PAT, else None. Needs: clone = contents:read, push = contents:write, pr = pull_requests:write
  + metadata:read, list = metadata:read; always `repositories=[repo]`. `Credential.arevoke()` in `finally` (clone, push, PR,
  picker). App errors fall back to PAT/anonymous and set `access.note` (used in hints). `prepare_workspace(github_auth=)`
  still takes a bool (True = may use the server's connection: app > PAT) or a `GitHubAccess`, so executors and runners are
  unchanged. PR job: push key and PR key are separate; the PR key is in no SandboxSpec. `require_credentials()` is the
  no-network early check (the existing test needs it BEFORE any DB access).
- Tables (migration 0010): `GitHubInstallation` (non-secret), `OneTimeState` (SHA-256, 10 min, single use,
  `consume_state` is a conditional UPDATE), `Repo.private` (cosmetic lock from the picker).
- Routes `/api/github` (auth): GET status, POST app (admin; 409 `no_public_url`/`app_exists`), POST sync, GET repos,
  DELETE app. OPEN: `/callback/app` (needs a live `github_app` state BEFORE any outbound call; bad states count toward
  the login throttle) and `/callback/setup` (never trusts `installation_id`; per-IP 20/min + 5 s global cooldown unless it
  carries a live `github_install` state). GitHub's docs don't say whether `state` is echoed to the setup URL, so nothing
  depends on it. CSP `form-action 'self' https://github.com` (the manifest is a real form POST).
- Commit author: `THINAIR_GIT_AUTHOR_EMAIL` if set, else the app owner's noreply address (`commit_identity()`).
  `THINAIR_GITHUB_FOR_MEMBERS` = alias of `..._TOKEN_FOR_MEMBERS`. No `workflows` permission on purpose.
- UI: header link GitHub -> `#/setup/github` (O2), `#/setup/project` (O4 picker), home "Choose from your GitHub
  repositories". `thinair github status|disconnect`. Tests: test_github_app.py (fake GitHub session, 40), docker clone with a
  fake app key. Browser smoke (Edge, phone size, scratch): 26 checks; never follow GitHub's redirect in the browser
  (read `Location` with `ctx.request.get(maxRedirects: 0)`, else it leaves for the real github.com).
- `cryptography` is a dependency (RS256); arm64 manylinux wheels exist (checked 2026-11-17).

## First run, setup state, health (`setup_state.py`, `api/setup_api.py`, onboarding v2 phase 4)
- `SetupEvent(user_key, name, at, value)` (migration 0011; owner's `user_key` is "owner" because a nullable user id
  can't be unique). Funnel events are stamped ONCE by `setup_state.mark(name, user_id)` (never raises): `installed`
  (startup), `paired` (`pairing.redeem`), `github_connected` (setup callback/sync), `agent_connected` (LoginHub done),
  `project_added` (`add_repo`, new repos only), `first_job` (`JobService.submit`), `first_agent_success`/`first_pr`
  (`_funnel` after a job ends). Choices are rows too: `skip:<step>`, `dismissed`, `preferred_agent` (value). They never
  leave the server; `thinair stats` prints them.
- `steps_for()` computes `done` from real state every time (github = app with an installation or a PAT; agent =
  preferred agent's login in `LoginHub.known` OR a succeeded agent job; project = repos visible; first_task = any
  agent/profile job by that user). Members' github step is "managed by this server's admin". `/api/setup` GET/POST
  (skip, unskip, dismiss, preferred_agent -> single-flight background `ensure_agent_image(node)`), `/api/meta` (open,
  60/min per address), `/api/health` (admin; `doctor.run_checks(online=False)` in a thread, problems first, `Check` now
  has `id` and `action=(kind,target,label)`), `POST /api/health/fix/images` (202, single flight).
- Short pairing codes: `create_pairing(session, user_id, ttl, short=True)` -> (long, `XXXX-XXXX`); Crockford base32,
  `short_hash`, `failures` on the row; a wrong short code increments `failures` on ALL live short rows, 20 burns the short
  form (long QR unaffected). `POST /api/pair` takes either; `thinair qr --short`; the installer uses it when supported.
- UI: `#/setup` (Welcome + agent radios), `#/setup/github|agent|project|first-task` with a progress bar and Skip, "Finish
  setting up" card + first-visit hop to Welcome (localStorage `thinair.setup.seen`), empty states, `#/settings/health`
  (admin link at the bottom of Projects). `#/setup/project` shows an address field when GitHub isn't connected. Screens
  render before their data arrives: browser tests must poll for text (a `textIs` helper), not read once.

## Web Push (`src/thinair/push.py`, `notify.py` `WebPushNotifier`, `api/push_api.py`, onboarding v2 phase 5)
- `push.py`: VAPID key in `secrets/vapid.json` (generated on first use, never when `THINAIR_WEB_PUSH=false`), ES256 JWT
  (12 h, `aud` = the push service origin, `sub` = the https public URL or `mailto:thinair@example.com`), RFC 8291
  `aes128gcm` encryption on `cryptography` (HKDF + AESGCM; `test_push.py` checks it against RFC 8291's own Appendix A
  vector), `send()` (blocking, `allow_redirects=False`, errors never contain the endpoint: it identifies a device).
  `endpoint_allowed`: https, no userinfo/port/IP literal, host `fcm.googleapis.com` or a subdomain of
  `push.services.mozilla.com` / `push.apple.com` / `notify.windows.com`.
- `PushSubscription` (migration 0012) tied to the creating API token: `auth.revoke_token` and `users.disable_user` delete
  them; 404/410 deletes at once, 5 failures in a row delete. One per login, 20 per person. Routes `/api/push` (GET status +
  public key, POST/DELETE `/subscriptions`, POST `/test` 10 s cooldown). Payload `{"web_push": 8030, "notification":
  {title, body, navigate, tag}}` (iOS 18.4+ shows it natively; `sw.js` `push`/`notificationclick` do it elsewhere and
  only open same-origin addresses).
- `notify.job_summary()` feeds Telegram (`job_message`, byte-identical) and `WebPushNotifier` (to the JOB OWNER's
  subscriptions only). `JobService(notifier=<Telegram>, push_notifier=, attention_notifier=)`: keep Telegram in
  `notifier` (an existing test requires `_notifier is None` when its settings are bad). Each notifier runs on its own
  and never touches the job. `JobService.attention(job_id, kind)` (agent executor calls it on `permission_request` /
  `waiting` via `ctx.attention`) is debounced to one push per job per 60 s; tests move the clock through
  `service._now`, never `time.monotonic` (that would freeze asyncio).
- UI: banner `#push-banner` on Projects and on a running job (once per job), iOS Add-to-Home-Screen steps, Android
  `beforeinstallprompt` -> "Install the app", "Not now" remembered in localStorage; manifest `id` + `share_target`
  (a shared GitHub link prefills the add-project form). Real push delivery is untested here (needs a real phone).

## Deployment (`Dockerfile`, `docker-compose.yml`)
- Server image: no apt; `/data` pre-created and owned by 10001 (empty-volume ownership gotcha again); optional
  `PIP_INDEX_URL` build arg fed from `THINAIR_PIP_INDEX_URL`. `.dockerignore` sends only pyproject/README/src.
- Compose pins `THINAIR_HOST=0.0.0.0` / `THINAIR_PORT=8080` inside the container; `.env`'s THINAIR_PORT/BIND only
  choose the published host port. Verified end to end through the socket proxy (image build, clone, real profile).
- The socket proxy grants only what Thinair calls (containers, images, volumes, networks, build, info): no EXEC.
- Measured idle: ~82 MiB total (docs/footprint.md). Never start a Compose instance while Docker tests run: its
  startup sweep removes the tests' sandbox containers.

## Status
MVP (steps 1-12) done: 223 tests (187 fast + 36 Docker), browser smoke 24/24, Compose quickstart verified on this
machine (not on a clean VM), first real public repo profiled (more-itertools: 0.25 CPU / 512 MiB).
Phase 2 done (13 private repos, 14 pull requests, 15 Telegram, 16 Codex + Gemini configs). Browser smoke 28/28.
Phase 3: 17 egress allowlist (Claude Code on by default), 18 Postgres support, 19 multiple users (design choices
made without the owner, listed in README "Several users"; review them), 20 ACP agents and 21 remote runners
(profile jobs only) done.
Phase 4 (2026-11-07): projects screen + per-project defaults + dependency cache, ACP registry agents, agent
conversations (replies + approvals from the phone), installer that installs Docker/Tailscale and knows Oracle Cloud.
