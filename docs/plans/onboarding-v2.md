# Onboarding v2: one command, then everything from the phone

Status: plan only (2026-11-14). Nothing here is built yet. Facts marked **[verified]** were checked
against the vendor's current docs on that date (sources in the appendix). Items marked
**[verify in build]** could not be settled from docs and must be checked by running the real thing
before the phase that depends on them ships.

Naming: the code, UI and README say "Thinair"; this plan uses that spelling in microcopy.

---

## 1. Summary

### The new first run, in 10 lines

1. On the VM: `curl -fsSL .../install.sh | sh`. No questions on a public-IP machine.
2. The installer installs Docker if needed, starts Thinair, finds the machine's public IP, turns on
   HTTPS at `https://<ip-with-dashes>.sslip.io` (Caddy + Let's Encrypt), and opens the host firewall.
3. About a minute in, it prints **one big QR code**, a short URL and an 8-character code. Sandbox
   images keep building in the background.
4. Phone: scan. The browser opens Thinair, already signed in. **Welcome** asks one thing: which
   agent you use (so its image starts building while you do the next step).
5. **Connect GitHub**: tap, tap "Create GitHub App" on GitHub, choose repositories, tap Install.
   The server now owns a private GitHub App; no token was pasted anywhere.
6. **Connect your agent**: tap, open the agent's own sign-in link, approve, paste the code back
   (Claude Code) or type the shown code at the provider (Codex).
7. **Pick a repository** from the list GitHub returned. One tap.
8. **Try a first task**: a pre-filled, low-risk task ("add a Project structure section to the
   README"). One tap starts it; the live view opens.
9. **Turn on notifications** while it runs: Add to Home Screen (iOS needs this for push), Allow.
10. The push arrives when the agent is done. Review the diff, tap **Create pull request**, done.

### Today vs. target

Counts are user actions (a command typed, a field filled, a button that matters). Times are the
user's hands-on time, not machine time; target times are estimates to be measured in phase 4.

| Path | Today: actions / tools / time | Target: actions / tools / time |
|---|---|---|
| Public-IP VM (Hetzner, DO, AWS, GCP...) with own domain today | ~28 / 5 (SSH, DNS panel, GitHub settings, agent site, phone) / 15-25 min | **~15 / 3 (SSH once, GitHub, agent site) / about 3 min on the phone** |
| Oracle Free Tier, security list closed | ~38 / 6 (adds Tailscale admin + phone app, or the Oracle console twice) / 25-40 min | ~19 (adds 4 in the Oracle console, once) / about 6 min |
| NAT'd home box | ~34 / 6 (Tailscale everywhere) / 20-30 min | ~19 (adds a Tailscale sign-in on the box) / about 5 min; needs a Tailscale account |
| `.env` edits + restarts | 3-5 edits, 2-3 restarts | **0** |
| Third-party accounts beyond GitHub + the agent | 1-2 (Tailscale or DNS; Telegram) | **0** on public-IP machines; 1 (Tailscale) behind NAT |

What stays impossible: a machine with no public IPv4 cannot be reached without *some* third party
in the path. The plan is honest about that (section 2.2) instead of pretending a tunnel is free.

---

## 2. Decisions

### 2.1 GitHub auth

| Option | Setup for the user | Token model | Works when the server is only reachable from the phone? | Verdict |
|---|---|---|---|---|
| **A. Per-server GitHub App, created by the manifest flow** | 2 taps to create, repo choice + 1 tap to install | Private key on the server mints 1-hour installation tokens, restricted per call to one repo and chosen permissions **[verified]** | **Yes.** Every redirect happens in the phone's browser; GitHub never calls the server (webhook inactive); the server only calls `api.github.com` outbound | **Recommended** |
| B. One shared "Thinair" GitHub App + device flow | Type a code at github.com/login/device, then install the app | User access tokens (`ghu_`, 8 h, refresh 6 months) **[verified]**; cannot be narrowed per repo or permission | Yes | Rejected |
| C. Per-server App + device flow | As A, plus a manual "Enable Device Flow" checkbox in app settings (no manifest field for it **[verified]**) | As B | Yes | Rejected: adds homework, loses scoping |
| D. OAuth App | 1 authorization | `repo` scope = every private repo, long-lived | Yes | Rejected: strictly worse than today's fine-grained PAT |
| E. PAT in `.env` (today) | ~9 actions on GitHub + edit + restart | Long-lived, in every clone container's env | n/a | Kept as the advanced fallback |

**Why A.** It is the only option that is both zero-paste and a security upgrade:

- The long-lived secret (the app's private key) never enters any container. What enters the clone
  container is a token for **one repository, `contents:read`, valid at most 1 hour**, and Thinair
  revokes it (`DELETE /installation/token`) as soon as the step ends. The push container gets a
  separate `contents:write` token; the `pull_requests:write` token never leaves Thinair's process.
  Today the same long-lived PAT goes into every clone and push container and is visible in
  `docker inspect` while they run.
- The existing invariant holds unchanged: tokens reach only clone/push containers through
  `git_auth_env()` (env, never argv, never `.git/config`), are in `SandboxSpec.redact`, and never
  reach install/test/agent containers.
- Bonus: pull requests are authored by `<app-slug>[bot]`, so the user can approve and merge them
  under branch protection that forbids approving your own PR, and (unlike `GITHUB_TOKEN`) they
  trigger CI.

**Why not B, although it needs no private key and no central server.** The problem is not a
central *server*, it is a central *key*: whoever owns the shared app (the maintainer, or anyone who
compromises that account) can mint installation tokens for every Thinair user's repositories. That
breaks the self-hosted promise far more than a per-server app does.

**What we give up with A.**

- A secret is now stored on the server (section 4.1). Today no GitHub secret is stored unless the
  user puts a PAT in `.env`.
- Each server leaves a small app registration in the user's GitHub account (named
  `thinair-<6 hex>`; names are globally unique, so a fixed name would collide).
- PRs come from a bot, not from the user. Commits stay authored by `THINAIR_GIT_AUTHOR_*`; when
  those are unset, default to the connecting account's noreply address
  (`<id>+<login>@users.noreply.github.com`, both in the manifest conversion response).
- No webhooks, so Thinair learns about "repositories added/removed" only when it asks. It asks
  whenever the picker opens and whenever a token mint fails. That is deliberate: no inbound
  dependency, zero idle cost.
- If the server's public URL changes, the app's stored `setup_url` goes stale (there is no API to
  edit an app registration). Effect: after "Configure repository access" GitHub redirects to a dead
  address. Mitigation: the app re-syncs on `visibilitychange`, and Setup health offers "Reconnect
  GitHub" (creates a fresh app; 4 taps).

**Multi-user (deliberate change, staged).**

- Phases 3-6: the app's installations belong to the **server**. `github_token_allowed(user)` keeps
  its exact meaning (owner/admins, or everyone with the setting on) but now gates "may use the
  server's GitHub connection" for both the App and the PAT. `THINAIR_GITHUB_TOKEN_FOR_MEMBERS`
  keeps working; `THINAIR_GITHUB_FOR_MEMBERS` is added as the clearer name (either one enables it).
- Phase 7: members connect **their own** installation of the server's app. That needs the app to be
  public (one switch in the app's Advanced settings; only relevant to servers with members) and
  needs the OAuth step GitHub's docs require: the `installation_id` arriving on the setup URL can be
  spoofed **[verified]**, so a member's claim on an installation is accepted only after a user
  access token for that member lists it in `GET /user/installations`. A job may then use an
  installation if it is the job owner's, or the server's and `github_token_allowed(owner)`.

**Migration.** Nothing changes for an install with `THINAIR_GITHUB_TOKEN`. Resolution order per job
and repository: (1) an app installation that covers the repo and that the job's owner may use,
(2) the PAT if `github_token_allowed(owner)`, (3) anonymous. Setup health shows "GitHub: token in
.env (works). Connect the GitHub App to get short-lived tokens." The PAT is never read or removed
automatically.

**One code fix this decision forces.** Since 2026-04-27 installation tokens can be stateless:
`ghs_<appid>_<JWT>`, about 520 characters, containing dots **[verified]**. `github.TOKEN_RE`
(`^[A-Za-z0-9_]{20,255}$`) rejects them. It must become `^[A-Za-z0-9_.-]{20,4096}$` (still no
whitespace, quotes or CR/LF, so header injection stays impossible). Without this, every App clone
fails with "does not look like a GitHub token".

### 2.2 Reaching the server from the phone

| Option | User steps | Accounts | Oracle Free Tier | Behind NAT/CGNAT | HTTPS good enough to install the PWA | Stable URL | Exposure | Idle cost |
|---|---|---|---|---|---|---|---|---|
| **Public IP + `<ip>.sslip.io` + Caddy/Let's Encrypt** | 0 (Oracle: 4, once, in its console) | none | Yes, after the VCN security list is opened | No | Yes (public CA) | While the IP is stable | Login page on the public internet | Caddy, ~20 MiB (already a Compose service, 48 MiB limit) |
| Let's Encrypt IP certificate (`https://1.2.3.4`) | 0 | none | as above | No | Yes | as above | as above | as above |
| Cloudflare quick tunnel | 0 | none | Yes | Yes | Yes | **No**: new hostname on every start | Public, via Cloudflare | `cloudflared` ~30 MiB |
| Cloudflare named tunnel | ~10 | Cloudflare + a domain on Cloudflare | Yes | Yes | Yes | Yes | Public, via Cloudflare | same |
| Tailscale Serve (today's option) | ~8, phone needs the Tailscale app | Tailscale | Yes | Yes | Yes | Yes | Private (tailnet only) | tailscaled, not ours |
| Tailscale Funnel | ~4, phone needs nothing | Tailscale | Yes | Yes | Yes | Yes (`*.ts.net`) | Public, via Tailscale, bandwidth-limited | same |
| Own domain + Caddy (today's option) | ~6 | DNS provider | Yes, after the security list | No | Yes | Yes | Public | Caddy |
| Thinair relay (server dials out, phone connects through it) | 0 if hosted by the project | none | Yes | Yes | Yes | Yes | Depends on the relay | relay client + a hosted service |

**Recommendation: public IP + sslip.io is the default.** It is the only row with zero accounts,
zero user steps and a real certificate, and it reuses the Caddy service that already exists.

Rejected, with the reason that decides it:

- **Cloudflare quick tunnel**: Cloudflare's docs say "Quick Tunnels do not support Server-Sent
  Events (SSE)" and "have no uptime guarantee" **[verified]**. Thinair's live view *is* SSE, and a
  URL that changes on restart breaks the installed PWA, its push subscription and the GitHub App's
  setup URL. Not offered at all, not even as a fallback.
- **Thinair relay**: not built. An end-to-end encrypted relay still needs someone to run it, a
  domain per server for a browser to trust, and an abuse policy; a project-hosted one makes every
  install depend on the maintainer, which is exactly the central service the project promises not
  to have. A self-hostable relay only helps people who already own a public machine, and those
  people can put Thinair on it. Tailscale Funnel already is this relay, run by someone whose job it
  is. Revisit only if NAT'd installs turn out to be a large share (the funnel events in 3.9 will say).
- **Let's Encrypt IP certificates** (GA since 2026-01-15, 160-hour "shortlived" profile
  **[verified]**): attractive because it removes the sslip.io DNS dependency, but Caddy's support
  is unclear (an open report of IP identifiers being refused, caddyserver/caddy#7399)
  **[verify in build]**. Keep as the planned replacement for sslip.io once Caddy handles it
  reliably; the installer logic is identical.

What we give up with the default:

- **A login page on the open internet.** The certificate lands in Certificate Transparency logs, so
  scanners will find the host within minutes. Section 4.5 lists what that requires (real client IPs
  for the throttle, `/docs` behind auth, HSTS). The secrets are 256-bit tokens, so this is exposure
  to bugs, not to guessing.
- **A dependency on sslip.io's DNS** (a free service run by one maintainer) and on its shared
  Let's Encrypt quota, which has been exhausted before and raised on request **[verified]**. Caddy
  falls back to ZeroSSL when Let's Encrypt refuses **[verify in build]**. If sslip.io is down, the
  name does not resolve and the phone cannot connect; `thinair doctor` says so.
- **The URL is tied to the IP.** On clouds that change the address on stop/start (AWS without an
  Elastic IP), re-running the installer re-points it, and the phone must re-pair. Doctor detects the
  mismatch.

**Fallback order the installer walks, without asking, until one fits:**

1. **Already configured**: `THINAIR_PUBLIC_URL`, `THINAIR_DOMAIN` or an explicit `THINAIR_PHONE` is
   respected as today. Upgrades never change how a server is reached.
2. **Tailscale is already installed, signed in, and has certificates** (`CertDomains` non-empty):
   use `tailscale serve` as today. These users chose private access; don't expose them.
3. **Public IPv4 reachable**: the machine's own interface carries a public address, or it is a
   known cloud with 1:1 NAT (DMI/metadata: Oracle, AWS, GCP, Azure, Hetzner, DigitalOcean), and
   ports 80/443 are free locally. Use sslip.io.
4. **Otherwise (home NAT, CGNAT, IPv6-only)**: detection has failed, so this is the one place the
   installer asks: "1) Tailscale Funnel: public HTTPS link, nothing to install on the phone
   (recommended) 2) Tailscale, private: the phone needs the Tailscale app 3) Skip". Both
   `tailscale serve` and `tailscale funnel` now open a consent page that enables HTTPS certificates
   and the funnel attribute for you **[verified]**, so the "turn on MagicDNS and HTTPS in the admin
   console" homework disappears: the installer prints that one link.

**Oracle, specifically.** The installer can open the host firewall (it already does: `iptables` /
`firewalld`) but not the VCN security list. It can, however, *detect* that state and wait:

- Let's Encrypt is the outside observer. If Caddy has no certificate after 45 s on a machine where
  `on_oracle_cloud` is true, the installer prints exactly one instruction and keeps waiting:

  ```text
  Oracle's cloud firewall is still blocking ports 80 and 443. Open them once (about a minute):
    Oracle console > Networking > Virtual cloud networks > your network > Security Lists >
    Default Security List > Add Ingress Rules: Source 0.0.0.0/0, TCP, Destination ports 80,443
  Waiting for the ports to open... (Ctrl-C, then THINAIR_PHONE=tailscale to skip this)
  ```

  It probes `public-ip:443` from the host every 5 s (on 1:1-NAT clouds the packet leaves through the
  internet gateway and comes back through the security list **[verify in build]** on a real Oracle
  VM; if the hairpin proves unreliable, poll Caddy's log for "certificate obtained" instead) and
  restarts Caddy the moment it connects, so the certificate arrives without waiting for ACME backoff.
- `thinair doctor` gets the same knowledge in one line (3.7).

### 2.3 App technology

Scores: ++ best, + fine, - weak, -- poor. One developer maintains this.

| Criterion | (a) Upgrade the PWA | (b) Wrap it (TWA for Android, Capacitor for iOS) | (c) Native / cross-platform (Compose, SwiftUI, Expo, Flutter) |
|---|---|---|---|
| Install size | ++ ~200 KB cached | + 2-5 MB | - 10-40 MB |
| Cold start | + (shell from the service worker) | + | ++ |
| Push, Android | ++ Web Push via FCM, no Firebase project, no relay | ++ same Web Push | - needs FCM credentials, which a self-hosted server cannot hold without a project-run relay |
| Push, iOS | + iOS 16.4+, **only after Add to Home Screen** **[verified]** | - Capacitor needs APNs keys = relay again | - same relay problem |
| QR scan to pair | + the phone's camera opens the link; no in-app scanner needed | + | ++ in-app |
| Biometric unlock | - (device lock only) | + | ++ |
| Background behaviour | + push wakes the service worker; nothing else needed | + | ++ |
| Build + release burden | ++ none: ships with the server, always the matching version | - signing, two stores, Play's closed-test requirement, review delays | -- a second codebase, two stores |
| Fees / review | ++ none | Play $25 once; Apple $99/yr; Apple often rejects thin wrappers | same fees, full review |
| Duplicated UI | ++ none | + none (same web UI) | -- all of it |
| Share a GitHub URL into the app | + Android (`share_target`); not on iOS | + Android | ++ both |
| Several servers in one app | - one install per server (each origin is its own app) | - | ++ |

**Recommendation: (a), fully.** Reasons that decide it:

1. **Push is the feature that matters, and Web Push is the only kind a self-hosted server can send
   by itself.** VAPID keys are generated on the server; the browser's push service accepts them
   with no account. Native push needs APNs/FCM credentials that belong to an app publisher, so every
   self-hosted server would have to send through a relay run by the project. That is the central
   service again.
2. The app can never be out of step with the server, because the server serves it.
3. Zero release work, zero fees, zero review.

What we give up: push on iOS only after the user adds the app to the Home Screen (the onboarding
makes that step part of "Turn on notifications"); no share-into-app on iOS; no biometric lock; one
Home Screen icon per server. None of these blocks the 3-minute goal.

Later, only if demand shows up: a Trusted Web Activity for Android (S; it is the same PWA, gets a
Play listing) in `apps/android/`. **The "no Node build step" rule stays as written**; nothing in
this plan needs it changed. Anything native lives outside `src/thinair/` with its own toolchain.

iOS detail that shapes the flow: a Home Screen web app has its own cookie jar, but since iOS 17.2
Safari copies its cookies into the app at the moment it is added **[verify in build]** (community
sources, not Apple documentation). So "scan in Safari, set up, then Add to Home Screen" carries the
session over. For older iOS the installed app shows the login screen with "Enter the 8-character
code from `thinair qr`" (3.6), so nobody is stuck.

### 2.4 Agent login

| Option | User steps | What passes through Thinair | Works for | Verdict |
|---|---|---|---|---|
| **A. In-app login: the agent's own login command in a PTY inside its auth container; the app relays the link and the code** | 4-5 taps, no terminal | The sign-in URL (out) and a one-time code (in or out). Never a token | Claude Code, Codex now; any agent with a line-based login | **Recommended** |
| B. Raw terminal in the app (xterm-style) | Many; typing in a TUI on a phone | Everything on the screen | Everything | Later, as the fallback for registry agents (needs a screen emulator; see risks) |
| C. Paste a token/API key in the app | 1 paste, after generating it elsewhere | **The credential itself** | All | Rejected for subscriptions (see below); `.env` stays the place for keys |
| D. Terminal (`thinair auth`, today) | SSH + docker command | Nothing (the user's own terminal) | All | Kept as the advanced path |

**Exactly which bytes pass through Thinair, and why that is inside the constraint.**

- *Claude Code* (`claude auth login`; with no browser it shows a link and a
  `Paste code here if prompted` prompt **[verified]** for the prompt, **[verify in build]** for the
  exact output in a PTY):
  - Out (container to phone): the authorization URL. It carries a client id, a state value and a
    PKCE challenge; none of them is a secret.
  - In (phone to container): the code the provider shows after sign-in. It is a single-use,
    short-lived authorization code that can only be redeemed together with the PKCE verifier, and
    the verifier never leaves the CLI's process memory inside the container **[verify in build]**
    (assert that the URL contains `code_challenge=`). Thinair cannot turn that code into a login.
  - The resulting credential is written by the CLI to `.credentials.json` in the agent's volume.
    Thinair never mounts, reads or copies that volume outside the agent's own containers, as today.
- *Codex* (`codex login --device-auth`): out only, a URL and a one-time user code that the user
  types at the provider. Nothing comes back through Thinair; the CLI polls its provider itself.
  Note for the UI: device-code login must first be enabled in the user's ChatGPT security settings
  **[verified]**.
- Everything is relayed in memory only. The login session never touches `EventStore` (which
  persists to SQLite) or the log; the server logs state transitions ("login started", "succeeded"),
  never content.
- Honest boundary: the Thinair process creates these containers, so a *compromised server* could
  already mount the login volume. Relaying a URL and a code adds no new power to that attacker.
  What the constraint forbids, and what stays forbidden, is Thinair holding, storing or forwarding
  the credential in normal operation.

**Why not C for subscriptions.** A pasted `claude setup-token` token *is* the subscription
credential travelling through Thinair's API and living in its environment. That already exists as
an owner-only `.env` option and stays there, documented as advanced; the phone flow does not offer
it. API keys (`ANTHROPIC_API_KEY`, `CODEX_API_KEY`, `GEMINI_API_KEY`) likewise stay in `.env`.

**Gemini CLI.** Its Google sign-in is a full-screen TUI and its docs say the method "requires a web
browser on a machine that can communicate with the terminal" **[verified]**; Google has also moved
unpaid and Google One users to Antigravity CLI. Not in the guided flow: the Connect screen shows
"Gemini CLI needs an API key in `.env` or a terminal login" with the exact command. It gets in-app
login when option B exists.

---

## 3. Architecture changes

### 3.1 New modules

| Module | Purpose |
|---|---|
| `src/thinair/secrets.py` | `write_secret(name, data)`, `read_secret(name)`, `delete_secret(name)` under `$THINAIR_DATA_DIR/secrets/` (dir 0700, files 0600, written to a temp file then renamed). Used for `github-app.json` and `vapid.json`. Never the DB |
| `src/thinair/github_app.py` | The app: `build_manifest(public_url)`, `convert(code)`, `app_jwt()` (RS256, `iat` 60 s back, `exp` +9 min, `iss` = client id), `sync_installations()`, `installation_for_repo(owner, repo)`, `mint_token(installation_id, repo, permissions)`, `revoke(token)`, `list_repos(installation_id, page)` |
| `src/thinair/api/github_api.py` | Routes in 3.4. Two routers: authenticated `/api/github/*`, and open callbacks authenticated by a one-time `state` |
| `src/thinair/agents/login.py` | `LoginHub` (in memory, `app.state.logins`) and `LoginSession`: runs `auth.login_flow` in a PTY container, parses URL/code, accepts the pasted code, verifies with `check_command` |
| `src/thinair/api/login_api.py` | Routes in 3.4 |
| `src/thinair/push.py` | VAPID key pair, RFC 8291 `aes128gcm` encryption, `send(subscription, payload)`, endpoint host allowlist, `WebPushNotifier` |
| `src/thinair/setup_state.py` | `mark(event, user_id)` (first occurrence only), `steps_for(principal)`, funnel summary |
| `src/thinair/netinfo.py` | Shared by the installer's helper command and doctor: public IPv4 discovery, "is this address on a local interface", cloud detection, sslip name, hairpin probe |
| `src/thinair/api/setup_api.py` | `/api/meta`, `/api/setup`, `/api/health` |

### 3.2 Changed modules (real functions)

| File | Change |
|---|---|
| `github.py` | `TOKEN_RE` widened (2.1). New `GitHubAccess` (built per job from the job's owner): `await access.token_for(repo_url, need)` with `need` in `clone` / `push` / `pr`, returning a `Credential(token, source, revoke)` or `None`. `github_token()` stays as the PAT source. `_explain()` messages stop naming `THINAIR_GITHUB_TOKEN` when the source is the app. `GitHubClient` unchanged |
| `project/prepare.py` | `prepare_workspace(..., github: GitHubAccess \| None)` replaces `github_auth: bool` (a bool is still accepted and mapped: `True` = PAT-only access, `False` = `None`, so `runner_client.py` and `profiler/runner.py` keep working). `_load_source` asks `github.token_for(url, "clone")`, passes it to `clone_spec()` unchanged, and revokes in `finally`. Clone-failure hints gain an App variant ("Thinair's GitHub App can't see this repository. Add it: <manage link>") |
| `jobs/pr.py` | `require_token()` becomes `await access.token_for(url, "push")` for `build_commit`/`push_branch` and a second `token_for(url, "pr")` for `GitHubClient`. `pr_needs_token` message: "Connect GitHub first (Settings > GitHub)". `github_not_allowed` check unchanged |
| `jobs/service.py` | `submit_pr` uses the same resolver for its early check. New `attention(job_id, kind)` hook (3.5). Calls `setup_state.mark` on first job / first PR |
| `jobs/executors.py` | Builds `GitHubAccess.for_user(owner)` in `profile_executor` and `agent_executor`. `owner_github_auth()` stays for the runner protocol (runners keep using their own PAT; no app token is sent to runners in this plan) |
| `users.py` | `github_token_allowed()` reads either settings name. Phase 7 adds `installations_for(user)` |
| `sandbox/spec.py`, `sandbox/manager.py` | `SandboxSpec.tty: bool = False` (only valid with `stdin=True`). `SandboxManager.open_terminal(spec) -> Terminal` creates the container with a TTY, attaches stdin+stdout **before** start (same `attach_socket` call `StdinPipe` uses, so the socket proxy needs no new permission; EXEC stays denied), and exposes `read()`, `write()`, `close()` (kill + remove). Containers are labelled managed with job id `login-<session>` so the janitor sweeps leftovers |
| `agents/config.py` | `AuthConfig.login_flow` (optional): `command`, `url` (regex), `url_hosts` (allowlist for rendering a link), `code_out` (regex), `code_in` (prompt regex), `success` (regex), `help`. Model validator: `url` is required when `login_flow` is set |
| `agents/builtin/claude.yaml`, `codex.yaml` | Add `login_flow` (Claude: `[claude, auth, login]`, hosts `claude.ai`, `platform.claude.com`; Codex: `[codex, login, --device-auth]`, `code_out`). `hint` texts change to "Connect it in Settings > Agents" with the CLI as the alternative |
| `pairing.py` | `create_pairing_code(..., ttl=TTL, short=False)`; short codes (3.6); `redeem` marks `paired` |
| `api/auth.py` | `POST /api/pair` accepts `{code}` (long or short) and optional `"respond": "token"` (returns the new token in the body for non-browser clients; default stays cookie-only 204). `_client_key` unchanged, but see `api/app.py` |
| `api/app.py` | Registers the new routers; composes notifiers (`Telegram` and `WebPush`, each optional); creates `LoginHub`; starts a one-shot background image build when `setup` says images are missing (no loop) |
| `api/web.py` | CSP `form-action 'self' https://github.com` (the manifest flow is a form POST to github.com; nothing else changes). `Strict-Transport-Security` when the request arrived over HTTPS. Serves `/static/icons.svg` |
| `api/routes.py` | `GET /api/agents` adds `login: {in_app: bool, state: "ok"\|"missing"\|"unknown"}` (state from an in-memory cache filled by login sessions, job auth failures and explicit checks; never a container run per list call). `RepoOut` adds `private: bool \| None` |
| `doctor.py` | `Check` gains `id` and `action` (3.7). `_phone` and `_github` rewritten, new `_push`. Module docstring's "only one check leaves the machine" updated |
| `notify.py` | `job_message()` split into `job_summary() -> (title, body, url)` used by both notifiers; Telegram output byte-identical |
| `config.py` | New keys (3.8) |
| `cli/main.py` | `thinair qr --ttl`, `thinair qr --short`; `thinair net detect` (prints what the installer needs as `KEY=value` lines); `thinair github status\|disconnect`; `thinair stats` (funnel); `auth` unchanged |
| `install.sh` | Section 3.6 |
| `docker-compose.yml` | `thinair` gets `THINAIR_TRUSTED_PROXIES` default for the Compose network. Caddy command unchanged |
| `web/static/*` | Section 5 |
| `pyproject.toml` | One new dependency: `cryptography` with the comment `# RS256 for the GitHub App JWT and P-256/AES-GCM for Web Push (stdlib has neither)`. JWT signing and RFC 8291 are ~120 lines on top of it; no PyJWT, no pywebpush. **[verify in build]** that a wheel exists for the server image's platform on arm64 |

### 3.3 Database (migrations continue from 0009)

| Migration | Table / column | Notes |
|---|---|---|
| `0010_github_app` | `GitHubInstallation(installation_id int PK, account_login, account_type, repository_selection, user_id FK NULL, created_at, removed_at)` | Non-secret only. `user_id NULL` = the server's. The app id, slug and key live in `secrets/github-app.json`, never here |
| | `Repo.private bool NULL` | Filled by the picker; shown as a lock |
| | `OneTimeState(hash PK, purpose, user_id NULL, expires_at, used_at)` | SHA-256 only, 10 minutes, one use. For the manifest and install redirects |
| `0011_setup` | `SetupEvent(user_id NULL, name, at)`, unique `(user_id, name)` | Funnel timestamps and per-user `skip:<step>` / `dismissed` |
| | `PairingCode.short_hash NULL`, `PairingCode.failures int default 0` | Short codes |
| `0012_push` | `PushSubscription(id, user_id NULL, token_id FK, endpoint unique, p256dh, auth, created_at, last_ok_at, failures)` | Removed when its `ApiToken` is revoked, on 404/410 from the push service, or after 5 consecutive failures |

No new enum members, so no Postgres `ALTER TYPE`. Login sessions are deliberately not a table.

### 3.4 API

All under the existing rules: `Depends(require_auth)` unless marked open; errors as
`{"detail": {"code", "message"}}`.

**Meta and versioning**

- `GET /api/meta` (open) -> `{"version": "0.3.0", "api": 1, "features": ["github_app", "agent_login", "push", "setup"]}`.
  Contract: within `api: 1` changes are additive only (new fields, new routes; clients ignore
  unknown fields). A breaking change ships as `/api/v2/...` next to v1 for at least one minor
  release and bumps `api`. A client checks `features` before showing a screen, so an old cached
  shell or a future wrapper degrades instead of breaking. The service worker is network-first
  already, so shell and server match whenever the phone is online.

**GitHub**

- `GET /api/github` -> `{"mode": "app"|"token"|"none", "app": {"slug", "html_url", "owner"}|null, "installations": [{"id", "account", "selection": "all"|"selected", "manage_url"}], "can_manage": true}`
- `POST /api/github/app` (admin) body `{"org": null}` -> `{"action": "https://github.com/settings/apps/new?state=<s>", "manifest": "<json string>"}`.
  The page submits a real `<form method="post">` built with `el()` (top-level navigation; no CORS).
  Refused 409 `no_public_url` unless `THINAIR_PUBLIC_URL` is set (the redirects need an address the
  phone's browser can open; a tailnet address is fine).
- `GET /api/github/callback/app?code&state` (**open**; `state` must match an unused `OneTimeState`
  *before* any outbound call) -> converts the code (`POST /app-manifests/{code}/conversions`, valid
  1 hour **[verified]**), writes the secret, 303 to `https://github.com/apps/<slug>/installations/new`.
- `GET /api/github/callback/setup` (**open**) -> ignores the `installation_id` it was given, runs
  `sync_installations()` (app JWT, `GET /app/installations`: authoritative and unspoofable), then 303
  to `/#/setup/github`. With one server-level owner there is nothing to bind, so no OAuth is needed
  until phase 7.
- `POST /api/github/sync` -> re-reads installations (called on `visibilitychange` while the GitHub
  screen is open).
- `GET /api/github/repos?installation_id=&page=` -> `{"repos": [{"id", "full_name", "url", "private", "default_branch", "pushed_at", "description", "added"}], "total", "manage_url"}`,
  newest push first. Uses a token minted with `{"metadata": "read"}` only, revoked after the call.
  Adding a project stays `POST /api/repos {"url"}`: no new endpoint.
- `DELETE /api/github/app` (admin) -> uninstalls (`DELETE /app/installations/{id}`), deletes the
  secret and rows, returns `{"delete_url": "<app settings page>"}` so the user can remove the
  registration itself (GitHub has no API for that).

Manifest sent (webhooks off, least privilege, private app):

```json
{"name": "thinair-3fa9c1", "url": "<public_url>", "public": false,
 "redirect_url": "<public_url>/api/github/callback/app",
 "setup_url": "<public_url>/api/github/callback/setup", "setup_on_update": true,
 "hook_attributes": {"url": "<public_url>/api/github/hook", "active": false},
 "default_permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
 "default_events": []}
```

`workflows: write` is deliberately not requested, so a patch that touches `.github/workflows/` is
refused at push with a clear message (open question 4).

**Agent login**

- `POST /api/agents/{id}/login` -> 201 `{"session": "<id>", "state": "preparing"}`; 409
  `login_running` returns the caller's existing session; 422 `no_in_app_login` when the agent has no
  `login_flow`.
- `GET /api/agent-logins/{sid}/events` (SSE) -> `state` events:
  `{"state": "preparing"|"link"|"code"|"verifying"|"done"|"failed"|"expired", "url": "...", "user_code": "...", "needs_code": true, "message": "..."}`.
  Replays the current state on connect; no history.
- `POST /api/agent-logins/{sid}/input` body `{"code": "..."}` (max 2,000 chars, written to the PTY
  followed by Enter) -> 202.
- `DELETE /api/agent-logins/{sid}` -> cancels and removes the container.
- `POST /api/agents/{id}/login/check` -> runs `check_command` once -> `{"logged_in": true|false|null}`.
- Rules: only the session's own user may see it (others, admins included, get 404: it is that
  person's login, in `auth_volume_name(cfg.login_id, user)`). One session per user. 10-minute
  lifetime. Container: uid 10001, cap-drop ALL, read-only rootfs, 1 CPU, 768 MiB, pids 512, only the
  login volume mounted, behind the agent's egress allowlist when it has one. `env_passthrough`
  variables are **not** passed (same as `login_command()` in `cli/main.py` today).
- A URL is rendered as a link only if it is `https://` and its host is in `login_flow.url_hosts`;
  anything else is shown as plain text.

**Push**

- `GET /api/push` -> `{"public_key": "<base64url>", "subscribed": false}`
- `POST /api/push/subscriptions` body `{"endpoint", "keys": {"p256dh", "auth"}}` -> 201. 422
  `bad_endpoint` unless the host is one of `fcm.googleapis.com`, `*.push.services.mozilla.com`,
  `*.push.apple.com`, `*.notify.windows.com` (the server will POST to this URL; an arbitrary one
  would be an SSRF primitive).
- `DELETE /api/push/subscriptions` body `{"endpoint"}` -> 204. `POST /api/push/test` -> 202.

**Setup and health**

- `GET /api/setup` -> `{"steps": [{"id": "github"|"agent"|"project"|"first_task"|"notifications", "done": true, "skipped": false, "detail": "2 repositories"}], "dismissed": false, "preferred_agent": "claude"}`.
  `done` is computed live from real state, so it can never disagree with reality.
- `POST /api/setup` body `{"skip": "github"}` | `{"dismiss": true}` | `{"preferred_agent": "claude"}`
  (the last one also starts building that agent's image in the background).
- `GET /api/health` (admin) -> `{"checks": [{"id", "status", "name", "detail", "fix", "action": {"kind": "route"|"post"|"copy", "target", "label"}|null}]}`
  from `doctor.run_checks(online=False)` in a thread. `POST /api/health/fix/images` -> 202.

**What a non-browser client needs (the contract, for a future wrapper or native app):**
pair with `respond: "token"`, then `Authorization: Bearer` on everything including SSE (already
accepted by `require_auth`); `Last-Event-ID` resume (exists); `/api/jobs/{id}/input` for approvals
(exists); push registration would need a new subscription type and a relay, which is why 2.3 says no.

### 3.5 Notifications

- `WebPushNotifier(job_id)` sends to the **job owner's** subscriptions (today every Telegram
  message goes to the owner's chat regardless of whose job it is). Same content rules as
  `notify.py`: repo, kind, outcome, first line of the task, short error or result. Never logs,
  diffs or summaries. Payloads are encrypted end to end between server and browser (RFC 8291); the
  push service sees only the endpoint and size.
- New: **attention** pushes for conversations. `JobService.attention(job_id, "approval"|"your_turn")`
  is called by the agent executor on `permission_request` and `waiting` events; at most one push per
  job per 60 s. This is what makes "Ask me before it acts" usable with the phone in a pocket (today
  an unanswered request is refused after 10 minutes without the user ever knowing).
- One payload shape for every browser: `{"web_push": 8030, "notification": {"title", "body", "navigate", "tag"}}`.
  iOS 18.4+ shows it natively (Declarative Web Push); elsewhere `sw.js`'s `push` handler reads the
  same JSON and calls `showNotification` **[verify in build]**. `notificationclick` focuses an open
  window or opens `navigate` (`/#/job/<id>`).
- Telegram is untouched and documented as the alternative for people who prefer it.

### 3.6 Installer (`install.sh`) and pairing

- New `auto_phone()` replaces the question in `phone_access()` and walks 2.2's order.
  `THINAIR_PHONE=auto|public|tailscale|funnel|domain|skip` (`auto` is the new default on first
  install; the old values behave as today).
- Detection runs **inside the already-pulled server image** (`docker compose run --rm thinair thinair net detect`)
  so the POSIX script stays small and shellcheck-clean and the logic is unit-tested in Python.
  Public IP: cloud metadata where it exposes one, else a single anonymous `GET` to
  `https://icanhazip.com` (disclosed in the output; `THINAIR_PUBLIC_IP=...` skips it).
- Public path: `set_env THINAIR_DOMAIN <a-b-c-d>.sslip.io`, `THINAIR_PUBLIC_URL`, `COMPOSE_PROFILES=https`;
  open 80/443 in the host firewall on any distro where one is active (`ufw`, `firewalld`, Oracle's
  iptables rules), printing what was changed; wait for the certificate with the Oracle message
  from 2.2 when relevant.
- **QR first.** Order becomes: start -> phone access -> print QR -> then `thinair images` and
  `thinair doctor`. The final screen is the QR (30-minute code: `thinair qr --ttl 30m --short`),
  the URL, and the short code; the admin token is no longer printed (it is in `.env`; one line says
  so). The "Next steps" list disappears: the app's Welcome screen is the next step.

  ```text
  Thinair is running at https://203-0-113-7.sslip.io

      [ QR ]            Scan with your phone's camera.

  No camera? Open the address above and enter:  K7QM-2XVD
  (Valid 30 minutes. New code: cd ~/thinair && docker compose exec thinair thinair qr)
  Anyone on the internet can reach this address; only someone with a code or token can sign in.
  Keep it private instead: re-run with THINAIR_PHONE=tailscale
  ```

- **Short codes.** 8 characters of Crockford base32 (40 bits), shown as `XXXX-XXXX`, stored as a
  SHA-256, same TTL and one-use rule as the long code. Because 40 bits is guessable in principle:
  each pairing row counts failures across all addresses and is burned after 20 wrong short codes
  while it is live (the long QR code is unaffected), on top of the existing per-IP throttle. The
  login screen gets a second field, "Code from `thinair qr`".
- Runner mode, `THINAIR_RAW_URL`, and the "body in `main()`" structure are unchanged.

### 3.7 `thinair doctor` checks

`Check(status, name, detail, fix, id="", action="")`. New or rewritten:

| id | Detail when it fails | Fix line / one-tap action |
|---|---|---|
| `phone.url` | no https address set | "Run the installer again: it sets one up" |
| `phone.dns` | `203-0-113-7.sslip.io` no longer matches this machine's address | "Your IP changed. Run the installer again, then pair your phone again" |
| `phone.reach` | port 443 not reachable from outside (hairpin probe to the address taken from the sslip name: no third party involved). Skipped when a request for the public host has been seen since start | On Oracle: "Oracle's cloud firewall blocks 443: Console > Networking > VCN > Security List > add ingress 0.0.0.0/0 TCP 80,443". Else: "Open ports 80 and 443 in your provider's firewall" |
| `phone.cert` | Caddy has no certificate yet | "It retries by itself; usually a blocked port 80" |
| `github` | modes: app connected (N installations) / token in `.env` / none | action `route:#/settings/github` "Connect GitHub" |
| `github.key` | `secrets/github-app.json` missing, unreadable, or readable by others | "Reconnect GitHub in Settings" |
| `agent.<id>` | not signed in | action `route:#/settings/agents/<id>` "Sign in" |
| `images` | sandbox images not built | action `post:/api/health/fix/images` "Prepare now" |
| `push` | no device subscribed for this user | action `route:#/settings/notifications` |
| `version` | newer release | action `copy:` the installer command (Thinair cannot update itself: it has no write access to Docker beyond sandboxes, by design) |

Still read-only, still exit 1 on any fail, still must not create the SQLite file.

### 3.8 Config keys

| Key | Default | Meaning |
|---|---|---|
| `THINAIR_PHONE` (installer) | `auto` | 2.2 |
| `THINAIR_PUBLIC_IP` (installer) | detected | Skip the lookup |
| `THINAIR_TRUSTED_PROXIES` | Compose network subnet in Compose; empty otherwise | Addresses whose `X-Forwarded-For` is believed, so the failed-login throttle counts real clients behind Caddy |
| `THINAIR_GITHUB_FOR_MEMBERS` | `false` | Clearer alias of `THINAIR_GITHUB_TOKEN_FOR_MEMBERS` (either enables it) |
| `THINAIR_WEB_PUSH` | `true` | `false` hides the feature and generates no keys |
| `THINAIR_API_DOCS` | `auth` | `auth` puts `/docs`, `/redoc`, `/openapi.json` behind login; `open` = today's behaviour; `off` |

Deprecated: none removed. `THINAIR_GITHUB_TOKEN`, `_FILE`, `THINAIR_DOMAIN`, Tailscale and all
`TELEGRAM` keys keep working and move under "Advanced" in the README and `.env.example`.

### 3.9 Funnel measurement (local only)

Events, each stored once per user with its first timestamp in `SetupEvent`: `installed` (first
migration), `paired`, `github_connected`, `agent_connected`, `project_added`, `first_job`,
`first_agent_success`, `first_pr`, `push_enabled`. `thinair stats` prints them with the time
between steps; admins see the same on Setup health.

**Privacy stance, to state in the README:** these rows never leave the server. Thinair has no
telemetry, no phone-home, and no opt-in switch for one, because there is nowhere to send it. If
aggregate numbers are ever wanted, the path is a user running `thinair stats` and pasting the
output into an issue by choice.

---

## 4. Security review

### 4.1 GitHub App private key

- **Storage:** `/data/secrets/github-app.json` (app id, slug, client id, client secret, PEM), 0600
  in a 0700 directory, owned by uid 10001, inside the `thinair-data` volume. Never in the database
  (so it is absent from SQLite copies and from an external Postgres), never in the environment,
  never logged; `repr()` of the credentials object prints `***`.
- **Who can read it:** the Thinair process and root on the host (who can already do everything).
  No sandbox can: sandboxes only get per-job workspace, auth and cache volumes, and the server's
  data volume is never passed to `VolumeMount`. Add a test that `SandboxSpec` refuses a mount of
  the volume named in `THINAIR_DATA_VOLUME`/`thinair-data` as defence in depth.
- **If stolen:** the thief can mint tokens for every repository the app is installed on, with the
  app's permissions (contents write, pull requests write), until the user deletes the app or
  generates a new key on GitHub. Same blast radius as a read/write PAT covering the same repos;
  smaller if the user chose "Only select repositories".
- **Rotation:** Settings > GitHub > Reconnect (uninstall, delete the secret, run the manifest flow
  again: a new app with a new key). Simpler and more certain than key rotation, because the app is
  disposable. Manual alternative documented: generate a key in the app's settings and replace the
  PEM in the file.
- **What gets better than today:** no long-lived GitHub credential ever enters a container or
  `docker inspect`; per-step tokens are single-repo, single-purpose, at most an hour, and revoked
  on completion; a malicious repository's clone can at worst leak a token that reads that same
  repository for the seconds before revocation.
- **What gets worse:** a GitHub secret now exists on disk by default, and backups of the data
  volume contain it. Say so plainly.
- **Callbacks:** both are open routes. They act only on a valid one-time `state` (SHA-256 stored,
  10 minutes, single use) and never trust `installation_id` from the query string. The conversion
  `code` is useless without also completing the flow from the admin's session, and it is only
  redeemed after the state check, so the routes cannot be used to make the server call GitHub at will.
- **CSP:** `form-action` gains `https://github.com`. Nothing else is loosened.

### 4.2 Relay

None is built (2.2). Tailscale Funnel, when chosen, terminates TLS on the machine itself; Tailscale
relays ciphertext.

### 4.3 Agent login passthrough

- Bytes: section 2.4. Stored nowhere; a 16 KB in-memory ring buffer per session, dropped at the end.
- **Phishing through the relay:** a hostile or buggy CLI could print any URL. Only allowlisted
  `https` hosts become tappable links.
- **Session hijack:** a session is visible only to the user who started it. Another signed-in
  user cannot read the URL or submit a code (404), so nobody can complete a login into someone
  else's volume or swap in their own account.
- **Input injection:** the pasted code is length-capped, stripped of control characters, and
  written only when the CLI is at the declared `code_in` prompt. There is no general "send keys"
  endpoint in the guided flow.
- **Resource abuse:** one session per user, 10 minutes, hard container limits, swept by the janitor.
- **Unchanged risk:** a malicious repository can still trick an agent into leaking its own login
  (already in SECURITY.md).

### 4.4 Web Push

- VAPID private key in `secrets/vapid.json`, same handling as 4.1. If stolen, the thief can send
  notifications to subscribed devices until the key is regenerated (which drops all subscriptions).
  It grants no access to Thinair or to any data.
- Payloads are end-to-end encrypted to the browser; push services see metadata only. Content rules
  are the same as Telegram's (no logs, diffs or summaries), and lock-screen text is limited to repo
  name, job kind, outcome and the task's first line.
- Endpoint allowlist prevents server-side request forgery through a crafted subscription.
- Subscriptions are tied to the API token that created them and die with it (revoking a phone
  also stops its notifications).

### 4.5 A public IP on the internet

This is the largest change to the threat model: the default install moves from "listening on
127.0.0.1" to "login page reachable by anyone".

| Threat | Today | Required before the public default ships (phase 1) |
|---|---|---|
| Token guessing | 256-bit tokens; 10 failures/min per IP | Keep. Make the per-IP count real behind Caddy (`THINAIR_TRUSTED_PROXIES`); today every client looks like Caddy's address |
| Short pairing code guessing | n/a | Per-code failure cap (3.6); codes exist only for 10-30 minutes after someone asks for one |
| QR shoulder-surfing | In SECURITY.md | Unchanged; the installer's 30-minute code is called out in the output |
| API surface discovery | `/docs` and `/openapi.json` are open | `THINAIR_API_DOCS=auth` default |
| Downgrade / cookie theft | Cookie is `Secure` when HTTPS | Add HSTS; Caddy already redirects 80 to 443 |
| Bugs in unauthenticated routes | `/healthz`, `/api/login`, `/api/pair`, static shell | New open routes: `/api/meta`, two GitHub callbacks. Each gets a test that it reveals nothing without a valid state/code; `/healthz` keeps returning only status + version |
| DoS on a 1 GB VM | Not addressed | Caddy in front absorbs slow connections; request body cap of 1 MB on JSON routes; documented as not a hardened public service |
| Sandbox escape made reachable | Requires an authenticated job | Unchanged: no unauthenticated path starts a container |

`THINAIR_PHONE=tailscale` remains the answer for anyone who wants none of this, and the installer's
last lines say so.

### 4.6 Replacement text for the security model

README "Security model", replace the GitHub bullet with:

> **GitHub:** by default Thinair connects through a GitHub App that your own server creates and
> owns. Its private key is stored on your server (`secrets/github-app.json` in the data volume,
> readable only by Thinair) and is never given to any container. For each clone or push Thinair
> asks GitHub for a token limited to that one repository and that one job (read for clone, write
> for the push), valid for at most an hour, and revokes it when the step ends. Install, test and
> agent runs never see any GitHub token. A token in `.env` (`THINAIR_GITHUB_TOKEN`) still works as
> before.

Add to README "Security model":

> **Reaching it from your phone:** the installer's default puts Thinair's login page on the public
> internet at `https://<your-ip>.sslip.io`, with a certificate from Let's Encrypt. Signing in needs
> a 256-bit token or a one-time pairing code; wrong attempts are rate-limited per address. Prefer
> it private? Install with `THINAIR_PHONE=tailscale`.
>
> **Signing an agent in from the app:** Thinair starts the agent's own login command inside the
> agent's container and shows you the sign-in link it prints. The one-time code you paste back goes
> straight to that command. The login itself is saved by the agent in its own volume, which
> Thinair never reads.

SECURITY.md, "Protected by design": replace the `THINAIR_GITHUB_TOKEN` bullet with the paragraph
above plus "Callbacks from GitHub are accepted only with a one-time state value". Add the pairing
short-code cap, the push endpoint allowlist and the login-session isolation. "Not protected": add

> - **The GitHub App key and the notification key are files on your server.** Anyone who can read
>   Thinair's data volume (root on the host, or a backup of the volume) can act as your GitHub App
>   on the repositories you installed it on until you delete the app. Reconnect GitHub in Settings
>   if in doubt.
> - **A public address is found by scanners within minutes.** Thinair's login is the only thing
>   between them and your server. Keep it updated (`thinair doctor` tells you), or use Tailscale.
> - **sslip.io is a third party.** It only answers DNS (it never sees your traffic), but if it is
>   down your address stops resolving.

---

## 5. Design

### 5.1 Visual direction

| Direction | What it looks like | For | Against |
|---|---|---|---|
| **A. Altitude** | Cool paper background, deep ink text, one teal accent, hairline (1px) dividers instead of boxes and shadows, large light-weight headings, lots of air. Status is carried by a small coloured dot plus a word, never colour alone | Fits the name literally (thin lines, air); calm next to noisy logs; cheap to build with system fonts; evolves today's teal instead of replacing it | Restraint has to be held everywhere or it reads as unfinished |
| B. Terminal | Dark-first, monospace everywhere, green/amber on black | Familiar to the audience | Looks like every other dev tool; poor for reading prose from an agent; weak in sunlight |
| C. Instrument panel | Dense cards, gauges, status lights, tabular numbers | Good for the profiler | Wrong emphasis: the core loop is a conversation and a diff, not a dashboard |

**Recommendation: A, borrowing C's tabular numerals for the profiler and diff stats.** The one
signature element is the **hairline**: lists are separated by 1px lines with generous padding, cards
are used only for things the user acts on (an approval, the result, a setup step).

### 5.2 Design tokens

All in `style.css` as custom properties; dark values under `prefers-color-scheme: dark`. No
external fonts or assets (the CSP forbids them anyway). A test in `tests/test_web.py` parses the
tokens and asserts WCAG AA contrast for every text/background pair listed here, so the palette
cannot drift below 4.5:1 (3:1 for large text and UI borders that carry meaning).

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#f5f7f9` | `#0a111c` | Page |
| `--surface` | `#ffffff` | `#111b2b` | Cards, sheets, inputs |
| `--surface-2` | `#eef2f5` | `#17243a` | Pressed rows, segmented control track |
| `--line` | `#dfe5ea` | `#233149` | Hairlines |
| `--ink` | `#0e1726` | `#e8edf3` | Text |
| `--ink-2` | `#4a5568` | `#b3bdcb` | Secondary text |
| `--ink-3` | `#5f6b7a` | `#8e9aab` | Captions, placeholders (still AA) |
| `--accent` | `#0f766e` | `#2dd4bf` | Primary buttons, links, focus ring |
| `--accent-ink` | `#ffffff` | `#04201d` | Text on accent |
| `--accent-soft` | `#d7f0ec` | `#0f2f33` | Selected chips, progress track |
| `--ok` / `--warn` / `--danger` / `--info` | `#15803d` / `#b45309` / `#dc2626` / `#1d4ed8` | `#4ade80` / `#fbbf24` / `#f87171` / `#7aa7ff` | Status dot + word |
| `--code-bg` / `--code-ink` | `#0b1220` / `#d1d5db` | `#060b13` / `#d1d5db` | Logs, diffs |
| `--add-bg` / `--del-bg` | `#e6f6ec` / `#fdecec` | `#0f2a1c` / `#34161a` | Diff line backgrounds |

Note: today's accent `#0d9488` with white text is about 3.7:1 and fails AA for button labels;
`#0f766e` is about 5.5:1. That is the reason for the shift, not taste.

- **Type:** `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`; mono
  `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`. Scale (px / line height / weight):
  display 28/34/300, title 22/28/600, heading 18/24/600, body 16/24/400, small 14/20/400,
  caption 12/16/500 (letter-spacing 0.02em, uppercase only for section labels), code 13/19.
  Inputs stay at 16px so iOS does not zoom. `font-variant-numeric: tabular-nums` on stats.
- **Spacing:** 4, 8, 12, 16, 24, 32, 48. Page gutter 16. Rows are at least 56 tall.
- **Radius:** 6 (chips), 12 (buttons, inputs, cards), 20 (bottom sheets), 999 (pills, dots).
- **Elevation:** none by default (hairlines). `--e1: 0 1px 2px rgb(14 23 38 / 6%)` for the sticky
  action bar; `--e2: 0 8px 24px rgb(14 23 38 / 14%)` for sheets. Dark mode uses a lighter surface
  and a line instead of shadows.
- **Motion:** 120ms (press feedback), 200ms (fade/slide in), 320ms (sheet), 600ms (the one
  celebration). Enter `cubic-bezier(.2, 0, 0, 1)`, exit `cubic-bezier(.4, 0, 1, 1)`. Under
  `prefers-reduced-motion: reduce` every duration is 0.01ms and transforms are removed.
- **Icons:** one sprite, `static/icons.svg`, about 18 symbols drawn on a 24px grid with a 1.5px
  round stroke and `currentColor`, used through `<use href="/static/icons.svg#name">`. A new
  `icon(name, label)` helper next to `el()` builds them with `createElementNS` (the innerHTML ban
  stays). Decorative icons get `aria-hidden="true"`; icon-only buttons get `aria-label`.
- **Touch and layout:** every target at least 44x44; the primary action of each screen sits in a
  sticky bottom bar padded by `env(safe-area-inset-bottom)`; top bar padded by
  `env(safe-area-inset-top)`; destructive actions never sit in the primary slot.
- **Loading:** skeleton rows (grey bars at the real row height), never a bare spinner; `#loading`
  becomes a skeleton of the destination view. `aria-busy="true"` on the region.
- **Optimistic UI, only where it is safe:** sent chat messages, permission answers and "skip step"
  render at once and roll back with an inline error. Starting a job, creating a PR and anything on
  GitHub wait for the server.
- **Offline:** a thin banner "Offline. Showing what was loaded." (the shell already opens from the
  service worker); actions are disabled, not hidden. No offline job cache in this plan.
- **Haptics:** `navigator.vibrate(10)` on Android for approve/deny and the first PR; feature-tested,
  absent on iOS.
- **Brand:** icon = three horizontal hairlines of decreasing length on the dark `#0a111c` ground,
  the top one teal (air thinning with altitude); redraw `icon.svg` and regenerate the PNGs with the
  existing stdlib script. Celebration, first PR only: the "Pull request opened" card rises 8px
  while three hairlines drift up and fade behind it (600ms, once, never again for that user;
  static under reduced motion). No confetti.

### 5.3 Navigation

Bottom tab bar with three tabs: **Projects** (`#/`), **Activity** (`#/activity`, all jobs),
**Settings** (`#/settings`). Job and project pages hide the tab bar and show a back chevron plus
the sticky action bar. Onboarding is `#/setup` and `#/setup/<step>`; until it is finished or
dismissed, Projects shows a "Finish setting up" card at the top.

### 5.4 Screens

Wireframes are 36 columns wide, about a 6-inch phone. `[ Label ]` is a button, `( )` a radio,
`·` a status dot.

#### O1. Welcome (`#/setup`)

Purpose: confirm the phone is connected, set expectations, learn the agent. Primary: Continue.

```text
+------------------------------------+
|                                    |
|  ≡  (three hairlines)              |
|                                    |
|  You're connected.                 |
|  Four short steps and your phone   |
|  can hand work to a coding agent.  |
|                                    |
|  Which agent do you use?           |
|  (•) Claude Code                   |
|  ( ) Codex                         |
|  ( ) Something else                |
|                                    |
|  1  Connect GitHub                 |
|  2  Sign in to your agent          |
|  3  Pick a repository              |
|  4  Try a first task               |
|                                    |
|  About 3 minutes. Skip anything.   |
+------------------------------------+
| [          Continue              ] |
+------------------------------------+
```

- Choosing an agent posts `preferred_agent` (starts its image build). "Something else" shows
  "Add agents later in Settings. You can still connect GitHub now."
- States: *error* (pairing failed) is the login screen with "This code has expired or was already
  used. Run `thinair qr` on the server for a new one." *Member* (not admin): step 1 is replaced by
  "GitHub is managed by this server's admin."

#### O2. Connect GitHub (`#/setup/github`)

Purpose: get repository access with no token. Primary: Connect GitHub.

```text
+------------------------------------+
| ‹ Setup                  1 of 4    |
| ━━━━━━━━━───────────────────────   |
|                                    |
|  Connect GitHub                    |
|  Thinair creates its own private   |
|  GitHub App on your account. You   |
|  choose which repositories it      |
|  can see.                          |
|                                    |
|  · No token to copy                |
|  · Access only to repos you pick   |
|  · Each job gets a one-hour key    |
|    for one repository              |
|                                    |
|  What happens next                 |
|  GitHub asks you to tap "Create    |
|  GitHub App", then to pick         |
|  repositories and tap "Install".   |
|                                    |
|  Skip for now                      |
+------------------------------------+
| [        Connect GitHub          ] |
+------------------------------------+
```

- *Returning from GitHub:* "Connected as **octocat** · 3 repositories" with a green dot;
  primary becomes Continue; link "Change repositories on GitHub" (the installation's `manage_url`).
- *Created but not installed* (user backed out): "Almost there. Thinair's app exists but isn't
  installed on any repository yet." Primary: Choose repositories.
- *Error:* "GitHub didn't finish. Nothing was changed. Try again." *Token mode:* "You're using a
  token from .env. That works. Connect the app for short-lived keys." *Skipped:* public
  repositories still work by URL; PRs are unavailable.

#### O3. Connect your agent (`#/setup/agent`)

Purpose: sign the agent in without a terminal. Primary changes with the state.

```text
+------------------------------------+
| ‹ Setup                  2 of 4    |
| ━━━━━━━━━━━━━━━━━───────────────   |
|                                    |
|  Sign in to Claude Code            |
|  Your login stays inside Claude    |
|  Code's own storage on your        |
|  server. Thinair never sees it.    |
|                                    |
|  1  Open Claude's sign-in page     |
|     [ Open sign-in page  ↗ ]       |
|                                    |
|  2  Paste the code it gives you    |
|     +--------------------------+   |
|     |                    Paste |   |
|     +--------------------------+   |
|                                    |
|  Use a different agent             |
|  Skip for now                      |
+------------------------------------+
| [           Finish sign-in       ] |
+------------------------------------+
```

- *Preparing* (image build or container start): skeleton for step 1 and "Getting Claude Code
  ready. About a minute the first time."
- *Codex variant:* step 1 "Open OpenAI's device page" and step 2 shows the code large, with Copy:
  "Enter this code there: **ABCD-1234**". No input box; the screen completes by itself. A note:
  "First time? Turn on device code login in your ChatGPT security settings."
- *Verifying:* "Checking the login..." *Done:* "Claude Code is signed in." Primary: Continue.
- *Failed:* "That code wasn't accepted. Codes expire quickly; open the sign-in page again."
  *Expired* (10 min): "This sign-in timed out. Start again." *No in-app login* (Gemini, registry
  agents): the exact `thinair auth <id> --print-command` line with Copy.

#### O4. Pick a repository (`#/setup/project`)

Purpose: add the first project with one tap. Primary: tap a row.

```text
+------------------------------------+
| ‹ Setup                  3 of 4    |
| ━━━━━━━━━━━━━━━━━━━━━━━━━───────   |
|                                    |
|  Pick a repository                 |
|  +------------------------------+  |
|  | Search                       |  |
|  +------------------------------+  |
|                                    |
|  octocat/api               🔒      |
|  Python · pushed 2 h ago           |
|  --------------------------------  |
|  octocat/website                   |
|  TypeScript · pushed yesterday     |
|  --------------------------------  |
|  octocat/dotfiles                  |
|  Shell · pushed 3 weeks ago        |
|  --------------------------------  |
|                                    |
|  Don't see it?                     |
|  Add repositories on GitHub  ↗     |
|  Paste a public repository URL     |
+------------------------------------+
```

- *Empty* (app installed on zero repos): "Thinair's GitHub App can't see any repositories yet."
  [ Choose repositories on GitHub ]. *GitHub skipped:* only the URL field, with "Public
  repositories work without connecting GitHub." *Loading:* five skeleton rows. *Error:* "Couldn't
  reach GitHub. Try again." with Retry.

#### O5. Try a first task (`#/setup/first-task`)

Purpose: a low-risk first run that visibly ends in a PR. Primary: Start.

```text
+------------------------------------+
| ‹ Setup                  4 of 4    |
| ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   |
|                                    |
|  Try a first task                  |
|  on octocat/api                    |
|                                    |
|  +------------------------------+  |
|  | Add a short "Project         |  |
|  | structure" section to the    |  |
|  | README that explains what    |  |
|  | each top-level folder is     |  |
|  | for. Don't change any code.  |  |
|  +------------------------------+  |
|  Or:  [ Fix typos in the docs ]    |
|       [ Write my own ]             |
|                                    |
|  The agent works on a copy.        |
|  Nothing reaches GitHub until      |
|  you tap Create pull request.      |
+------------------------------------+
| [   Start with Claude Code       ] |
+------------------------------------+
```

- Starting it marks the setup complete and opens the live job. *Agent not signed in:* primary
  becomes "Sign in to Claude Code first". *No agent at all:* offers a resource profile instead
  ("See the smallest machine this project's tests pass on").

#### O6. Turn on notifications (banner on the first running job, and Settings)

Purpose: replace Telegram; reach the user when a job ends or needs an answer.

```text
+------------------------------------+
|  Get told when it's done           |
|  One notification when a job       |
|  finishes or needs your answer.    |
|  [ Turn on ]         Not now       |
+------------------------------------+
```

- *iOS in Safari* (push needs the Home Screen app): tapping Turn on opens a sheet: "Add Thinair to
  your Home Screen first. 1 Tap Share. 2 Tap Add to Home Screen. 3 Open Thinair from there and
  turn notifications on." *Android/desktop:* the browser prompt, then "On. You'll hear from
  Thinair when a job ends." plus [ Send a test ]. *Denied:* "Notifications are blocked for this
  site. Allow them in your browser's site settings." *Not supported:* the banner never appears.

#### C1. Projects (`#/`)

Purpose: get to a project or see what is running. Primary: New task.

```text
+------------------------------------+
|  Projects                          |
|                                    |
|  +------------------------------+  |
|  | Finish setting up     2 of 4 |  |
|  | Next: sign in to your agent  |  |
|  +------------------------------+  |
|                                    |
|  api                    · Running  |
|  Fix the flaky date test · 2 min   |
|  --------------------------------  |
|  website                   · Done  |
|  Update footer links · yesterday   |
|  --------------------------------  |
|  dotfiles                          |
|  No jobs yet                       |
|  --------------------------------  |
|  + Add a project                   |
|                                    |
+------------------------------------+
| [          New task              ] |
+------------------------------------+
|  Projects    Activity    Settings  |
+------------------------------------+
```

- *Empty:* "No projects yet. A project is a GitHub repository Thinair can work on."
  [ Add your first project ]. *Loading:* skeleton rows. *Error:* "Can't reach your server." with
  Retry and the last successful load time. *Offline:* banner, list as last loaded.
- "New task" opens the composer for the most recently used project, with the project switchable.

#### C2. New task composer (`#/repo/<id>/new`, also the bottom of the project page)

Purpose: describe the work and start. Primary: Start.

```text
+------------------------------------+
| ‹ api                              |
|                                    |
|  What should the agent do?         |
|  +------------------------------+  |
|  | Fix the failing tests in     |  |
|  | tests/test_dates.py          |  |
|  |                              |  |
|  +------------------------------+  |
|                                    |
|  Agent          Claude Code (ACP) ›|
|  --------------------------------  |
|  Branch                    main   ›|
|  --------------------------------  |
|  Let me reply while it works   [x] |
|  Ask before it acts            [ ] |
|  --------------------------------  |
|  Measure resources instead        ›|
+------------------------------------+
| [   Start with Claude Code       ] |
+------------------------------------+
```

- The two toggles are today's `#opt-chat` / `#opt-ask`, disabled with the existing explanation for
  one-shot agents. "Measure resources instead" replaces the Agent/Profile tabs (profile is the
  secondary job). *Agent not signed in:* inline row "Claude Code isn't signed in. Sign in" (to the
  O3 screen, returning here with the text kept). *429:* the server's `too_many_jobs` message.
  *Draft:* the text is kept in `localStorage` per project until sent.
- Android share target: sharing a GitHub URL to Thinair opens this screen with the repository
  resolved (added if new) and any shared text in the box.

#### C3. Live job (`#/job/<id>`)

Purpose: follow the agent, answer it, see the outcome. Primary depends on state.

```text
+------------------------------------+
| ‹ api                   · Running  |
|  Fix the failing tests in tests/…  |
|  Claude Code · main · 2 min        |
|  --------------------------------  |
|  I'll start by running the test    |
|  suite to see what fails.          |
|                                    |
|  › Run  pytest -q tests/test_da…   |
|  › Edit tests/test_dates.py        |
|                                    |
|  The test assumed the local        |
|  timezone. I've pinned it to UTC.  |
|                                    |
|            also update the README ◂|
|                                    |
|  ▸ Log (214 lines)                 |
+------------------------------------+
| +----------------------------+ [↑] |
| | Reply to the agent         |     |
| +----------------------------+     |
|  Stop this step         Finish     |
+------------------------------------+
```

- Agent text is prose; tool calls are one-line mono rows (tap to expand the full command); the
  raw log is collapsed by default for agent jobs and open for profile jobs. New content
  auto-scrolls only when the user is at the bottom; otherwise a "New messages ↓" pill appears.
- *Queued:* "Waiting for a free slot (1 job ahead)." *Starting:* skeleton with "Cloning,
  installing dependencies..." fed by the `[thinair]` system lines. *Waiting for you:* state line
  "Your turn" in `--warn`, composer focused. *Finished:* composer replaced by the result card
  (C5/C6 entry) and [ Review changes ]. *Failed:* the `error_message` in a card with
  [ Run again ]; auth errors link to O3. *Disconnected:* "Reconnecting..." (EventSource resumes
  with `Last-Event-ID`). *Not conversational:* no composer; Cancel sits in an overflow menu.

#### C4. Approval card (inside C3)

Purpose: a decision the user can make in two seconds. Primary: the agent's own allow option.

```text
+------------------------------------+
|  Allow this?                       |
|  Run a command                     |
|  +------------------------------+  |
|  | rm -rf build/ && make clean  |  |
|  +------------------------------+  |
|  [    Allow once    ] [  Deny   ]  |
|  Always allow commands like this   |
|  Waiting 8:40 before it's refused  |
+------------------------------------+
```

- Buttons are the agent's options, as today (`appendPermissionCard`); the countdown is new and
  comes from `THINAIR_AGENT_IDLE_S`. Answered: the card collapses to one line ("Allowed once · you").
  Timed out: "Refused: no answer in 10 minutes." While a card is open the sticky bar shows it too,
  so the answer is reachable without scrolling. Arrives as a push when the app is closed (3.5).
  Screen readers: `role="alertdialog"`, focus moves to the card.

#### C5. Diff review (`#/job/<id>/changes`)

Purpose: decide whether the change is good, on a small screen. Primary: Create pull request.

```text
+------------------------------------+
| ‹ Job            3 files  +24  −7  |
|  --------------------------------  |
|  ▾ tests/test_dates.py    +12  −5  |
|  @@ def test_parse_iso            |
|   12   def test_parse_iso():      |
|   13 -     d = parse("2024-01-01")|
|   13 +     d = parse("2024-01-01",|
|      +               tz=UTC)      |
|   14       assert d.year == 2024  |
|  --------------------------------  |
|  ▸ README.md               +9  −0  |
|  ▸ src/dates.py            +3  −2  |
|                                    |
|                       [ ↑ ] [ ↓ ]  |
+------------------------------------+
| [ Create pull request ]   Patch ⤓  |
+------------------------------------+
```

- **Wrap, don't scroll sideways:** lines wrap with a hanging indent so continuation lines sit
  under the code, not under the line number; a toggle in the header switches to horizontal scroll
  for people who prefer it (remembered). One line-number column (new side; old number for removed
  lines). Added/removed lines carry `+`/`-` *and* a background, so colour is never the only signal.
- **Per-file collapse:** each file is a `<details>`; the first file opens, files over 400 changed
  lines and binary files start closed ("Binary file changed" / "Large change: 1,240 lines. Show").
  File headers are sticky while their file scrolls.
- **Jump to next/previous change:** the two floating buttons move between hunks across files,
  opening a file when needed.
- Parsed client-side from the existing `diff` string (`diff --git` splits files, `@@` splits
  hunks); no API change. Rendering is lazy per file, so a 5,000-line diff does not build 5,000 nodes
  up front. *No changes:* "The agent didn't change any files." with its summary. *Truncated:*
  "This diff is long; the rest is in the patch." [ Download patch ]. *GitHub not connected:*
  primary becomes "Connect GitHub to open a pull request"; Patch stays.

#### C6. Pull request created

```text
+------------------------------------+
|                                    |
|        ≡  (lines rise, once)       |
|                                    |
|  Pull request opened               |
|  Fix the failing tests in          |
|  tests/test_dates.py               |
|  octocat/api #128 · 3 files        |
|                                    |
|  [     View on GitHub  ↗     ]     |
|  Start another task                |
+------------------------------------+
```

- *In progress:* "Pushing the branch and opening the pull request..." with the pr job's log
  behind a disclosure. *Failed:* the pr job's `error_message` and [ Try again ]. *First ever:* the
  celebration plays; every later time the card simply appears.

#### C7. Settings (`#/settings`)

```text
+------------------------------------+
|  Settings                          |
|                                    |
|  GitHub                octocat  · ›|
|  Agents            Claude Code  · ›|
|  Notifications     This phone   · ›|
|  --------------------------------  |
|  Setup health        1 warning    ›|
|  Devices                    2     ›|
|  People                     3     ›|
|  --------------------------------  |
|  Thinair 0.3.0                     |
|  https://203-0-113-7.sslip.io      |
|  Log out                           |
+------------------------------------+
|  Projects    Activity    Settings  |
+------------------------------------+
```

- GitHub: O2's connected state plus "Reconnect" and "Disconnect" (confirm: "Thinair will lose
  access to your repositories. Open pull requests stay."). Agents: one row per agent with signed-in
  state, tap for O3. Devices: this user's tokens with last-used time and Revoke (uses the existing
  token model; "Add a phone" shows a pairing QR drawn by the server as an SVG path, admin or self).
  People: admins only, the existing `/api/users` actions.

#### C8. Setup health (`#/settings/health`)

Purpose: `thinair doctor` for the phone. Primary: the first fixable problem's action.

```text
+------------------------------------+
| ‹ Settings                         |
|  Setup health                      |
|  Working, with 1 suggestion.       |
|  --------------------------------  |
|  · Phone access                 ok |
|    https://203-0-113-7.sslip.io    |
|  --------------------------------  |
|  · GitHub                       ok |
|    App connected, 3 repositories   |
|  --------------------------------  |
|  · Sandbox images             warn |
|    Not prepared yet: node          |
|    [ Prepare now ]                 |
|  --------------------------------  |
|  · Agent logins                 ok |
|  · Docker 27.3, 2 CPU, 1.9 GiB  ok |
|  · Disk 31.2 GiB free           ok |
|  · Version 0.3.0, up to date    ok |
|  --------------------------------  |
|  First run: paired 4 min after     |
|  install · first PR after 11 min   |
+------------------------------------+
```

- Rows come straight from `/api/health`; status is a dot plus the word. Problems sort first.
  *Loading:* skeleton rows (the checks take a second or two). *Members:* only the checks about
  themselves (agent logins, notifications).

Accessibility across all screens: visible focus ring (`2px solid var(--accent)`, offset 2);
headings in order; live regions for the job status (`aria-live="polite"`) and errors
(`role="alert"`, as today); every form control labelled; the tab bar is a `<nav>` with
`aria-current="page"`.

---

## 6. Phased roadmap

Ordered by which drop-off each phase removes. Every phase ends with `uv run ruff check . &&
uv run ruff format --check .`, `uv run pytest -m "not docker"` and the listed Docker tests passing,
a CLAUDE.md section update, and the manual script below. Commits and PRs follow CLAUDE.md
(conventional commits, no attribution trailers).

### Phase 1: Reach it (L)

*Drop-off removed:* the Tailscale/domain homework, the largest one. *After it ships:* one command
on a public-IP VM ends in a QR code; scanning it opens Thinair over HTTPS, signed in, on a Welcome
screen with a checklist (later steps still show today's CLI instructions, with Copy buttons).

- Scope: `netinfo.py`; `thinair net detect`; `install.sh` `auto_phone()`, firewall opening, Oracle
  wait, QR-first ordering, new final screen; short pairing codes; `THINAIR_TRUSTED_PROXIES`;
  `THINAIR_API_DOCS`; HSTS; `/api/meta`; `setup_state.py` + migration 0011 + `/api/setup`;
  `doctor` phone checks; design tokens, icon sprite, `icon()` helper, Welcome + checklist screens;
  README/SECURITY/oracle.md rewrites for the new default.
- Files: `install.sh`, `docker-compose.yml`, `src/thinair/{netinfo,setup_state,pairing,doctor,config}.py`,
  `api/{app,auth,web,setup_api}.py`, `cli/main.py`, `db/models.py`, migration, `web/static/*`,
  `docs/oracle.md`, `README.md`, `SECURITY.md`, `.env.example`.
- Tests: `test_netinfo.py` (address classification, sslip names, fake metadata); installer runs in
  `docker:cli` as today with a fake `thinair net detect` for each branch (public, Oracle-blocked
  then opened, NAT, Tailscale present, existing config untouched); short-code cap and throttle in
  `test_limits_and_pairing.py`; real-client-IP throttle behind a trusted proxy and spoofed
  `X-Forwarded-For` from an untrusted one in `test_auth.py` (against a live uvicorn, per the
  TestClient gotcha); `/docs` requires auth; contrast test; browser smoke adds Welcome.
- Verify manually: fresh Ubuntu VM with a public IP: run the one-liner, time it to the QR, scan,
  land on Welcome. Re-run the installer: nothing changes, no re-pair needed. On an existing
  Tailscale install: upgrade leaves the URL as it was. On Oracle (first real Oracle test): confirm
  the waiting message appears and clears when the ingress rule is added; record the hairpin result.

### Phase 2: Sign in to the agent from the phone (M)

*Drop-off removed:* SSH + `docker run -it` + `/login`. *After:* Settings > Agents and setup step 2
sign Claude Code and Codex in with a link and a code.

- Scope: `SandboxSpec.tty`, `SandboxManager.open_terminal`; `agents/login.py`; `login_flow` in
  `agents/config.py` and the two built-in YAMLs; `login_api.py`; O3 screen; agent `login` field;
  docs/agents.md "In-app login" section with what the real CLIs print in a PTY.
- First task of the phase (it can change the design): run `claude auth login` and
  `codex login --device-auth` in a PTY in their `-test` images with no account and record the exact
  output, the prompt text, whether the URL carries `code_challenge`, and which hosts are contacted.
  If `claude auth login` turns out to need full-screen rendering, add `pyte` (pure Python screen
  emulator, with a why-comment) and match on the rendered screen instead of the byte stream.
- Tests: `test_agent_login.py` with a fake login program (prints a URL, waits for a code, writes a
  file into the volume) through the real PTY path in Docker; state machine unit tests with canned
  byte streams including ANSI noise and split chunks; isolation (another user gets 404; admin too);
  timeout and cancel remove the container; a non-allowlisted URL is not rendered as a link; nothing
  from the session appears in `JobEvent` or the log (asserted). Real CLIs: `-test` ids only, up to
  the point where the link appears; **never a real account** (CLAUDE.md).
- Verify manually (owner, real account): sign Claude Code in from the phone, run a task. Same for
  Codex. Start a login, close the app, confirm the container is gone after 10 minutes.

### Phase 3: Connect GitHub (L)

*Drop-off removed:* PAT creation, `.env`, restart, and URL pasting. *After:* one-tap connect, a
repository picker, per-job tokens.

- Scope: `cryptography`; `secrets.py`; `github_app.py`; `GitHubAccess` and the `TOKEN_RE` fix;
  `prepare.py`, `pr.py`, `service.py`, `executors.py` rewiring; `github_api.py`; migration 0010;
  CSP `form-action`; O2, O4, Settings > GitHub; doctor checks; `thinair github status|disconnect`;
  new `docs/github.md` (seeded from the appendix here, in the style of docs/agents.md); README
  "Private repositories" rewritten around Connect, PAT moved to Advanced.
- Tests: `test_github_app.py` with a fake GitHub (requests session): manifest shape, state
  required before any outbound call, conversion, secret file mode, JWT claims verified with the
  public key, mint body (`repositories`, `permissions`) per need, revoke in `finally` even when the
  clone fails, resolution order app > PAT > anonymous, members refused without the setting, 520-char
  dotted token accepted and masked in output, CR/LF token still refused. `test_pr.py` extended: push
  token is `contents:write` only and the PR token never appears in any `SandboxSpec`. Docker: clone
  with a fake app token fails with the App-specific hint.
- Verify manually (owner): connect on a server reachable only over Tailscale, and on an sslip.io
  server; pick a private repo; run the starter task; open the PR; confirm it is authored by the bot
  and CI runs; confirm on GitHub that no long-lived token exists; disconnect and reconnect; an
  install with only `THINAIR_GITHUB_TOKEN` behaves exactly as before the upgrade.

### Phase 4: The first run, end to end (M)

*Drop-off removed:* "now what?" between the pieces. *After:* scan-to-first-PR is one guided flow,
and Setup health replaces SSHing in to run doctor.

- Scope: O1-O5 wired into one resumable flow with skip/resume and the progress bar; starter tasks;
  "Finish setting up" card; `/api/health` + C8 with one-tap fixes; background image build;
  funnel events at every step + `thinair stats`; empty states for Projects and the project page.
- Tests: `/api/setup` step computation for owner/admin/member across states; skip and dismiss
  persistence; health actions; browser smoke walks the whole flow with fake executors, a fake
  GitHub and a fake login agent; funnel rows written once.
- Verify manually: a stopwatch run on a fresh VM by someone who has not seen the product, phone
  only after the first command. Record actions and time per step from `thinair stats`; that table
  replaces the estimates in section 1.

### Phase 5: Notifications and install (M)

*Drop-off removed:* the Telegram setup (BotFather, chat id, `.env`). *After:* a push when a job
ends or an approval is waiting; the app installs from a prompt.

- Scope: `push.py`; migration 0012; push routes; `sw.js` `push`/`notificationclick`; O6 banner,
  iOS Add-to-Home-Screen sheet, Android install button (`beforeinstallprompt`); `attention` hook;
  manifest `id` and `share_target`; per-user routing; README "Notifications" rewritten, Telegram
  kept as an alternative.
- Tests: RFC 8291 encryption against the RFC's test vector; VAPID JWT verified; endpoint
  allowlist; 404/410 deletes the subscription; revoking a token deletes its subscriptions; a broken
  push never delays a job (as for Telegram); attention debounce; Telegram output unchanged.
- Verify manually: Android Chrome and iOS (Home Screen app, 16.4+ and 18.4+): job-finished push,
  approval push, tap opens the job; iOS login carries over at Add to Home Screen (if not, the
  short-code path works); share a GitHub URL into the app on Android.

### Phase 6: The core loop, redesigned (L)

*Drop-off removed:* none at onboarding; this is retention and the "top-notch" bar. *After:* tab
bar, composer, live job, approval card, diff review and PR screens match section 5.

- Scope: C1-C7 in `index.html`/`app.js`/`style.css`; diff parser and lazy renderer; celebration;
  Devices screen; removal of the old tabs and inline styles of meaning. `app.js` is split into a
  few plain script files loaded with `defer` (`api.js`, `views-*.js`); still no build step.
- Tests: `test_web.py` (innerHTML ban covers every new file; CSP unchanged); diff parser unit
  tests run under Node when available (like `test_acp.py`) with CRLF, binary, rename, no-newline
  and 5,000-line fixtures; browser smoke grows to cover wrap toggle, collapse, jump, reduced motion,
  dark mode, 320px width.
- Verify manually: review a real 30-file diff one-handed on a 6-inch phone; VoiceOver and TalkBack
  pass over C3-C5; airplane mode shows the offline banner.

### Phase 7: Several people, and leftovers (M)

- Scope: members' own installations (public app switch, OAuth verification with the stored client
  secret, `GitHubInstallation.user_id`), `installations_for(user)`; in-app raw terminal login for
  agents without a `login_flow` (Gemini, registry agents) if phase 2's findings justify it;
  optional Android TWA in `apps/android/`; Let's Encrypt IP certificates if Caddy supports them.
- Tests: a member cannot claim an installation their GitHub user does not have; cross-user token
  resolution matrix; existing `test_users.py` guarantees unchanged.
- Verify manually: two real GitHub accounts on one server; each sees only their own repositories.

---

## 7. Risks and open questions

Risks the plan carries:

| Risk | Why it matters | Mitigation |
|---|---|---|
| `claude auth login` output in a PTY is undocumented | Phase 2's guided flow depends on recognising a URL and a prompt, and a CLI update can change them | Verify first (phase 2, task 1); pin the CLI version as today; on a parse failure show the terminal command instead of a dead screen; a Docker test against the pinned CLI catches drift on upgrade |
| sslip.io availability and its shared Let's Encrypt quota | The default URL depends on both | ZeroSSL fallback in Caddy; doctor names the cause; IP certificates as the planned replacement |
| Hairpin probe on Oracle is unverified | The "waiting for ports" UX depends on it | Fall back to watching Caddy's log; first real Oracle run is in phase 1's manual script |
| iOS cookie copy at Add to Home Screen is from community sources | If wrong, iOS users must sign in again after installing | Short-code entry on the login screen works regardless |
| Public exposure by default | A bug in an unauthenticated route becomes internet-reachable | 4.5; the installer states the exposure and the private alternative |
| GitHub manifest flow details not in the docs (whether `hook_attributes` may be inactive with an unreachable URL; whether `state` is echoed to the setup URL) | Could force small changes | The design depends on neither: webhooks are never delivered, and the setup callback re-syncs from the API |
| `cryptography` on the server image | New compiled dependency | Wheels exist for common arm64/x86 Linux targets; confirm in phase 3 before merging |
| Phase 6 touches every screen | Regression risk in a 900-line `app.js` | It lands after the smoke test covers the whole flow (phase 4) |

Questions only the owner can decide. Each has the default the plan assumes; none blocks phase 1.

1. **Is exposing the login page to the internet by default acceptable?** Default assumed: yes, on
   machines with a public IP, stated plainly in the installer output, with `THINAIR_PHONE=tailscale`
   as the private path. The alternative is one yes/no question at install, which costs a step.
2. **One anonymous request to `icanhazip.com` during install** (only when cloud metadata has no
   answer). Default assumed: acceptable and disclosed. Alternative: ask the user to type the IP.
3. **Tailscale Funnel as the NAT fallback** (public link, nothing on the phone) rather than private
   Serve (phone needs the app). Default assumed: Funnel first, Serve second.
4. **Should the GitHub App request `workflows: write`?** Default assumed: no; patches touching
   `.github/workflows/` cannot be pushed and say why. Yes makes those PRs work at the cost of a
   more powerful app.
5. **PRs authored by `thinair-xxxx[bot]` instead of you.** Default assumed: fine, and useful under
   branch protection. If PRs must come from your own account, the PAT path remains.
6. **Admin token no longer printed at the end of the install** (it stays in `.env`). Default
   assumed: yes, the QR and short code replace it.
7. **Gemini CLI gets no in-app login until phase 7.** Default assumed: acceptable given Google's
   move to Antigravity CLI. If Gemini matters more than Codex to you, swap their priority.
8. **Remote runners and private repositories:** runners keep using their own PAT; the server does
   not send app tokens to runners. Default assumed: unchanged until someone needs it.
9. **Spelling:** "Thinair" (code and UI today) or "ThinAir" (the repo name and your brief)? The
   plan keeps "Thinair"; a rename is a find-and-replace best done in phase 6.
10. **Dropping the Agent/Profile tabs** in favour of "Measure resources instead" inside the
    composer makes profiling a secondary feature. Default assumed: yes, since the product story is
    now agent-first.

---

## Appendix: facts checked on 2026-11-14

To be moved into `docs/github.md` (phase 3) and docs/agents.md (phase 2) when built.

**GitHub** ([manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest),
[setup URL](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url),
[REST: apps](https://docs.github.com/en/rest/apps/apps),
[REST: installations](https://docs.github.com/en/rest/apps/installations),
[JWT](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app),
[rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api),
[user access tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app),
[choosing permissions](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app),
[token format notice](https://github.blog/changelog/2026-04-24-notice-about-upcoming-new-format-for-github-app-installation-tokens/))

- Manifest: form `POST` to `https://github.com/settings/apps/new?state=...` (organizations:
  `/organizations/ORG/settings/apps/new`), field `manifest` (JSON string). GitHub redirects to
  `redirect_url?code=...&state=...`. Exchange with `POST /app-manifests/{code}/conversions`; all
  three steps must finish within one hour. Response includes `id`, `slug`, `client_id`,
  `client_secret`, `webhook_secret`, `pem`, `owner`, `html_url`.
- Manifest fields: `name`, `url` (required), `hook_attributes` (`url` required inside it, `active`
  default true), `redirect_url`, `callback_urls` (up to 10), `setup_url`, `description`, `public`,
  `default_events`, `default_permissions`, `request_oauth_on_install`, `setup_on_update`. No field
  for device flow or token expiry. The docs do not say URLs must be public or HTTPS.
- Setup URL receives `installation_id`; "Bad actors can hit this URL with a spoofed
  `installation_id`", so verify with a user access token.
- `POST /app/installations/{id}/access_tokens` (app JWT): body `repositories` or `repository_ids`
  (up to 500) and `permissions`; cannot exceed what the installation has; tokens expire after one
  hour. `DELETE /installation/token` revokes the calling token. `GET /repos/{owner}/{repo}/installation`
  and `GET /app/installations` need the JWT; `GET /installation/repositories` needs an installation
  token (`per_page` max 100).
- JWT: RS256; `iat` 60 s in the past; `exp` at most 10 minutes ahead; `iss` = client id (recommended)
  or app id.
- Rate limits: installation tokens 5,000/hour, plus 50 per repository above 20, capped at 12,500;
  unauthenticated 60/hour per IP; at most 80 content-creating requests/minute and 500/hour.
- Permissions: Contents (read to clone, write to push); Workflows (write) to change workflow
  files. Git over HTTPS uses `x-access-token` as the username (what `git_auth_env` already sends).
  Pull request creation needs Pull requests (write) and Metadata (read) is granted to every app:
  standard, but **not confirmed on the pages fetched**; check the create-PR endpoint page in phase 3.
- Device flow must be enabled in the app's settings and needs no client secret; user tokens last
  8 hours, refresh tokens 6 months, and reach only what both the user and the app can.
- Installation tokens may be `ghs_<appid>_<JWT>`, about 520 characters with dots, since
  2026-04-27; do not assume 40 characters.

**Phone access** ([Cloudflare quick tunnels](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/),
[Tailscale Funnel](https://tailscale.com/kb/1223/funnel), [Tailscale Serve](https://tailscale.com/kb/1312/serve),
[Let's Encrypt 6-day and IP certificates](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability.html),
[Caddy IP certificate report](https://github.com/caddyserver/caddy/issues/7399),
[sslip.io quota issue](https://github.com/cunnie/sslip.io/issues/108), [sslip.io](https://sslip.io/))

- Quick tunnels: no account; hostname changes each time; 200 in-flight requests; "do not support
  Server-Sent Events (SSE)"; "for testing and development"; no uptime guarantee.
- Named tunnels: Cloudflare dashboard, and a site on Cloudflare to publish a hostname.
- Funnel: needs MagicDNS, HTTPS certificates and the funnel node attribute; ports 443, 8443, 10000;
  visitors need no Tailscale; bandwidth-limited; the CLI opens a web page to approve enabling it.
  Serve needs HTTPS certificates and the CLI can prompt to enable them.
- Let's Encrypt IP certificates are generally available, short-lived profile only; Caddy support
  unconfirmed.
- sslip.io has hit its shared Let's Encrypt limit before and had it raised; whether it is on the
  Public Suffix List was not confirmed.

**Web Push on iOS** ([WebKit: Web Push for web apps](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/),
[WebKit: Declarative Web Push](https://webkit.org/?p=16535),
[Home Screen storage isolation](https://openpwa.net/reference/storage/home-screen-storage-isolation/))

- iOS/iPadOS 16.4+, Home Screen web apps only, permission only from a user gesture. Declarative
  Web Push since iOS 18.4. Home Screen apps have their own storage; the cookie copy at install time
  (iOS 17.2+) comes from third-party write-ups, not Apple's documentation.

**Agent logins** ([Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference),
[Claude Code authentication](https://code.claude.com/docs/en/authentication),
[Codex authentication](https://developers.openai.com/codex/auth),
[Gemini CLI authentication](https://google-gemini.github.io/gemini-cli/docs/get-started/authentication.html))

- Claude Code: `claude auth login` (`--email`, `--sso`, `--console`), `claude auth status` (exit 0
  = logged in), `claude setup-token` (one-year token, printed, not saved, for
  `CLAUDE_CODE_OAUTH_TOKEN`). With no redirect the browser shows a code to paste at "Paste code
  here if prompted". Credentials: `.credentials.json` under `CLAUDE_CONFIG_DIR`, mode 0600. The
  docs do not describe the exact headless output.
- Codex: `codex login --device-auth` (beta) for headless machines; device code login must be
  enabled in ChatGPT security settings or by a workspace admin; `codex login status`; credentials
  in `auth.json` under `CODEX_HOME` (or the OS keyring).
- Gemini CLI: Google sign-in "requires a web browser on a machine that can communicate with the
  terminal"; headless use needs a cached login or `GEMINI_API_KEY`; `.env` is read from
  `~/.gemini/.env`. `NO_BROWSER` is not on that page (it comes from the CLI's own messages, per
  docs/agents.md).

---

## Progress log

### Phase 1: Reach it (2026-11-14, branch `onboarding-v2/phase-1-reach`)

Scope as amended by the owner's build brief: phone reachability (sslip.io + Caddy, installer
detection, doctor checks) plus the GitHub token-format fix, which shipped first as its own commit.

Shipped:
- **Token format** (`github.py`): tokens are opaque, `[\x21-\x7e]{20,4096}` (no whitespace, CR,
  LF or control characters, so no header injection), tested with a realistic 520-character
  stateless `ghs_<id>_<JWT>` token and a 4096-character one. **Found and fixed while doing it:**
  `_stream_logs` cut lines to `MAX_LINE_CHARS` *before* masking secrets, so a token across the cut
  leaked its first characters; masking now comes first (regression test fails on the old code).
- `netinfo.py` + `thinair net detect | https-check | firewall-hint`.
- Installer: `THINAIR_PHONE=auto` (new default) detects Tailscale-with-HTTPS, then a public IP
  (sslip.io + Caddy, waits for a *verified* certificate through `caddy:443`), and asks only when
  neither works (Funnel / Tailscale / domain / skip). New `public` and `funnel` choices. Re-runs
  follow a changed public IP. QR code last (`thinair qr --ttl 30`), images built in the
  background, doctor's report shown only on failures, one line about public exposure.
- Doctor: `Public address` (IP changed) and `HTTPS` (certificate, with a per-cloud firewall hint).
- Server: one-shot startup check for a changed IP (no polling); `THINAIR_TRUSTED_PROXIES` so the
  login throttle counts real clients behind Caddy; HSTS without `includeSubDomains`;
  `THINAIR_API_DOCS=auth` default puts `/docs`, `/redoc`, `/openapi.json` behind login.
- App: the "can't reach" message explains an IP change; retries when back online.
- Docs: README (phone access, security model, troubleshooting), SECURITY.md, docs/oracle.md
  (reserved IP, new flow), new docs/phone-access.md (checked facts with sources), .env.example,
  CLAUDE.md.

Deviations from the plan, and why:
- Moved out of phase 1 (the owner's brief made phase 1 small): Welcome screen, `setup_state`,
  `/api/setup`, `/api/meta`, funnel events, short pairing codes, design tokens. They belong with
  the guided first run (phase 4) and the redesign (phase 6). Phase 1 adds no new open route, so
  "no claiming before pairing" holds trivially here.
- Detection runs inside the running server container (`compose exec`), with the local IP and DMI
  strings read by the shell and passed in, instead of `docker run --network host`.
- Readiness is a verified TLS handshake through `caddy:443` instead of a hairpin probe to the
  public IP: it needs no hairpin routing, and a valid certificate already proves Let's Encrypt
  reached the machine from outside. So the Oracle hairpin question no longer matters.
- Host firewalls: ufw is not touched (Docker-published ports bypass it, per Docker's docs); the
  existing Oracle iptables prompt is kept unchanged. No new prompts.
- AWS can't be recognised without root (cloud-init uses the root-only `product_uuid`), so AWS
  takes the "nat" path: 90 s, then "keep waiting while you open the ports?" or the tunnel choices.
- A hand-set `THINAIR_PUBLIC_IP` is saved in `.env` and trusted by doctor and the startup check
  (otherwise a machine whose outgoing address differs would report "IP changed" forever).
- Token test: the old case refusing a quote character was replaced by control-character, tab,
  DEL, non-ASCII and over-length cases, because the brief asks for opaque tokens.

Follow-ups:
- Phase 3: when the IP changes, also update the GitHub App's callback/setup URLs (there is no app yet).
- 1 MB request-body cap on JSON routes (plan 4.5) is not done yet.
- Owner's manual tests still open: Tailscale consent link, Oracle Linux firewalld (see
  docs/phone-access.md "Not verified yet").

Real Oracle VM test (2026-11-14, Ubuntu on Ampere A1, phase-1 image built on the VM):
- Closed security list: outside connections time out. After the ingress rule, Let's Encrypt
  issued a real certificate for `152-67-4-135.sslip.io` in under a minute; /docs 401, HTTP 308,
  HSTS and doctor's new checks all good from outside.
- Oracle's iptables `INPUT ... REJECT` does not affect Docker-published ports (tcpdump: SYN in,
  SYN-ACK out, rule untouched). The installer's iptables offer is removed; it now only explains the
  console rule (firewalld on Oracle Linux still offered, untested).
- Bug found and fixed: `THINAIR_PHONE=public` on a Tailscale install failed because `tailscale
  serve` holds the tailnet address:443, so Docker can't publish 0.0.0.0:443. The installer now
  turns that handler off first and restores it if public mode fails.

### Phase 2: Sign in to the agent from the phone (2026-11-14, branch `onboarding-v2/phase-2-agent-login`)

Shipped:
- `SandboxSpec.tty` + `SandboxManager.open_terminal` / `Terminal` (attach before start; reads in a
  thread because Docker Desktop's Windows named pipe ignores socket timeouts).
- `agents/login.py`: `FlowMachine` (pure), `LoginSession`, `LoginHub`, `DockerLoginBackend`.
  `login_flow` on `claude` and `codex` (Claude Code (ACP) signs in through Claude Code's flow).
- `api/login_api.py`: `POST /api/agents/{id}/login`, `GET /api/agent-logins/{sid}` (+ `/events`
  SSE), `POST .../input`, `DELETE`, `POST /api/agents/{id}/login/check`; `GET /api/agents` gains
  `login: {in_app, state}`. All behind `require_auth`; no new open route.
- UI: header link **Agents** (`#/agents`) and the O3 sign-in screen (`#/agent/<id>/login`);
  a job that failed with `agent_not_authenticated` links to it. Doctor's fix text and the agents'
  hints point to the Agents screen.

[verify in build] results:
- `claude auth login` in a PTY (2.1.311): line-based (no `pyte`), prints `visit: <link>` (OSC 8
  + coloured copy) and `Paste code here if prompted > `. The link host is **`claude.com`**
  (`/cai/oauth/authorize`), not `claude.ai` as planned; `code_challenge` + `S256` present. The
  pinned binary exchanges the code at `platform.claude.com/v1/oauth/token` and calls
  `api.anthropic.com` (strings in the binary), both already on the egress allowlist.
- `codex login --device-auth` (0.166.0): `https://auth.openai.com/codex/device` + a code like
  `ABCD-EFGH1` ("expires in 15 minutes"); contacts auth.openai.com; exits 0 when saved.
- TTY attach through tecnativa/docker-socket-proxy: works with the existing permissions (real
  server on the Oracle ARM VM: both CLIs reached the link, cancel removed the containers).

Deviations from the plan:
- No `success` regex: the exit code plus `check_command` decide (more reliable than text).
- States add `cancelled`; `GET /api/agent-logins/{sid}` added (the screen renders from GET or SSE,
  same payload). A server-wide cap of 3 sessions (429 `too_many_logins`) on top of one per user.
- The O3 screen lives at `#/agent/<id>/login` behind a new Agents screen, because Settings and
  the setup flow arrive in phases 4 and 6; they will link to the same route.
- `login.state` comes from sign-ins and explicit checks only; a job failing with
  `agent_not_authenticated` doesn't update it yet (follow-up).

Follow-ups:
- Owner's manual test: complete a real Claude Code and Codex sign-in from the phone, then run a
  task (below). Not possible in automation (never a real account).
- Phase 4: setup step 2 reuses `#/agent/<id>/login`; mark the job-failure state in `known`.

### Phase 3: Connect GitHub (2026-11-17, branch `onboarding-v2/phase-3-github`)

Shipped:
- `secrets.py`, `onetime.py`, `github_app.py` (manifest, conversion, RS256 JWT, installation sync, token
  mint/revoke, repo listing, disconnect), `GitHubAccess`/`Credential`/`commit_identity` in `github.py`,
  `api/github_api.py` (status, start, two open callbacks, sync, picker list, disconnect), migration 0010
  (`GitHubInstallation`, `OneTimeState`, `Repo.private`), `cryptography` dependency.
- Rewiring: `prepare._load_source` (clone key, revoked in `finally`, app-specific hints), `jobs/pr.py` (separate
  push and PR keys, workflows-refusal message, `require_credentials()`), `submit_pr`, `RepoIn/RepoOut.private`.
- UI: header link GitHub (`#/setup/github`, O2), picker (`#/setup/project`, O4), home shortcut. CSP
  `form-action` + `https://github.com`. Doctor `GitHub App` check, `thinair github status|disconnect`,
  `THINAIR_GITHUB_FOR_MEMBERS`, `docs/github.md`, README/SECURITY/.env.example rewritten around Connect.
- Defence in depth: `VolumeMount` refuses the server's data volume.

[verify in build] results:
- `cryptography` has arm64 manylinux wheels for CPython 3.12 (`pip download --platform manylinux2014_aarch64`:
  cryptography 50.0.2 abi3) and installs on x86; no compiler needed in the server image.
- GitHub's setup-URL docs name only `installation_id` (and warn it can be spoofed); they do not say whether
  `state` is echoed back. The design doesn't depend on it (live state skips the cooldown; otherwise the route
  re-reads installations, rate limited).
- Create-a-PR needs Pull requests: write; GET repo needs Metadata: read (GitHub's permissions table). The PR key asks
  for both. Not tested against a real GitHub (needs the owner's account).
- Existing tests unchanged: one required `pr_executor` to fail on missing credentials before touching the database,
  so `require_credentials()` runs first.

Deviations / decisions (choose-the-best-option, please review):
- `prepare_workspace` keeps its `github_auth` parameter (bool or `GitHubAccess`) instead of a new `github` one:
  `test_users.py` asserts the executors pass a bool, and installations are server-level until phase 7 so a bool
  carries all the information. True now means "may use the server's GitHub connection (app > PAT)".
- Early PR check (`submit_pr`) is "any credential configured", not "this repo is covered": coverage needs a GitHub call,
  so it is reported in the job instead.
- App errors (GitHub down, repo not covered) fall back to PAT/anonymous rather than failing, so public clones keep
  working during an outage; the clone-failure hint carries the reason.
- The setup callback is open and has no reliable state, so it is per-IP rate limited (20/min) and cooled down (5 s)
  rather than state-gated.
- No Settings screen exists yet (phase 6) and phase 1 added no Welcome/checklist, so Connect GitHub and the picker
  are reached from a header link and the home screen, at the plan's `#/setup/*` routes.
- `GET /api/github/repos` is available to anyone allowed to use the server's connection (admins, or members with the
  setting); manage actions are admin-only.

Follow-ups: owner's real-GitHub round trip (below); phase 4 wires O2/O4 into the guided flow; phase 7 members'
own installations.

### Phase 4: The first run, end to end (2026-11-17, branch `onboarding-v2/phase-4-first-run`)

Shipped:
- `setup_state.py` + migration 0011 (`SetupEvent`, `PairingCode.short_hash/failures`), `api/setup_api.py`
  (`/api/meta`, `/api/setup`, `/api/health`, `/api/health/fix/images`), funnel hooks, `thinair stats`.
- The pieces phase 1 deferred: Welcome screen, setup state, `/api/meta`, funnel events and short pairing codes
  (`thinair qr --short`, "Or use a code" on the login screen, the installer prints one).
- UI: Welcome (O1) with agent choice, one resumable flow O2-O5 with a progress bar and Skip on every step, "Finish
  setting up" card, starter tasks, empty states for Projects and the project page, Setup health (C8) with one-tap
  actions, first-run timing line.

Deviations / decisions (please review):
- `SetupEvent` uses `user_key` ("owner" or the user id) instead of a nullable `user_id`, so the unique key really is
  one row per person (NULLs are not unique in SQL).
- The agent step is `done` when the preferred agent's login is known to be good in this server's memory (a sign-in
  or check since the last start) OR an agent job of that agent already succeeded for that person. After a restart
  with no such job it shows "isn't signed in yet" until they tap Check or sign in again: running a container on every
  setup read would break "idle cost near zero".
- Opening Setup health uses `run_checks(online=False)`: no request leaves the machine. The GitHub token check and
  version check stay in `thinair doctor` only. Members don't get the screen (403); they see their agent state on Agents.
- No Settings screen yet (phase 6): Setup health is linked from the bottom of Projects (admins) at its planned route
  `#/settings/health`. Notifications aren't a setup step until phase 5.
- Short codes: a wrong short code can't be attributed to a row, so it counts against every live short code (20 burns
  them). An attacker can therefore burn the short form (not the QR) with 20 guesses; the per-address throttle slows them.
- `/api/meta` lists only `github_app`, `agent_login`, `setup` as features until push exists.

[verify in build]: every first-run step runs in the browser against fake executors, a fake GitHub and a fake login
agent (18 checks, light + dark). Not done: the plan's stopwatch run on a fresh VM with someone who hasn't seen the
product (needs a person); `thinair stats` is what records it.

Follow-ups: the owner's stopwatch run (replace the estimates in section 1 with `thinair stats` numbers); phase 5 adds
the notifications step.

### Phase 5: Notifications and install (2026-11-17, branch `onboarding-v2/phase-5-push`)

Shipped:
- `push.py` (VAPID, RFC 8291 encryption, allowlisted sender), migration 0012 (`PushSubscription`), `api/push_api.py`,
  `WebPushNotifier` + shared `job_summary()`, the attention hook with a 60 s debounce, `THINAIR_WEB_PUSH`.
- `sw.js` `push` / `notificationclick`, manifest `id` and `share_target`, the O6 banner (Projects and running job),
  iOS Add-to-Home-Screen steps, Android install button, share-a-GitHub-link handling.
- Doctor `push` check; README "Notifications" rewritten (Web Push first, Telegram kept as the alternative).

[verify in build] results:
- RFC 8291: the encryption reproduces the RFC's Appendix A message byte for byte (values read from the RFC text).
- `sw.js` `push` handler reads the same JSON the server sends for Declarative Web Push and calls `showNotification`;
  checked in a real Edge with the push manager stubbed (banner, subscribe, test, iOS sheet, install, share). Delivery
  through a real push service, iOS 16.4+/18.4+ behaviour and the iOS cookie carry-over are NOT verified: they need
  real phones (see the pull request's manual steps).

Deviations / decisions (please review):
- `JobService` keeps Telegram in `notifier` and takes Web Push as `push_notifier`: an existing test requires
  `_notifier is None` when Telegram settings are malformed, so the plan's "compose the notifiers" is done inside the
  service instead of in `app.py`.
- The banner appears on Projects and on a running job (there is no Settings screen until phase 6); "Not now" is
  remembered per browser.
- VAPID `sub` is the server's https `THINAIR_PUBLIC_URL`, else `mailto:thinair@example.com` (the spec needs one of the
  two; nothing personal is sent).
- A test notification goes to all of the caller's own devices; at most 20 devices per person, one per login.
- Declarative Web Push `navigate` is an absolute https URL when `THINAIR_PUBLIC_URL` is https, else a path.

Follow-ups: the owner's real-phone tests (below); phase 6 moves the banner into Settings.

### Phase 6: The core loop, redesigned (2026-11-17, branch `onboarding-v2/phase-6-core-loop`)

Shipped:
- Design tokens with a WCAG AA test; hairline/dot-and-word styling; icon sprite and `icon()`; redrawn brand icon with a
  stdlib generator (`scripts/make_icons.py`); skeleton rows, offline banner, haptics (Android), reduced motion.
- Bottom tab bar (Projects, Activity, Settings), Activity, Settings with GitHub / Agents / Notifications / Setup health /
  Devices / People / version / address / Log out; Devices (list, revoke, "Add a phone" QR drawn as an SVG path by the
  server); People (list, disable, add + pairing code).
- Composer: "Measure resources instead" replaces the tabs, "Start with <agent>", draft kept per project, inline
  "isn't signed in. Sign in" row.
- Live job: prose + one-line tool rows that expand, raw log folded for agent jobs, queued/starting/reconnecting states,
  follow-the-bottom with a "New messages" pill, approval card (alertdialog, countdown, collapses when answered).
- Diff review (`#/job/<id>/changes`): `diff.js` parser (Node-tested: CRLF, binary, rename, no-newline, 5,000 lines),
  wrapped lines with a hanging indent and a sideways-scroll switch, per-file folds, large/binary start closed, jump
  between changes, sticky Create pull request / Patch bar. Result card with Review changes; "Pull request opened" card
  with the one-time celebration.
- `app.js` split into seven plain scripts (no build step).

Deviations / decisions (please review):
- Two plan colours were darkened to pass AA on the tinted diff backgrounds: `--ok` #15803d -> #137537, `--danger`
  #dc2626 -> #c81e1e. Added `--danger-ink` (white on light, near-black on dark): dark `--danger` with white text was
  2.8:1.
- Notifications has no screen of its own: its Settings row re-opens the banner (with test / turn off).
- "Always allow commands like this" on the approval card is not built: the buttons are the agent's own options, as
  before.
- The first-PR celebration is remembered per browser (localStorage), not per user on the server.
- Optimistic UI is limited to what already was (chat messages); skip-step and approvals still wait for the server.
- Android share target opens the add-project form with the link filled in (it doesn't add the repository by itself).
- The old header links (GitHub, Agents, Log out) moved into Settings; the phase 2 and 3 smoke scripts that clicked them
  were updated or are stale (not in CI).
- The "added by an admin" person gets a pairing code, but the "first" token `POST /api/users` creates stays unused
  (revoke it from the CLI if you care).

Not verified: the plan's real-device checks (a real 30-file diff one-handed on a 6-inch phone; VoiceOver and
TalkBack; airplane mode on a phone). Chrome-level checks pass at 390 px and 320 px, in dark mode, with reduced
motion and offline.

### Phase 7: Several people, and leftovers (2026-11-17, branch `onboarding-v2/phase-7-members`)

Shipped:
- Members connect **their own** GitHub installation of the server's app: `POST /api/github/mine/start`, the open
  callback `/api/github/callback/user`, `DELETE /api/github/mine`, `installations_for(user)`, `claim_installations`,
  per-person token resolution (`GitHubAccess(user_id, use_server)`), `users.github_allowed`, the picker and PR path
  for members with their own installation, Connect my GitHub UI, and the admin's note about making the app public.
- `docs/github.md`, README and SECURITY updated; `apps/android/README.md` (what a TWA needs).

[verify in build] results:
- GitHub's docs say that with "Request user authorization during installation", GitHub redirects to the first
  callback URL with only a `code` (no `state` or `installation_id` is documented), and the manifest page for those
  fields returned 404. So the build does **not** rely on install-time OAuth: the member authorizes with our own
  `state` (documented web flow), and claims are checked against `GET /user/installations` for their own token.
  The token endpoint takes `client_id`, `client_secret`, `code` (and `redirect_uri`); the docs don't state the response
  format, so the request sends `Accept: application/json`. User tokens are `ghu_`, 8 hours.
  `GET /user/installations`'s token type isn't stated on its own page (the user-token page points to it).
- Not verified against a real GitHub: the whole member round trip (needs two accounts).

Decisions (please review):
- No in-app raw terminal login for Gemini and registry agents. Phase 2's findings don't justify it: Claude Code and Codex
  log in with a line-based command, while Gemini has no login command (its sign-in lives inside its full-screen UI), so a
  raw terminal would need a terminal emulator in the page for one agent whose vendor is moving to another CLI.
  They keep the terminal command shown on the Agents screen.
- Android TWA: documented only (`apps/android/README.md`). It needs the owner's signing key and Digital Asset Links
  hosted at `/.well-known/assetlinks.json`; the latter would be another open route, so it isn't added speculatively.
- Let's Encrypt IP certificates: not adopted. Caddy's IP-certificate support (the `shortlived` ACME profile, issue
  #7399, closed) exists but no release that first shipped it could be confirmed, and it would change the default
  install; sslip.io stays the default. Revisit by testing a pinned Caddy on a real public IP.
- Members' installations are never adopted by an admin sync of existing rows, but an admin tapping refresh between a
  member installing and returning can record it as the server's (documented limit in docs/github.md).
- The app's callback URL only exists for apps created from this release on; older apps show "reconnect once".

Follow-ups: the owner's two-account test; revisit the leftovers above if they matter.
