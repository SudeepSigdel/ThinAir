# Remote runners

A **runner** is another machine that runs profile jobs for your Thinair server. A typical setup
has a small, always-on VM that hosts Thinair and takes requests from your phone, and a bigger
computer at home that does the heavy test runs.

- The runner **connects out to the server**. It needs Docker and an internet connection, and no
  open port, so it works behind a home router.
- It proves who it is with **its own secret**, which you create on the server. User API tokens
  don't work as runner secrets, and runner secrets don't work as API tokens.
- It runs **profile jobs only**. Agent jobs need agent logins and pull requests need the
  server's GitHub token, so those always stay on the server.
- It is **shared by everyone** on the server. Jobs keep their owner and visibility rules.

## Set one up

On the server:

```sh
docker compose exec thinair thinair runner add homebox
```

This prints a secret starting with `thinair_runner_`, shown once.

On the runner machine, get Thinair and create `.env.runner`:

```sh
git clone https://github.com/SudeepSigdel/ThinAir.git thinair && cd thinair
cat > .env.runner <<'EOF'
THINAIR_SERVER_URL=https://thinair.example.com
THINAIR_RUNNER_TOKEN=thinair_runner_...
EOF
docker compose -f docker-compose.runner.yml up -d
docker compose -f docker-compose.runner.yml logs -f runner   # "Runner started; waiting ..."
```

Without Compose, use `uv run thinair runner start --server https://...` with
`THINAIR_RUNNER_TOKEN` set in the environment. The secret is never accepted on the command line,
where other users of the machine could see it in `ps`; `--token-file` reads it from a file.

Back on the server, `thinair runner list` (or `GET /api/runners`) shows the runner, when it was
last seen and its CPU count. `thinair runner disable homebox` locks it out at once.

## Where jobs run: `THINAIR_RUNNER_PLACEMENT`

| Value | Profile jobs |
|---|---|
| `auto` (default) | Go to a runner if one is waiting for work when the job is queued, otherwise run on the server. A job sent to a runner that nobody picks up within `THINAIR_RUNNER_CLAIM_TIMEOUT_S` (60 s) comes back and runs on the server. |
| `runners` | Always run on a runner and wait until one is free. Use this when the server is too small to run tests at all. The job time limit (`THINAIR_JOB_TIMEOUT_S`) includes the wait. |
| `local` | Never use runners. |

With no runner registered, `auto` behaves exactly like before.

## What a runner gets, and what it can do

The server sends a runner the job's id, repository URL and ref, and whether the job's owner may
use a GitHub token. That is all it sends. It never sends the server's GitHub token, agent logins,
API keys, other jobs or other users' data.

- **Private repositories:** give the runner its own `THINAIR_GITHUB_TOKEN` in `.env.runner`
  (fine-grained and read-only is enough). The runner uses it only for jobs whose owner may use a
  token on the server (admins, or members with `THINAIR_GITHUB_TOKEN_FOR_MEMBERS=true`).
- **Trust:** a runner reports results. A broken or malicious runner can report a wrong profile
  or log lines for the jobs it claims, and nothing more. It can't post to jobs it doesn't hold,
  can't read the API, and is limited to bounded, validated messages (4 MB per request). Only
  register machines you control.
- **Isolation on the runner:** jobs run in the same hardened sandboxes as on the server: non-root,
  no capabilities, resource limits, no network for test runs. The runner reaches Docker through
  docker-socket-proxy with fewer API sections than the server needs.
- Use `https://` for the server address. The runner refuses plain `http://` except to
  `localhost`, unless you pass `--allow-http` (or set `THINAIR_RUNNER_ALLOW_HTTP=true`) on a
  private network you trust.

## How it works

The runner long-polls `POST /runner/v1/claim`. Each request waits up to 25 s, below common proxy
timeouts. While a job runs, it posts log lines and probe results to
`/runner/v1/jobs/<id>/events` about once a second, with an empty post every 10 s as a
heartbeat. At the end it posts the report to `/runner/v1/jobs/<id>/finish`. The server stores
it exactly like a local profile, so the phone shows a remote job just like a local one, plus the
line `running on runner 'homebox'`.

- **Cancel:** the reply to the runner's next post says `cancel: true`, and the runner stops its
  sandbox.
- **A runner that goes silent** for `THINAIR_RUNNER_LEASE_S` (90 s), because it was switched off
  or lost its network, loses the job, which fails with `runner_lost`. Retry it. A runner that
  claimed a job but never reported anything for it (for example, it restarted in the middle of
  the request) doesn't fail the job: the job is offered again.
- **Server restarts** mark running jobs `interrupted`, as for local jobs. The runner learns about
  it on its next post and stops.
- **Idle cost:** one small request per runner every 25 s. On the server, the jobs waiting for a
  runner use a separate set of lightweight workers, so they don't block local agent jobs.

The runner runs one job at a time. Run several runners (each with its own secret) for more.
