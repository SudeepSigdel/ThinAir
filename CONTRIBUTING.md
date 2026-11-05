# Contributing to Thinair

Thanks for helping. Small, focused pull requests are easiest to review. If you plan something
larger, open an issue first so we can agree on the approach.

## Set up (about 2 minutes)

You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and, for the integration tests, Docker.

```sh
git clone https://github.com/<you>/thinair.git && cd thinair
uv sync --extra dev
uv run pytest -m "not docker"     # fast suite, no Docker needed (~20 s)
uv run thinair serve              # http://localhost:8080 (token printed on first start)
```

## Tests

| Command | What it runs |
|---|---|
| `uv run pytest -m "not docker"` | Unit and API tests with fake executors: no Docker, no network |
| `uv run pytest -m docker` | Real containers: sandbox hardening, cloning, the profiler on the sample projects, agents |
| `uv run ruff check . && uv run ruff format --check .` | Lint and formatting (CI runs both) |

Docker tests skip themselves when no Docker daemon is reachable. They need internet for pip/npm,
and they leave their images behind to make re-runs fast (the agent ones are ~1 GB each): list them
with `docker images 'thinair-*'` and remove what you don't need.
On networks where PyPI is slow from containers, set `THINAIR_PIP_INDEX_URL` to a mirror.

Before calling a change done:

1. The fast suite and ruff pass, and the Docker tests do too if you touched sandbox, project,
   profiler, jobs or agents code.
2. New behaviour has a test that would fail without your change. Assert the *reason*, not only
   that something failed: a check like `error_code == "clone_failed"` once hid a broken clone.
3. If you changed the API or the web app, try it in a real browser and server, not only the tests.

## Where things live

```text
src/thinair/
  api/        FastAPI app, auth, REST + SSE routes, web UI serving + security headers
  jobs/       queue, workers, event log (SSE), job executors (profile, agent)
  sandbox/    hardened container runner, janitor, sandbox image Dockerfiles
  project/    thinair.yaml, stack detection, clone/upload + install
  profiler/   search strategy (Docker-free), failure classification, metrics, reports
  agents/     agent YAML configs, stream parser, CLIAdapter
  web/static/ the mobile web app (plain HTML/JS/CSS, no build step)
  db/         SQLModel tables + Alembic migrations
tests/        one file per area; *_docker.py files need Docker
docs/         agents.md and other reference docs
```

`CLAUDE.md` holds the working notes: decisions, conventions and hard-won gotchas. It is worth a
read before touching the sandbox or job code.

## Conventions

- **Security first.** Sandboxes stay non-root, capability-free and resource-limited, with no
  Docker socket and no host mounts. The spec makes host paths impossible to express; keep it that
  way. Secrets never go into logs (use `redact`).
- **Small footprint.** Nothing may poll while idle. Thinair must keep running on a 1 GB VM.
- **Dependencies:** add one only with a comment in `pyproject.toml` saying why.
- **Errors users see** say what happened and what to do next ("…run `thinair auth claude`"),
  never a stack trace.
- **Web app:** never build HTML from strings (a test enforces it). Use `el()` / `textContent`.
- **Database changes** need an Alembic migration (a test fails if you forget). See `CLAUDE.md`.
- **External CLIs:** check flags against official docs and record what you checked in
  `docs/agents.md`.
- Commit messages: [Conventional Commits](https://www.conventionalcommits.org/) (`fix: …`, `feat: …`).

## Adding a coding agent

Usually this is just a YAML file. See [docs/agents.md](docs/agents.md). Please include the docs
links you verified the flags against, and a test in `tests/test_agents_unit.py` that pins the
important flags.

## Reporting security issues

Please don't open a public issue. See [SECURITY.md](SECURITY.md).
