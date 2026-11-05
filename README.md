# Thinair

Thinair is a small, self-hosted server for working on your code from your phone. Point it at a
GitHub repository and it does one of two things inside a locked-down Docker sandbox:

- **Resource profile:** runs the project's tests under shrinking CPU and memory limits and
  reports the smallest setup that still passes, plus a recommended size with headroom.
- **Agent task:** runs a coding agent CLI (Claude Code today) on a task you type, such as
  "fix the failing tests", using **your own** agent login, then shows you what it changed as a diff.

Thinair never talks to an AI model itself. It drives the official agent CLIs as black boxes.

## The 30-second demo

On your phone, open Thinair and paste `https://github.com/you/your-api`. Tap **Start profile**.
The log streams in while Thinair clones the repo, installs its dependencies once, and re-runs the
tests at 2, 1, 0.5 and 0.25 CPUs with less and less memory. A run that gets killed for lack of
memory shows up as 💥. About a dozen runs later you get a card: *Minimum that passes: 0.25 CPU /
256 MiB. Recommended: 0.5 CPU / 384 MiB.* Switch to **Agent task**, type "make the flaky date
test deterministic", and watch Claude Code work. When it finishes, review the diff.

## Status

Early, working MVP (phase 1). Working and tested: resource profiles, Claude Code agent tasks,
live logs, the mobile web app (installable as a PWA), token auth, Docker Compose install.
Not yet built: private repositories, opening pull requests, more agents (Codex, Gemini),
notifications, multiple users. See [the roadmap](#roadmap).

## Quickstart (about 5 minutes)

You need a Linux machine (a 1–4 GB VM is enough, ARM or x86) or a desktop with Docker, plus
[Docker](https://docs.docker.com/engine/install/) with the Compose plugin.

```sh
git clone https://github.com/<you>/thinair.git
cd thinair
cp .env.example .env              # optional: every setting has a sensible default
docker compose up -d --build      # first build takes a few minutes
docker compose logs thinair | grep -A3 "admin token"
```

Copy the token from the log (it is shown only once) and open <http://localhost:8080>. Paste the
token to log in. Lost it? Run `docker compose exec thinair thinair token create --name phone`.

The first job builds the small sandbox images (Python, Node, git). Later jobs reuse them.

### Using it from your phone

By default Thinair listens only on `127.0.0.1`. Pick one way to reach it:

- **A tunnel (easiest):** [Tailscale](https://tailscale.com/), or a
  [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
  pointed at `http://localhost:8080`. To install the app from a tunnel URL, it must be HTTPS
  (for Tailscale, enable HTTPS certificates and use `tailscale serve`).
- **Your own domain with automatic HTTPS:** point a DNS name at the server, set
  `THINAIR_DOMAIN=thinair.example.com` in `.env`, open ports 80 and 443, and run
  `docker compose --profile https up -d`. Caddy gets a certificate for you.

Then open the URL on your phone and use "Add to Home Screen". It runs like an app. Jobs keep
running on the server when you close it, and the live log picks up where it left off when you
come back.

## Running a coding agent

Each agent logs in **once, with your account**, into its own Docker volume on your server:

```sh
uv run thinair auth claude        # from a checkout with Python + uv (needs a terminal)
# or, with Docker Compose:
docker compose exec thinair thinair auth claude --print-command   # then run the printed command
```

In Claude Code, type `/login`, open the link on any device, sign in, paste the code back, then
type `/exit`. Check the result with `thinair auth claude --check`. As an alternative, generate a
token on your own computer with `claude setup-token` and set `CLAUDE_CODE_OAUTH_TOKEN` in `.env`.

Then choose **Agent task** in the app, pick the agent, describe the task, and start it. You get
the agent's messages live, its summary, and the full diff. Nothing is pushed anywhere.

Adding another agent takes one YAML file, no code changes. See [docs/agents.md](docs/agents.md).

## Configuring a project: `thinair.yaml`

Most Python (`requirements.txt` / `pyproject.toml`) and Node (`package.json`) projects work with
no configuration. To override anything, add `thinair.yaml` to the repository root:

```yaml
version: 1
stack: python                # python | node | auto (default: detect)
install: pip install -r requirements.txt -r requirements-dev.txt   # runs once, network on
test: pytest -q              # runs for every profile step, network off
env: {APP_ENV: test}         # plain, non-secret values only
workdir: backend             # if the project lives in a subfolder
timeouts: {install_s: 600, test_s: 300, agent_s: 1800}
network: {install: true, test: false, agent: true}
profile:
  cpus: [2, 1, 0.5, 0.25]
  memory: [2g, 1g, 512m, 256m, 128m]
  headroom: 1.5              # recommended = minimum × headroom, rounded to a usual size
  confirm_runs: 2            # the minimum is re-run this many times to catch flaky results
```

Unknown keys are errors, so typos are reported instead of ignored. Python dependencies go into
`/workspace/.venv` and Node dependencies into `node_modules`, both inside the job's workspace.

### How the profile works

The tests run once with the most resources. If they don't pass there, Thinair stops and tells you
why: tests failing, out of memory, timeout, or a command that wouldn't start. Otherwise it
binary-searches memory at the highest CPU level, then checks each lower CPU level starting from
the previous answer. It assumes more memory never hurts and less CPU never needs *less* memory,
and every report says so. Swap is disabled so out-of-memory kills can't hide. Peak memory and CPU
time come from the container's own cgroup counters, read inside the container. That gives
typically 10–14 runs instead of 20 for a 4 × 5 grid. Reports download as Markdown or JSON.

## Command line

```text
thinair init                     write .env with a fresh admin token
thinair serve                    run the server (what the container runs)
thinair profile <dir|url>        profile locally without the server; --md/--json to save the report
thinair auth <agent> [--check]   log an agent in (interactive), or check its login
thinair agents                   list agent configs (and report broken ones)
thinair token create|list|revoke manage API tokens
```

The HTTP API is documented at `/docs` on your server (log in there with the same token).

## Security model

- **Sandboxes:** every clone, install, test run and agent run happens in a fresh container that
  runs as uid 10001. All Linux capabilities are dropped, and it has no-new-privileges, a read-only
  root filesystem, hard CPU, memory, process and time limits, and no swap. Sandboxes never get the
  Docker socket or any host folder. Only named volumes can be mounted. Test runs have no network.
- **Docker access:** Thinair itself reaches Docker only through
  [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy), which exposes the few
  API sections it needs. Honest caveat: anything that can create containers is powerful. Run
  Thinair on a machine (or VM) you dedicate to it. Rootless Docker or a Docker-in-Docker sidecar
  work too (set `THINAIR_DOCKER_HOST`).
- **The API** requires a token from the first start. Only SHA-256 hashes of tokens are stored,
  wrong tokens are rate-limited, and the phone app uses an HttpOnly, SameSite=Strict cookie.
  The web app has a strict Content-Security-Policy and renders all job output as text.
- **Containers are cleaned up** after every step, on cancel, and on the next start after a crash.
- **Agents and your login:** see the FAQ below. Only run agents on repositories you trust.

Found a security problem? See [SECURITY.md](SECURITY.md).

## FAQ: credentials

**Does Thinair see or store my Claude (or other) login?** No. The login lives in a Docker volume
named `thinair-auth-<agent>` that is mounted only into that agent's own sandboxes. Thinair's code
never reads it, never copies it, and never sends it anywhere. If you use a token from
`claude setup-token` instead, it sits in your server's `.env` and is passed only into that agent's
container, masked in every log.

**Can I share my Thinair server with friends so they use my subscription?** No, and don't.
Thinair is single-user by design. Every agent run uses the login of the person who set it up, so
anyone you give a token to would be using *your* account. Each person should run their own
Thinair with their own login. Check your plan's terms for automated use.

**Is my login safe from the code the agent works on?** Not fully. To work unattended the agent
runs without approval prompts inside the sandbox. A malicious repository could trick it into
reading and sending out its own login from inside the container. The agent's own documentation
gives the same warning. Use agent tasks on your own or trusted code, and revoke or rotate the
login if in doubt (`docker volume rm thinair-auth-claude`).

**Why does Thinair need network access in agent runs?** The agent has to reach its model
provider. Profile test runs have no network.

**Does Thinair call an AI API itself?** Never. It starts the agent's own CLI and reads its output.

## Footprint

Idle, Thinair is one Python process plus a tiny Docker API proxy. Nothing polls and no sandbox
runs. Memory limits are set on every service. Measured numbers are in
[docs/footprint.md](docs/footprint.md).

## Troubleshooting

- **"Cannot reach the Docker daemon"**: Docker isn't running, or `THINAIR_DOCKER_HOST` is wrong.
- **Installs hang or time out**: some networks block or throttle PyPI or npm from containers. Set
  `THINAIR_PIP_INDEX_URL` / `THINAIR_NPM_REGISTRY` in `.env` to a mirror you can reach.
- **"Could not detect the project type"**: add a `thinair.yaml` with `stack`, `install` and `test`.
- **"Claude Code is not logged in"**: run `thinair auth claude` (see above).
- **The 0.25 CPU runs time out**: that's a real result (too slow at that size). Raise
  `timeouts.test_s` if you want them to finish anyway.

## Roadmap

- **Phase 2:** private repositories (read-only token used only for cloning), Codex CLI and
  Gemini CLI configs, notifications (Telegram plugin), opening a pull request from an agent's diff.
- **Phase 3:** outbound allowlist for agent runs, multiple users, Postgres, remote runners,
  Agent Client Protocol (ACP) adapter.

## Contributing

Contributions are welcome, from typo fixes to new agent configs. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) and the [good first issues](docs/good-first-issues.md).

## License

[MIT](LICENSE)
