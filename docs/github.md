# GitHub access

Thinair needs GitHub for three things: cloning private repositories, listing your repositories in
the picker, and opening pull requests. There are two ways to give it access.

| | Connect GitHub (the app) | A token in `.env` |
|---|---|---|
| Setup | Two taps on your phone | Create a token on GitHub, edit `.env`, restart |
| What a job gets | A key for **one repository**, valid at most an hour, revoked when the step ends | The same long-lived token in every clone and push container |
| Pull requests | Authored by `thinair-xxxxxx[bot]`, so you can approve and merge them under branch protection, and CI runs | Authored by the token's account |
| Stored on the server | The app's private key, in `<data dir>/secrets/github-app.json` (mode 0600) | The token, in `.env` |

If both exist, the app wins for repositories it can see; the token covers the rest. A server with
only `THINAIR_GITHUB_TOKEN` behaves exactly as before.

## Connect GitHub

Open **GitHub** in the app's header (an admin does this once).

1. Tap **Connect GitHub**. Your phone's browser goes to github.com with a manifest for a private
   GitHub App named `thinair-<6 hex>` (app names are globally unique). Tap **Create GitHub App**.
2. GitHub sends the browser back to your server, which exchanges the one-time code for the app's
   credentials and stores them, then sends you to the app's install page.
3. Choose the repositories (or all) and tap **Install**. You land back in Thinair: "Connected as
   *you*". **Pick a repository** lists what the app can see, newest push first.

Needs `THINAIR_PUBLIC_URL` (the installer sets it): GitHub's redirects are followed by *your
phone's browser*, so a server reachable only from the phone (Tailscale, for example) works.
GitHub never calls the server: webhooks are switched off, and Thinair only makes outbound calls to
`api.github.com`. That also means it learns that repositories were added or removed only when it
asks (when the picker opens, when you return to the screen, or when a key can't be created).

**Organizations:** open "Using a GitHub organization?" and type its name before tapping Connect.

**Change repositories:** "Change repositories on GitHub" opens the installation's settings.
**Disconnect** uninstalls the app, deletes the key and forgets the installations. GitHub has no API
to delete an app registration, so the screen then links to the app's settings page for that last tap.
**Reconnect** = Disconnect, then Connect: a new app with a new key.

## What a job gets

Thinair holds the app's private key and mints **installation tokens** with the app's JWT
(RS256, valid 9 minutes, issued 60 s in the past):

| Step | Key | Where it goes |
|---|---|---|
| Clone | `contents: read`, one repository | the clone container only, in git's environment (never argv, `.git/config` or the workspace) |
| Push | `contents: write`, one repository | the push container only |
| Open the PR | `pull_requests: write` + `metadata: read`, one repository | Thinair's own process only; it is in no container |
| Picker | `metadata: read` | Thinair's process; revoked right after the listing |

Each key is revoked (`DELETE /installation/token`) as soon as its step ends, even when the step
fails. Keys are masked in all output. Install, test and agent containers never get one.

Since 2026-04-27 installation tokens can be stateless `ghs_<app id>_<JWT>` values of about 520
characters containing dots, so Thinair treats tokens as opaque (printable ASCII, no whitespace).

## Security notes

- **The private key** lives only in `secrets/github-app.json` (directory 0700, file 0600) inside the
  server's data volume. Never in the database, the environment or logs; `repr()` of the credentials
  prints `***`. No sandbox can read it: sandboxes only mount per-job volumes, and a mount of the
  server's data volume is refused outright. `thinair doctor` fails if the file is readable by others.
- **If it is stolen**, the thief can mint tokens for every repository the app is installed on, with
  contents and pull-request write access, until you delete the app on GitHub. That is the same
  reach as a read/write token for those repositories, and smaller if you chose "Only select
  repositories". Backups of the data volume contain the key.
- **The two callbacks are open routes** (`/api/github/callback/app` and `/setup`), because GitHub's
  redirect carries no Thinair session. The first acts only on a one-time `state` that an admin just
  asked for (random, 10 minutes, single use, only its SHA-256 stored) and checks it *before* any
  call to GitHub. The second never trusts the `installation_id` in its URL (it can be spoofed): it
  asks GitHub, as the app, what is installed. Both are throttled per client address, and the second
  also waits at least five seconds between GitHub calls unless it carries a live state.
- **Workflows:** the app does not get the *Workflows* permission, on purpose. A change that edits
  `.github/workflows/` cannot be pushed; the job says so, and the patch can still be downloaded.
- **Members:** with several users the server's GitHub connection is for admins; set
  `THINAIR_GITHUB_FOR_MEMBERS=true` (or the older `THINAIR_GITHUB_TOKEN_FOR_MEMBERS`) to let members'
  jobs use it. Members' own installations are a later phase.
- **Remote runners** keep using their own `THINAIR_GITHUB_TOKEN`; the server never sends them app keys.

## Members' own GitHub accounts

Phase 7 lets each member connect an installation of the server's app on **their own** account.

1. The admin turns the app **public** in its GitHub settings (Advanced) once. Apps created before this
   feature also need a Disconnect, then Connect, so GitHub is given a user-authorization callback URL.
2. A member taps **Connect my GitHub**. Thinair sends them to GitHub's authorize page with a one-time
   `state` it issued for them (10 minutes, single use, only its SHA-256 stored). If they haven't installed
   the app yet, they are sent to its install page; they tap **Connect my GitHub** again afterwards.
3. GitHub sends them back to `/api/github/callback/user` with a `code`. Nothing leaves the server unless the
   `state` is live. Thinair trades the code (with the app's client secret, from the secret file) for a
   short-lived user token, reads `GET /user` and `GET /user/installations`, and drops the token (it is never
   stored or logged).
4. An installation is claimed **only if it is in that user's own list** and the app's own list (the
   authoritative one). A spoofed installation id claims nothing. An installation already owned by the server
   or another member is never taken over: remove the app on GitHub and install it again.

Which installation a job may use: the person's own installation for their repositories; the server's
installation and the `THINAIR_GITHUB_TOKEN` only if they may use the server's connection (admins, or
`THINAIR_GITHUB_FOR_MEMBERS`). A member with their own installation can open pull requests even without that
setting. Known limit: if an admin taps refresh on GitHub's side between a member installing the app and
returning to Thinair, that installation is recorded as the server's and the member can't claim it (remove it
on GitHub and install again).

## Commands

```sh
docker compose exec thinair thinair github status       # app, installations, token in .env
docker compose exec thinair thinair github disconnect   # uninstall + delete the key
docker compose exec thinair thinair doctor              # key file mode, installations
```

## Troubleshooting

| You see | Why | Fix |
|---|---|---|
| "Set THINAIR_PUBLIC_URL first" | GitHub needs an address to send your phone back to | Run the installer again, or set `THINAIR_PUBLIC_URL` |
| "That sign-in link has expired or was already used" | The state is single use and lasts 10 minutes | Tap Connect GitHub again |
| "Thinair's GitHub App could not read it" on a clone | The repository isn't covered by the installation | GitHub > Change repositories, add it |
| "GitHub rejected Thinair's app key" | The app was deleted on GitHub, or its key replaced | Reconnect GitHub |
| A PR push says it edits `.github/workflows/` | The app has no Workflows permission | Download the patch, or leave those files out |
| The app's settings page is a dead address after your URL changed | GitHub stores the setup URL at creation; it can't be edited | Reconnect GitHub (four taps) |

## Facts checked while building (2026-11-17)

- Manifest flow, conversion (`POST /app-manifests/{code}/conversions`, valid one hour), the JWT
  rules, `POST /app/installations/{id}/access_tokens` (body `repositories`, `permissions`),
  `DELETE /installation/token` and `GET /app/installations`: as in GitHub's REST docs.
- The setup URL docs name only `installation_id` and warn it can be spoofed. They do **not** say
  whether `state` from `installations/new?state=...` is passed back, so Thinair sends one but never
  depends on it: without it the setup route simply re-reads the installations (rate limited).
- Creating a pull request needs **Pull requests: write**; reading the default branch needs
  **Metadata: read** (per GitHub's "permissions required for GitHub Apps" page). The PR key asks for
  both explicitly.
- Not verified against a real GitHub: the whole redirect round trip. It needs your account; see
  the manual steps in the pull request.
