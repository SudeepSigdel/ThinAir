# Thinair: working notes for Claude

Open-source, self-hostable platform: run coding-agent CLIs in Docker sandboxes, control from a phone,
and profile the minimum CPU/RAM a project needs to pass its tests.

## Hard constraints
- Agent-agnostic: Thinair never calls an LLM API. It drives agent CLIs as black boxes.
- Never proxy/store subscription credentials. Each agent logs in inside its own Docker volume
  (`thinair-auth-<agent>`) that Thinair never reads.
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
  (10/min/IP); requests with *no* token are a plain 401 and never count (else reloading the logged-out UI locks you out).
- `session_scope()` uses `expire_on_commit=False` so rows stay readable after the session closes.

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
  (`[A-Za-z0-9_]{20,255}`, no header injection). Only `clone_spec()` gets it, as `GIT_CONFIG_COUNT/KEY_0/VALUE_0`
  = `http.https://github.com/.extraHeader: Authorization: Basic b64(x-access-token:TOKEN)`; token and b64 are
  both in `redact`. Never in argv, .git/config, DB, install/test/agent containers. A blank env value falls back to
  the file (`.env.example` has `THINAIR_GITHUB_TOKEN=`). Docker tests use a fake token: GitHub rejects it -> `clone_failed`.
- Dependencies are installed into the workspace volume (`/workspace/.venv`, `node_modules`) so network-less test
  containers can use them. Sandbox base images are built from `sandbox/images/*.Dockerfile` on first use.
- Fixtures in `tests/fixtures/` hold ~150 MB in their tests; expected profiler minimum is the 256m tier.

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
  use locators. 24 checks pass, incl. XSS-as-text, cancel/retry, agent tab + diff, reload, logout, dark mode, SW.

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

## Deployment (`Dockerfile`, `docker-compose.yml`)
- Server image: no apt; `/data` pre-created and owned by 10001 (empty-volume ownership gotcha again); optional
  `PIP_INDEX_URL` build arg fed from `THINAIR_PIP_INDEX_URL`. `.dockerignore` sends only pyproject/README/src.
- Compose pins `THINAIR_HOST=0.0.0.0` / `THINAIR_PORT=8080` inside the container; `.env`'s THINAIR_PORT/BIND only
  choose the published host port. Verified end to end through the socket proxy (image build, clone, real profile).
- Measured idle: ~82 MiB total (docs/footprint.md). Never start a Compose instance while Docker tests run: its
  startup sweep removes the tests' sandbox containers.

## Status
MVP (steps 1-12) done: 223 tests (187 fast + 36 Docker), browser smoke 24/24, Compose quickstart verified on this
machine (not on a clean VM), first real public repo profiled (more-itertools: 0.25 CPU / 512 MiB).
Phase 2: step 13 (private repos via clone-only token) done: 245 tests (206 fast + 39 Docker). Next: 14 PR creation
(store full binary patch + base SHA; `pr` job re-clones, applies, pushes `thinair/<job-id>`, opens PR via REST),
15 Telegram notifications (outbound only), 16 Codex/Gemini configs (verify flags first).
