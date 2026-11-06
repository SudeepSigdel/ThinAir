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

Use only your own account: one person's login must never run another person's tasks. With
several Thinair users (see the README), every user has their own volume,
`thinair-auth-<id>.<user>`, and logs in with `thinair auth <id> --user <user>`; their jobs only
ever mount that volume. The owner keeps `thinair-auth-<id>`, and only the owner's jobs get the
`env_passthrough` variables from Thinair's `.env`. Check your plan's terms for automated use.

## Security model for agent runs

- The agent runs as uid 10001, with all Linux capabilities dropped, no-new-privileges, a read-only
  root filesystem, CPU, memory, process and time limits, and no access to the Docker socket or host
  files. Only the project workspace and the agent's own login volume are mounted.
- Agents need network access to reach their model provider, so the sandbox has it.
- To work unattended, Claude Code runs with `--permission-mode bypassPermissions`. That is safe
  for your machine because the container is the boundary. It is **not** safe for your login:
  as Claude Code's own docs warn, a malicious repository could get the agent to read and send out
  anything inside the container, including the credentials in its login volume. **Only run agents
  on repositories you trust.** Claude Code runs are limited to Anthropic's hosts (see
  [Outbound allowlist](#outbound-allowlist)); that narrows, but does not remove, this risk.

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

## Codex: what was verified (2026-11-06)

Sources: `codex exec --help`, `codex login --help` and `codex login status` run from the pinned
npm package in a container; the official docs (developers.openai.com/codex: non-interactive mode,
CLI reference, auth, environment variables); and the JSON event schema in the source at tag
`rust-v0.166.0` (`codex-rs/exec/src/exec_events.rs`).

| Need | Flag / setting | Notes |
|---|---|---|
| Non-interactive run | `codex exec [OPTIONS] -- "<task>"` | The prompt is positional; stdin is read only if no prompt is given |
| Machine-readable stream | `--json` | JSONL: `thread.started`, `turn.started`, `turn.completed` (`usage`), `turn.failed` (`error.message`), `item.started/updated/completed`, `error`. Item types: `agent_message` (`text`), `reasoning`, `command_execution` (`command`, `exit_code`, `status`), `file_change` (`changes[].path`), `mcp_tool_call`, `web_search`, `todo_list`, `error` |
| Act without approval prompts | `--dangerously-bypass-approvals-and-sandbox` | Help text: "Intended solely for running in environments that are externally sandboxed". Thinair's container is that sandbox |
| Not a git repo? | `--skip-git-repo-check` | Workspaces always have git here; kept for uploaded folders |
| No session files | `--ephemeral` | |
| Login location | `CODEX_HOME=/thinair-auth` | Must exist (it is the mounted login volume); holds `auth.json` and config |
| Login | `codex login --device-auth` | Prints a URL and a one-time code; works without a browser on the server |
| Login check | `codex login status` | Exit 0 = logged in, 1 = not (checked in a container) |
| Token login | `CODEX_API_KEY` | Documented for `codex exec` |
| Install | `npm install -g @openai/codex@0.166.0` | Node launcher + native binary |

Found by running it: the native binary verifies TLS against the system CA bundle, which
`node:22-slim` does not have (`invalid peer certificate: UnknownIssuer`). The agent config sets
`needs_ca_certs: true`, so the image gets Debian's bundle copied from `python:3.12-slim` (no apt).

Not set: Codex's analytics switch (`[analytics] enabled = false` in `config.toml`) is described in
the docs but not as a flag; add it to `/thinair-auth/config.toml` in the login volume if you want it.

## Gemini CLI: what was verified (2026-11-06)

Sources: `gemini --help` from the pinned npm package, the docs bundled inside that package
(`docs/cli/headless.md`, `trusted-folders.md`, `reference/configuration.md`,
`get-started/authentication.mdx`), its error messages, and the stream-json types in the source at
tag `v0.67.0` (`packages/core/src/output/types.ts`).

**Heads-up from Google's docs:** "Unpaid tier and Google One users: Gemini CLI was replaced by
Antigravity CLI on June 18th, 2026." A Gemini API key or a paid plan still works with Gemini CLI.
An Antigravity CLI config can be added once its headless flags are verified.

| Need | Flag / setting | Notes |
|---|---|---|
| Non-interactive run | `--prompt "<task>"` | A positional prompt starts interactive mode, so it is not used |
| Machine-readable stream | `--output-format stream-json` | JSONL: `init` (`model`), `message` (`role`, `content`, `delta`), `tool_use` (`tool_name`, `parameters`), `tool_result` (`status`, `error`), `error` (`severity`, `message`), `result` (`status`, `error`, `stats`). Assistant text arrives in `delta` chunks, which Thinair joins |
| Act without approval prompts | `--approval-mode yolo` | `--yolo` is deprecated. Gemini's own `--sandbox` needs Docker/Podman and stays off |
| Headless in an untrusted folder | `--skip-trust` | Otherwise `FatalUntrustedWorkspaceError` |
| Login location | `GEMINI_CLI_HOME=/thinair-auth` | The CLI uses `/thinair-auth/.gemini` |
| Login | `gemini`, "Sign in with Google", with `NO_BROWSER=true` | The CLI's own error messages recommend `NO_BROWSER=true` for manual authentication (link + code). Not run with a real account |
| Login check | none | There is no status command. A logged-out run exits 41 with "Please set an Auth method", which Thinair turns into the login hint |
| Token login | `GEMINI_API_KEY` | The documented headless method |
| Install | `npm install -g @google/gemini-cli@0.67.0` | Needs Node.js 20+ |

Optional settings that only exist in `settings.json` (put them in
`/thinair-auth/.gemini/settings.json`): `privacy.usageStatisticsEnabled`,
`general.enableAutoUpdate`, `model.maxSessionTurns`.

## Outbound allowlist

An agent whose config has an `egress:` list runs on a private Docker network
(`thinair-net-<job>`) with no route out. Its only way out is a per-run proxy container on that
network (also on the normal bridge), passed in as `HTTPS_PROXY`. The proxy accepts only
`CONNECT <host>:443` for host names on the list (`*.example.com` matches subdomains, not the
apex); IP addresses, other ports and plain HTTP are refused. Nothing is decrypted. The job log
says which hosts were blocked. The network and proxy exist only during the run.

| Agent | `egress` | Verified how |
|---|---|---|
| Claude Code | `api.anthropic.com`, `claude.ai`, `platform.claude.com` | Hosts from the network-config docs (API; claude.ai sign-in; OAuth token exchange and refresh). Proxy use checked by running the real CLI with a fake API key: it tunnelled to `api.anthropic.com` and the key was rejected |
| Codex | not set (open) | Honours `HTTPS_PROXY` (checked with a fake API key; it tried `api.openai.com`, `chatgpt.com` and `api.github.com`). The hosts a ChatGPT-account login needs are not documented |
| Gemini CLI | not set (open) | Proxy support and hosts are not documented |

To restrict Codex anyway, copy `codex.yaml` into `$THINAIR_DATA_DIR/agents/` and add e.g.
`egress: [api.openai.com, chatgpt.com]`; blocked hosts show up in the job log.

Settings: `THINAIR_AGENT_EGRESS=open` turns the allowlist off for every agent.
`THINAIR_AGENT_EGRESS_EXTRA_HOSTS=registry.npmjs.org,pypi.org,files.pythonhosted.org` adds hosts
for every agent (needed if tasks install packages; each one is another place data could go).

## ACP agents (Agent Client Protocol)

[ACP](https://agentclientprotocol.com/protocol/overview) is a standard way for a client to drive a
coding agent: newline-delimited JSON-RPC 2.0 over the agent's stdin/stdout, protocol version 1.
With `protocol: acp` in an agent's YAML, `command` starts the agent as an ACP server (no
`"{task}"`), and Thinair runs its own small client next to it **inside the sandbox**
(`src/thinair/agents/acp_driver.js`, Node.js standard library only, so the config needs
`needs_node: true`). The driver:

- sends `initialize` (version 1, **no** `fs` or `terminal` capabilities: the agent edits
  `/workspace` itself), `session/new` (`cwd` = the workspace, no MCP servers) and one
  `session/prompt` with the task as a text block;
- answers `session/request_permission` with the first `allow_once` (else `allow_always`)
  option, like the CLI agents' bypass/yolo modes, because the container is the security
  boundary; with no allow option it answers `cancelled`. Any other agent-to-client request gets
  JSON-RPC "method not found";
- turns `session/update` notifications into phone events (message chunks joined per message,
  tool calls, failed tools, plan progress, USD cost) and ignores thoughts;
- treats `stopReason: end_turn` as success; `refusal`, `max_tokens`, `max_turn_requests` and
  `cancelled` as failure. `auth_required` (-32000) or an error mentioning authentication / an API
  key maps to the login hint (driver exit code 3). An agent speaking another protocol version is
  refused. On SIGTERM it sends `session/cancel`.

`auth.shares_login_with: <agent id>` mounts that agent's login volume instead of a new one, so
the built-in ACP variants use your existing logins (`thinair auth claude` / `thinair auth gemini`).

| Built-in | Package / command | Verified (2026-11-06) |
|---|---|---|
| `claude-acp` | `@agentclientprotocol/claude-agent-acp@0.95.0` (the maintained adapter; `@zed-industries/claude-agent-acp` is deprecated) + Claude Code 2.1.311 for login and `claude auth status`; command `claude-agent-acp` | Logged out: `initialize` OK (agentInfo `@agentclientprotocol/claude-agent-acp 0.95.0`), `session/prompt` -> `-32000 Authentication required`. With a fake API key behind the allowlist it only tunnelled to `api.anthropic.com`, then reported the invalid key |
| `gemini-acp` | Gemini CLI 0.67.0, `gemini --acp` (`--experimental-acp` is the deprecated spelling, per `gemini --help`) | Logged out: `initialize` OK (`gemini-cli 0.67.0`, protocol 1), `session/new` -> `-32000 Gemini API key is missing or not configured.`; with a fake key, `session/prompt` fails with "API key not valid" |

Gemini in ACP mode does not need `--skip-trust`: it starts in an untrusted folder and then skips
the project's own hooks and agent definitions, which is what we want for repositories we don't
control. A copy of either config for another ACP agent (e.g. Codex via
`@agentclientprotocol/codex-acp`) needs only `install`, `command` and the login settings.

## Adding an agent

Drop `<id>.yaml` into `$THINAIR_DATA_DIR/agents/` (`/data/agents/` in Docker). A file whose id
matches a built-in config replaces it. Check your file with `thinair agents`.

```yaml
id: myagent                 # lowercase; login volume becomes thinair-auth-myagent
name: My Agent
version: "1.2.3"            # informational
install: npm install -g my-agent-cli@1.2.3   # run as root while building the agent image
needs_node: true            # copy Node.js 22 into Python project images
needs_ca_certs: false       # true: copy Debian's CA bundle in (native binaries on node:22-slim)
output: text                # or stream-json (Claude Code), codex-json, gemini-stream-json
protocol: cli               # or acp (see above): then command starts the ACP server, no "{task}"
command: [my-agent, run, --yes, "{task}"]    # argv, no shell; "{task}" is exactly one item
env: {MY_AGENT_HOME: /thinair-auth}
auth:
  config_dir: /thinair-auth                  # where the login volume is mounted
  login_command: [my-agent, login]           # run interactively by `thinair auth myagent`
  check_command: [my-agent, whoami]          # exit 0 = logged in (optional)
  env_passthrough: [MY_AGENT_TOKEN]          # optional host variables to forward (masked in logs)
  shares_login_with: null                    # or another agent's id to reuse its login volume
  login_help: Follow the prompts, then exit.
  hint: My Agent is not logged in. Run `thinair auth myagent`.
auth_error_patterns: [not logged in]         # output that means "bad login"
limits: {cpus: 2, memory: 2g, pids: 1024, timeout_s: 1800}
network: true
egress: [api.my-agent.example]          # optional allowlist (the CLI must honour HTTPS_PROXY)
```

The task is always passed as one argument and never through a shell. A task that starts with `-`
gets a leading space, so the agent cannot read it as an option. Unknown keys are rejected, so
typos fail loudly.
