# Coding agents

Thinair never talks to a model itself. It runs an agent's own command-line tool, non-interactively,
inside a sandbox next to your project, and shows you what it did. Each agent is described by one
YAML file, so adding an agent needs no code.

## Logging an agent in (your account, your server)

Each agent keeps its login in its own Docker volume, `thinair-auth-<id>`. That volume is mounted
only into that agent's sandboxes. Thinair never reads, copies or sends it anywhere, and the
cleanup job never deletes it.

```sh
thinair auth claude           # opens Claude Code; type /login, follow the link, then /exit
thinair auth claude --check   # prints "logged in" or "NOT logged in"
```

If Thinair runs in Docker Compose, run `docker compose exec thinair thinair auth claude --print-command`
and paste the printed `docker run -it ...` command into a terminal on the Docker host.

**Alternative:** generate a long-lived token on your own machine with `claude setup-token`, then
set `CLAUDE_CODE_OAUTH_TOKEN` in Thinair's `.env`. Variables listed under `auth.env_passthrough`
are forwarded only into that agent's container and are masked (`***`) in all logs.

Use only your own account. Thinair is single-tenant by design: one person's login must never run
another person's tasks. Check your plan's terms for automated use.

## Security model for agent runs

- The agent runs as uid 10001, with all Linux capabilities dropped, no-new-privileges, a read-only
  root filesystem, CPU, memory, process and time limits, and no access to the Docker socket or host
  files. Only the project workspace and the agent's own login volume are mounted.
- Agents need network access to reach their model provider, so the sandbox has it.
- To work unattended, Claude Code runs with `--permission-mode bypassPermissions`. That is safe
  for your machine because the container is the boundary. It is **not** safe for your login:
  as Claude Code's own docs warn, a malicious repository could get the agent to read and send out
  anything inside the container, including the credentials in its login volume. **Only run agents
  on repositories you trust.** An outbound allowlist (egress proxy) is planned.

## Claude Code: what was verified (2026-11-05)

Source: the official docs at code.claude.com (`headless`, `cli-reference`, `iam`, `settings`,
`env-vars`, `devcontainer`, `network-config`). The pinned version comes from `npm view`.

| Need | Flag / setting | Notes |
|---|---|---|
| Non-interactive run | `-p "<task>"` | Exit code 0 on success, non-zero on failure |
| Machine-readable stream | `--output-format stream-json --verbose` | One JSON object per line; the last is a `result` message (`result`, `is_error`, `total_cost_usd`, `session_id`) |
| Act without approval prompts | `--permission-mode bypassPermissions` | Headless runs otherwise start in "manual" mode and deny actions. The CLI refuses this mode as root |
| Bound the run | `--max-turns 80` | `--max-budget-usd` also exists (client-side estimate) |
| Login location | `CLAUDE_CONFIG_DIR=/thinair-auth` | Credentials file: `.credentials.json` in that directory |
| Login check | `claude auth status` | Exit 0 = logged in, 1 = not |
| Token login | `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`), or `ANTHROPIC_API_KEY` | API key wins over subscription login in `-p` mode |
| Quiet, no self-updates | `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | |
| Install | `npm install -g @anthropic-ai/claude-code@2.1.311` | Needs Node.js 22+ (copied into Python images) |

Not documented, so handled defensively: the exact shape of assistant/tool messages in the stream.
The parser shows text and tool calls it recognises and logs everything else verbatim.

Codex CLI and Gemini CLI configs are not shipped yet. Their flags will be verified the same way
before they are added.

## Adding an agent

Drop `<id>.yaml` into `$THINAIR_DATA_DIR/agents/` (`/data/agents/` in Docker). A file whose id
matches a built-in config replaces it. Check your file with `thinair agents`.

```yaml
id: myagent                 # lowercase; login volume becomes thinair-auth-myagent
name: My Agent
version: "1.2.3"            # informational
install: npm install -g my-agent-cli@1.2.3   # run as root while building the agent image
needs_node: true            # copy Node.js 22 into Python project images
output: text                # or stream-json (Claude Code's format)
command: [my-agent, run, --yes, "{task}"]    # argv, no shell; "{task}" is exactly one item
env: {MY_AGENT_HOME: /thinair-auth}
auth:
  config_dir: /thinair-auth                  # where the login volume is mounted
  login_command: [my-agent, login]           # run interactively by `thinair auth myagent`
  check_command: [my-agent, whoami]          # exit 0 = logged in (optional)
  env_passthrough: [MY_AGENT_TOKEN]          # optional host variables to forward (masked in logs)
  login_help: Follow the prompts, then exit.
  hint: My Agent is not logged in. Run `thinair auth myagent`.
auth_error_patterns: [not logged in]         # output that means "bad login"
limits: {cpus: 2, memory: 2g, pids: 1024, timeout_s: 1800}
network: true
```

The task is always passed as one argument and never through a shell. A task that starts with `-`
gets a leading space, so the agent cannot read it as an option. Unknown keys are rejected, so
typos fail loudly.
