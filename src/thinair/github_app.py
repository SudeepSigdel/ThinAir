"""The per-server GitHub App: how Thinair gets repository access without a pasted token.

The user taps "Connect GitHub"; their browser posts a manifest to github.com, which creates a
private app on their account and redirects back here with a one-time `code`. Thinair converts the
code into the app's credentials, stores them in `secrets/github-app.json` (never the database,
never the environment), and from then on mints **installation tokens**: valid at most one hour,
limited to one repository and one kind of access, and revoked as soon as the step that needed
them ends. The private key never enters a container. Every call is outbound to api.github.com:
GitHub never calls the server (webhooks are off), so a server reachable only from the phone works.

Docs (checked 2026-11-14, see docs/github.md): registering from a manifest, the setup URL, the
installation REST endpoints and generating a JWT.
"""

import base64
import json
import re
import secrets as _stdlib_secrets
import time
from dataclasses import asdict, dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlmodel import Session, select

from thinair.db.models import GitHubInstallation, utcnow
from thinair.github import API, TOKEN_RE, GitHubAPIError
from thinair.secrets import SecretError, delete_secret, read_secret, write_secret

SECRET_NAME = "github-app.json"  # noqa: S105 - a file name, not a secret
PERMISSIONS = {
    "clone": {"contents": "read"},
    "push": {"contents": "write"},
    "pr": {"pull_requests": "write", "metadata": "read"},  # metadata: read the default branch
    "list": {"metadata": "read"},
}
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
MAX_REPO_PAGES = 5  # 500 repositories is plenty for a phone picker


@dataclass(frozen=True, repr=False)
class AppCredentials:
    """What the manifest conversion returns. `repr()` prints `***`, so a stray log line or
    traceback can't leak the key."""

    app_id: int
    slug: str
    client_id: str
    client_secret: str
    pem: str
    html_url: str
    owner_login: str
    owner_id: int
    owner_type: str

    def __repr__(self) -> str:
        return "AppCredentials(***)"

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "AppCredentials":
        data = json.loads(raw)
        return cls(**{k: data[k] for k in cls.__dataclass_fields__})


# ---- stored credentials --------------------------------------------------------------------
def app_connected() -> bool:
    try:
        return read_secret(SECRET_NAME) is not None
    except SecretError:
        return True  # there is a file; whether it is usable is for load_credentials() to say


def load_credentials() -> AppCredentials | None:
    """The stored app, None if none is connected. Raises GitHubAPIError if the file is damaged
    (so a broken key is reported, not silently treated as "not connected")."""
    try:
        raw = read_secret(SECRET_NAME)
    except SecretError as e:
        raise GitHubAPIError(None, f"Thinair's GitHub App key can't be read ({e}).") from None
    if raw is None:
        return None
    try:
        return AppCredentials.from_json(raw)
    except (ValueError, KeyError, TypeError):
        raise GitHubAPIError(
            None, "Thinair's GitHub App key file is damaged. Reconnect GitHub."
        ) from None


# ---- manifest ------------------------------------------------------------------------------
def new_app_name() -> str:
    """App names are globally unique on GitHub, so a fixed name would collide."""
    return f"thinair-{_stdlib_secrets.token_hex(3)}"


def check_org(org: str | None) -> str | None:
    if org is None or org == "":
        return None
    if not _ORG_RE.match(org):
        raise ValueError("that is not a GitHub organization name")
    return org


def manifest_action(org: str | None, state: str) -> str:
    base = (
        f"https://github.com/organizations/{org}/settings/apps/new"
        if org
        else "https://github.com/settings/apps/new"
    )
    return f"{base}?state={state}"


def build_manifest(public_url: str, name: str) -> dict[str, Any]:
    """Webhooks off, least privilege, a private app. `workflows: write` is deliberately not
    requested: a patch that touches .github/workflows/ is refused at push with a clear message."""
    base = public_url.rstrip("/")
    return {
        "name": name,
        "url": base,
        "public": False,
        "redirect_url": f"{base}/api/github/callback/app",
        "setup_url": f"{base}/api/github/callback/setup",
        "setup_on_update": True,
        "hook_attributes": {"url": f"{base}/api/github/hook", "active": False},
        "default_permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
        "default_events": [],
    }


def install_url(slug: str, state: str | None = None) -> str:
    url = f"https://github.com/apps/{slug}/installations/new"
    return f"{url}?state={state}" if state else url


def manage_url(inst: GitHubInstallation) -> str:
    if inst.account_type == "Organization":
        return (
            f"https://github.com/organizations/{inst.account_login}"
            f"/settings/installations/{inst.installation_id}"
        )
    return f"https://github.com/settings/installations/{inst.installation_id}"


def app_delete_url(creds: AppCredentials) -> str:
    """The app's own settings page, where the registration can be deleted (GitHub has no API)."""
    if creds.owner_type == "Organization":
        return (
            f"https://github.com/organizations/{creds.owner_login}/settings/apps/"
            f"{creds.slug}/advanced"
        )
    return f"https://github.com/settings/apps/{creds.slug}/advanced"


def noreply_email(creds: AppCredentials) -> str:
    return f"{creds.owner_id}+{creds.owner_login}@users.noreply.github.com"


# ---- JWT -----------------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def app_jwt(creds: AppCredentials, now: float | None = None) -> str:
    """RS256 JWT for the app itself: `iat` 60 s in the past (clock drift), `exp` 9 minutes
    ahead (GitHub allows 10), `iss` = the client id (GitHub's recommendation)."""
    t = int(time.time() if now is None else now)
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(
        json.dumps(
            {"iat": t - 60, "exp": t + 540, "iss": creds.client_id}, separators=(",", ":")
        ).encode()
    )
    try:
        key = serialization.load_pem_private_key(creds.pem.encode(), password=None)
        sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr,call-arg]
    except (ValueError, TypeError):
        raise GitHubAPIError(
            None, "Thinair's GitHub App key is not a valid private key. Reconnect GitHub."
        ) from None
    return f"{head}.{body}.{_b64(sig)}"


# ---- HTTP ----------------------------------------------------------------------------------
def _message(resp, doing: str) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    detail = str(data.get("message") or "")[:300] if isinstance(data, dict) else ""
    if resp.status_code == 401:
        return (
            "GitHub rejected Thinair's app key. The app may have been deleted or its key "
            "replaced: reconnect GitHub."
        )
    if resp.status_code == 403 and "rate limit" in detail.lower():
        return "GitHub's rate limit was reached. Try again in a few minutes."
    return f"GitHub refused to {doing} ({resp.status_code}): {detail or 'no details'}"


def _call(session, method: str, path: str, *, bearer: str | None, doing: str, **kw: Any):
    import requests

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "thinair",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        resp = session.request(method, API + path, headers=headers, timeout=30, **kw)
    except requests.RequestException as e:
        raise GitHubAPIError(None, f"Could not reach GitHub: {type(e).__name__}") from None
    if resp.status_code >= 400:
        raise GitHubAPIError(resp.status_code, _message(resp, doing))
    return resp.json() if resp.content else None


def _session(session):
    if session is not None:
        return session
    import requests

    return requests.Session()


def convert(code: str, session=None) -> AppCredentials:
    """Exchange the manifest `code` GitHub redirected back with (valid one hour, one use) for the
    new app's credentials. Unauthenticated: the code is the credential."""
    if not _CODE_RE.match(code):
        raise GitHubAPIError(None, "GitHub sent an unexpected code.")
    data = _call(
        _session(session), "POST", f"/app-manifests/{code}/conversions", bearer=None,
        doing="finish creating the app",
    )  # fmt: skip
    try:
        owner = data["owner"]
        creds = AppCredentials(
            app_id=int(data["id"]),
            slug=str(data["slug"]),
            client_id=str(data["client_id"]),
            client_secret=str(data["client_secret"]),
            pem=str(data["pem"]),
            html_url=str(data["html_url"]),
            owner_login=str(owner["login"]),
            owner_id=int(owner["id"]),
            owner_type=str(owner.get("type") or "User"),
        )
    except (KeyError, TypeError, ValueError):
        raise GitHubAPIError(None, "GitHub's answer was missing something we need.") from None
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", creds.slug):
        raise GitHubAPIError(None, "GitHub sent an unexpected app name.")
    app_jwt(creds)  # refuses a key that can't sign, before anything is stored
    return creds


def save_credentials(creds: AppCredentials) -> None:
    write_secret(SECRET_NAME, creds.to_json())


class GitHubApp:
    """Calls made as the app (JWT) or as one installation (a minted token). Synchronous:
    call from a thread. `session` is injectable so tests need no network."""

    def __init__(self, creds: AppCredentials, session=None) -> None:
        self.creds = creds
        self._s = _session(session)

    def installations(self) -> list[dict[str, Any]]:
        """Authoritative list of this app's installations (the `installation_id` a browser
        sends to the setup URL can be spoofed, so it is never trusted)."""
        out: list[dict[str, Any]] = []
        for page in range(1, 6):
            data = _call(
                self._s, "GET", "/app/installations", bearer=app_jwt(self.creds),
                doing="list installations", params={"per_page": 100, "page": page},
            )  # fmt: skip
            out += data or []
            if not data or len(data) < 100:
                break
        return out

    def installation_for_repo(self, owner: str, repo: str) -> int | None:
        """The installation that covers this repository, or None."""
        try:
            data = _call(
                self._s, "GET", f"/repos/{owner}/{repo}/installation",
                bearer=app_jwt(self.creds), doing="look up the repository",
            )  # fmt: skip
        except GitHubAPIError as e:
            if e.status == 404:
                return None
            raise
        return int(data["id"]) if isinstance(data, dict) and "id" in data else None

    def mint_token(
        self, installation_id: int, permissions: dict[str, str], repo: str | None = None
    ) -> str:
        body: dict[str, Any] = {"permissions": permissions}
        if repo:
            body["repositories"] = [repo]
        data = _call(
            self._s, "POST", f"/app/installations/{int(installation_id)}/access_tokens",
            bearer=app_jwt(self.creds), doing="create a short-lived key", json=body,
        )  # fmt: skip
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            raise GitHubAPIError(None, "GitHub returned a key in an unexpected format.")
        return token

    def revoke(self, token: str) -> None:
        """Invalidate an installation token now (`DELETE /installation/token`)."""
        _call(self._s, "DELETE", "/installation/token", bearer=token, doing="revoke a key")

    def list_repos(self, installation_id: int) -> list[dict[str, Any]]:
        """Repositories the installation can see, newest push first. Uses a metadata-only token,
        revoked straight after."""
        token = self.mint_token(installation_id, PERMISSIONS["list"])
        repos: list[dict[str, Any]] = []
        try:
            for page in range(1, MAX_REPO_PAGES + 1):
                data = _call(
                    self._s, "GET", "/installation/repositories", bearer=token,
                    doing="list repositories", params={"per_page": 100, "page": page},
                )  # fmt: skip
                batch = (data or {}).get("repositories") or []
                repos += batch
                if len(batch) < 100:
                    break
        finally:
            try:
                self.revoke(token)
            except GitHubAPIError:
                pass  # it expires within the hour anyway
        repos.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
        return repos

    def delete_installation(self, installation_id: int) -> None:
        _call(
            self._s, "DELETE", f"/app/installations/{int(installation_id)}",
            bearer=app_jwt(self.creds), doing="uninstall the app",
        )  # fmt: skip


# ---- the installations table ----------------------------------------------------------------
def active_installations(session: Session) -> list[GitHubInstallation]:
    return list(
        session.exec(
            select(GitHubInstallation).where(
                GitHubInstallation.removed_at.is_(None),  # type: ignore[union-attr]
                GitHubInstallation.user_id.is_(None),  # type: ignore[union-attr]
            )
        )
    )


def sync_installations(session: Session, app: GitHubApp) -> list[GitHubInstallation]:
    """Make the table match what GitHub says. Rows GitHub no longer lists are marked removed."""
    remote = {int(i["id"]): i for i in app.installations()}
    now = utcnow()
    rows = {r.installation_id: r for r in session.exec(select(GitHubInstallation))}
    for iid, info in remote.items():
        account = info.get("account") or {}
        row = rows.get(iid) or GitHubInstallation(
            installation_id=iid, account_login="", account_type="User", repository_selection="all"
        )
        row.account_login = str(account.get("login") or row.account_login)[:100]
        row.account_type = str(account.get("type") or row.account_type)
        row.repository_selection = str(info.get("repository_selection") or "all")
        row.removed_at = None
        session.add(row)
    for iid, row in rows.items():
        if iid not in remote and row.removed_at is None:
            row.removed_at = now
            session.add(row)
    session.commit()
    return active_installations(session)


def disconnect(session: Session) -> str | None:
    """Uninstall (best effort), delete the secret and the rows. Returns the app's settings page,
    where the user can delete the registration itself, or None if no app was connected."""
    try:
        creds = load_credentials()
    except GitHubAPIError:
        creds = None
        delete_secret(SECRET_NAME)
    if creds is None:
        for row in session.exec(select(GitHubInstallation)):
            session.delete(row)
        session.commit()
        return None
    app = GitHubApp(creds)
    for row in active_installations(session):
        try:
            app.delete_installation(row.installation_id)
        except GitHubAPIError:
            pass  # already gone, or the app was deleted on GitHub: the secret goes either way
    delete_secret(SECRET_NAME)
    for row in session.exec(select(GitHubInstallation)):
        session.delete(row)
    session.commit()
    return app_delete_url(creds)
