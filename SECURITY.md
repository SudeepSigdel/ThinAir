# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's "Report a vulnerability" button
(Security tab of this repository). Don't open a public issue. Include steps to reproduce and the
version or commit. You can expect a first answer within a week.

Most interesting are escapes from a sandbox, ways to reach the Docker API or host files from a
job, ways to read another agent's login, authentication bypasses, and output (logs, diffs, repo
names) that runs as code in the web app.

## What Thinair protects, and what it doesn't

Thinair is a single-user, self-hosted tool. It runs untrusted code (the repositories you point it
at, their dependencies, and coding agents) inside Docker containers.

**Protected by design**

- Every sandbox runs as uid 10001 with all capabilities dropped, no-new-privileges, a read-only
  root filesystem, CPU, memory, process and time limits, and no swap. Mounts can only be named
  volumes, so sandboxes never get the Docker socket or host paths.
- Test runs have no network. Install and agent runs do (they need package registries and the
  model provider).
- Thinair reaches Docker through docker-socket-proxy, limited to the API sections it uses.
- The optional GitHub token (`THINAIR_GITHUB_TOKEN`) is passed only to the clone container (and,
  for pull requests, to a push container that runs no repo code, plus Thinair's own REST calls),
  through git's environment config scoped to `https://github.com/`. It is never in argv,
  `.git/config`, the workspace, the database or logs, and never reaches install, test or agent
  containers. Malformed values are refused so they can't inject extra HTTP headers.
- The API requires a token. Only SHA-256 hashes are stored, wrong tokens are rate-limited, and the
  browser session is an HttpOnly, SameSite=Strict cookie. The web app ships a strict CSP and
  renders all job output as plain text.

**Not protected (know these before you deploy)**

- **Docker isolation is not a hard security boundary** against kernel exploits. For stronger
  isolation run sandboxes under gVisor (`runsc`) or in a VM. Dedicate the machine to Thinair.
- **Anything that can create containers is powerful.** The socket proxy narrows the API, but a
  compromise of the Thinair process itself is serious. Keep the host patched and the API token
  secret.
- **Agent logins can be stolen by a malicious repository.** Agents run without approval prompts
  inside their sandbox. Code they work on could trick them into reading and sending out the login
  mounted in that sandbox. Only run agent tasks on repositories you trust, and rotate the login
  (`docker volume rm thinair-auth-<agent>`, then log in again) if in doubt.
- **The GitHub token is visible to the host's Docker users** while a clone runs (`docker inspect`
  shows container environment). That is the same group that can already control Thinair. Use a
  fine-grained, read-only token limited to the repos you need.
- **No outbound filtering yet** for install and agent runs (planned).
- Do not share one Thinair server between people. Every agent run uses the owner's login.
