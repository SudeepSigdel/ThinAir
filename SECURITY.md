# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's "Report a vulnerability" button
(Security tab of this repository). Don't open a public issue. Include steps to reproduce and the
version or commit. You can expect a first answer within a week.

Most interesting are escapes from a sandbox, ways to reach the Docker API or host files from a
job, ways to read another agent's login, authentication bypasses, and output (logs, diffs, repo
names) that runs as code in the web app.

## What Thinair protects, and what it doesn't

Thinair is a self-hosted tool for one person or a small group that trusts each other with the
machine. It runs untrusted code (the repositories you point it at, their dependencies, and coding
agents) inside Docker containers.

**Protected by design**

- Every sandbox runs as uid 10001 with all capabilities dropped, no-new-privileges, a read-only
  root filesystem, CPU, memory, process and time limits, and no swap. Mounts can only be named
  volumes, so sandboxes never get the Docker socket or host paths.
- Test runs have no network. Install and agent runs do (they need package registries and the
  model provider).
- Thinair reaches Docker through docker-socket-proxy, limited to the API sections it uses.
- Connect GitHub (a per-server GitHub App) mints keys for one repository that last at most an hour
  and are revoked when the step ends: a read-only key for the clone container, a write key for the
  push container, and a pull-request key that never leaves Thinair's process. The app's private key
  is a 0600 file in the data volume (never the database or environment), no sandbox can mount that
  volume, and `thinair doctor` flags a readable key. The two GitHub redirect routes are open but act
  only on a one-time state or re-read what GitHub says, and are throttled. See docs/github.md.
- The optional GitHub token (`THINAIR_GITHUB_TOKEN`) is passed only to the clone container (and,
  for pull requests, to a push container that runs no repo code, plus Thinair's own REST calls),
  through git's environment config scoped to `https://github.com/`. It is never in argv,
  `.git/config`, the workspace, the database or logs, and never reaches install, test or agent
  containers. Malformed values are refused so they can't inject extra HTTP headers.
- The API requires a token. Only SHA-256 hashes are stored, wrong tokens are rate-limited, and the
  browser session is an HttpOnly, SameSite=Strict cookie. The web app ships a strict CSP and
  renders all job output as plain text.
- Signing an agent in from the phone relays only the sign-in link and a one-time code between
  the agent's own login command (in a hardened container with only that user's login volume)
  and the user who started it. Nothing about it is stored or logged; other users, admins
  included, get 404. Links are tappable only for `https` hosts the agent's config allows; a
  pasted code is stripped of control characters and typed only at the CLI's code prompt.
  One sign-in per user, three per server, ten minutes each.
- With several users: members only see their own jobs (others' are a 404), only admins manage
  users, each user's agent login lives in its own volume, the owner's API keys only reach the
  owner's jobs, and the server's GitHub token is not used for members' jobs unless
  `THINAIR_GITHUB_TOKEN_FOR_MEMBERS=true`. Disabling a user revokes all their tokens.

- Remote runners connect out to the server with their own secret (hashed like API tokens, not
  usable on the API) and only ever get profile jobs: a repository URL, a ref and a yes/no for
  using a GitHub token. They never receive the server's GitHub token, agent logins, API keys or
  other users' data. Their messages are size-capped and validated, and a runner can only report
  on jobs it currently holds.

- Phone pairing (`thinair qr`): the QR code holds a random one-time code (only its SHA-256 is
  stored) that expires after 10 minutes and works once. It travels in the URL fragment, which
  browsers never send to servers or proxies, and the app removes it from the address bar at once.
  Redeeming it creates a new token for that phone; your own token is never in the QR code.
  Anyone who sees the QR code before it is used can sign in with it, so treat it like a password
  for those 10 minutes.
- Members can't fill the shared queue: at most `THINAIR_MEMBER_JOB_LIMIT` (default 5) unfinished
  jobs each.
- On a public address (the installer's default on machines with a public IP), only the web app's
  static shell, `/healthz`, login and pairing answer without a token; the API docs and schema
  need a login (`THINAIR_API_DOCS`). HTTPS responses carry HSTS (without `includeSubDomains`:
  other people own the sibling names under sslip.io). The failed-login throttle counts the real
  client address: `X-Forwarded-For` is believed only from `THINAIR_TRUSTED_PROXIES` (loopback and
  private ranges, where Caddy sits), and Caddy discards that header when a client sends it.
- GitHub tokens are treated as opaque (GitHub App tokens can be ~520 characters with dots) but
  must be printable ASCII without whitespace, so they can't inject or split HTTP headers. Output
  is masked before long lines are cut, so a cut never reveals part of a secret.

**Not protected (know these before you deploy)**

- **Users are not isolated from each other at the machine level.** All jobs share one Docker
  host, so a member who escapes a sandbox can reach everyone's data. Only add people you would
  trust with a shell on the machine.
- **A runner's results are trusted.** A compromised runner can report wrong profiles and log
  lines for the jobs it claims. Only register machines you control, and disable one
  (`thinair runner disable <name>`) if in doubt.
- **Docker isolation is not a hard security boundary** against kernel exploits. For stronger
  isolation run sandboxes under gVisor (`runsc`) or in a VM. Dedicate the machine to Thinair.
- **Anything that can create containers is powerful.** The socket proxy narrows the API, but a
  compromise of the Thinair process itself is serious. Keep the host patched and the API token
  secret.
- **Agent logins can be stolen by a malicious repository.** Agents run without approval prompts
  inside their sandbox. Code they work on could trick them into reading and sending out the login
  mounted in that sandbox. Only run agent tasks on repositories you trust, and rotate the login
  (`docker volume rm thinair-auth-<agent>`, then log in again) if in doubt.
- **The GitHub App's private key is on disk** in the data volume, so backups of that volume contain
  it. If it is stolen, the thief can mint tokens for every repository the app is installed on until
  you delete the app on GitHub (the same reach as a read/write token for those repositories).
- **The GitHub token is visible to the host's Docker users** while a clone runs (`docker inspect`
  shows container environment). That is the same group that can already control Thinair. Use a
  fine-grained, read-only token limited to the repos you need.
- **Outbound filtering covers Claude Code agent runs only.** Install steps, Codex and Gemini CLI
  runs still have open network access. An allowlisted host that accepts uploads (for example an
  API with an attacker's own key) can still receive data, so the allowlist narrows exfiltration;
  it does not make a malicious repository safe.
- **A public address is found by scanners within minutes.** Certificates are published in
  Certificate Transparency logs, so the sslip.io (or your own) name is public knowledge. Thinair's
  login is the only thing between strangers and your server: keep it updated (`thinair doctor`
  says when a release is out), or use Tailscale (`THINAIR_PHONE=tailscale`) to stay off the
  internet.
- **sslip.io is a third party.** It only answers DNS (it never sees your traffic), but if it is
  down, or Let's Encrypt's quota for sslip.io names is used up, your address stops resolving or
  can't get a certificate. Your own domain or Tailscale avoids that dependency.
- Share a server only with people you trust with the machine (see the first point above): each
  user gets their own agent logins, but all sandboxes share one Docker host.
