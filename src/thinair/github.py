"""GitHub access: the optional token Thinair uses to clone private repositories.

The token is read from `THINAIR_GITHUB_TOKEN` or `THINAIR_GITHUB_TOKEN_FILE` (for Docker secrets)
and never stored in the database. It is handed only to the short-lived clone container, through
git's `GIT_CONFIG_*` environment variables, so it never appears in argv, in `.git/config` or in
the workspace volume, and it never reaches the install, test or agent containers. The header it
becomes is scoped to `https://github.com/`, so git sends it nowhere else.
"""

import asyncio
import base64
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from thinair.config import get_settings

log = logging.getLogger(__name__)

# Tokens are opaque: classic (ghp_...), fine-grained (github_pat_...) and app installation
# tokens, which since 2026-04-27 can be stateless `ghs_<app id>_<JWT>` (~520 characters, with
# dots). So no assumption about length or alphabet beyond what keeps a header safe: printable
# ASCII without spaces (no CR/LF/tab, so a value can't inject or split headers), 20-4096 chars.
TOKEN_RE = re.compile(r"^[\x21-\x7e]{20,4096}$")


class GitHubTokenError(Exception):
    """The configured token is unreadable or malformed. The message never contains it."""


def github_token() -> str | None:
    """The configured token, or None. Read on every call so a rotated file takes effect."""
    s = get_settings()
    raw = s.github_token.get_secret_value().strip() if s.github_token else ""
    # A blank THINAIR_GITHUB_TOKEN= (as in .env.example) must not hide the file.
    if not raw and s.github_token_file is not None:
        try:
            raw = s.github_token_file.read_text(encoding="utf-8")
        except OSError as e:
            raise GitHubTokenError(
                f"Could not read THINAIR_GITHUB_TOKEN_FILE ({s.github_token_file}): {e.strerror}"
            ) from None
    raw = (raw or "").strip()
    if not raw:
        return None
    if not TOKEN_RE.match(raw):
        raise GitHubTokenError(
            "THINAIR_GITHUB_TOKEN does not look like a GitHub token "
            "(expected 20-4096 visible characters, no spaces or line breaks)."
        )
    return raw


def git_auth_env(token: str) -> tuple[dict[str, str], list[str]]:
    """Environment that makes git authenticate to github.com, plus the values to mask in output.

    Needs git >= 2.31 (GIT_CONFIG_COUNT). GitHub accepts any token as the Basic-auth password.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }
    return env, [token, basic]


# ---- which credential a step may use ------------------------------------------------------
@dataclass
class Credential:
    """A token for one step. `source` is "app" (a one-hour installation token for one
    repository, revoked when the step ends) or "token" (the PAT from THINAIR_GITHUB_TOKEN).
    `repr()` hides it."""

    token: str = field(repr=False)
    source: str
    _revoke: Callable[[], None] | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"Credential(source={self.source!r}, token=***)"

    async def arevoke(self) -> None:
        """Invalidate an app token now. Never raises: it expires within the hour anyway."""
        if self._revoke is None:
            return
        revoke, self._revoke = self._revoke, None
        try:
            await asyncio.to_thread(revoke)
        except Exception as e:  # noqa: BLE001 - revoking is best effort; log the type only
            log.warning("could not revoke a GitHub App token (%s)", type(e).__name__)


class GitHubAccess:
    """Resolves, per step and repository, which GitHub credential to use: (1) the GitHub App's
    installation, if one covers the repository, (2) the token in THINAIR_GITHUB_TOKEN, (3) none
    (anonymous). Whether a *user* may use the server's GitHub connection at all is decided by
    the caller (`users.github_token_allowed`): this class only resolves what exists.

    `note` says why the app could not be used (shown in clone failure hints)."""

    def __init__(self, app_factory: Callable | None = None) -> None:
        self._factory = app_factory
        self.note: str | None = None

    @staticmethod
    def app_connected() -> bool:
        from thinair.github_app import app_connected

        return app_connected()

    def _app(self, creds):  # noqa: ANN001, ANN202
        if self._factory is not None:
            return self._factory(creds)
        from thinair.github_app import GitHubApp

        return GitHubApp(creds)

    def _from_app(self, repo_url: str, need: str) -> Credential | None:
        from thinair.github_app import PERMISSIONS, load_credentials

        try:
            creds = load_credentials()
            if creds is None:
                return None
            owner, name = repo_slug(repo_url)
            app = self._app(creds)
            iid = app.installation_for_repo(owner, name)
            if iid is None:
                self.note = "not_installed"
                return None
            token = app.mint_token(iid, PERMISSIONS[need], repo=name)
        except GitHubAPIError as e:
            self.note = e.message
            log.warning("GitHub App token unavailable (status %s)", e.status)
            return None
        except ValueError:
            return None  # not a github.com repository URL
        return Credential(token, "app", lambda: app.revoke(token))

    async def token_for(self, repo_url: str, need: str) -> Credential | None:
        """`need` is "clone", "push" or "pr". Raises GitHubTokenError if THINAIR_GITHUB_TOKEN
        is set but malformed (as before)."""
        if self.app_connected():
            cred = await asyncio.to_thread(self._from_app, repo_url, need)
            if cred is not None:
                return cred
        token = github_token()
        return Credential(token, "token") if token else None

    def can_open_pr(self) -> bool:
        """Cheap early check (no network): is any credential configured at all?"""
        return self.app_connected() or github_token() is not None


def commit_identity() -> tuple[str, str]:
    """Author name and email for commits Thinair makes. THINAIR_GIT_AUTHOR_* wins; when the email
    isn't set and an app is connected, the connecting account's noreply address."""
    s = get_settings()
    name, email = s.git_author_name, s.git_author_email
    if "git_author_email" not in s.model_fields_set:
        from thinair.github_app import load_credentials, noreply_email

        try:
            creds = load_credentials()
        except GitHubAPIError:
            creds = None
        if creds is not None:
            email = noreply_email(creds)
    return name, email


# ---- REST API (opening pull requests) ------------------------------------------------------
API = "https://api.github.com"
_SLUG = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class GitHubAPIError(Exception):
    """A GitHub API call failed. `message` is safe to show (never contains the token)."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def repo_slug(url: str) -> tuple[str, str]:
    m = _SLUG.match(url)
    if not m:
        raise ValueError(f"not a GitHub repository URL: {url!r}")
    return m.group(1), m.group(2)


class GitHubClient:
    """Tiny synchronous client for the few REST calls Thinair makes. Call it from a thread."""

    def __init__(
        self, token: str, session=None, timeout_s: float = 30, source: str = "token"
    ) -> None:
        import requests

        self._s = session or requests.Session()
        self._token = token
        self._timeout = timeout_s
        self._source = source

    def _call(self, method: str, path: str, **kw):
        import requests

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "thinair",
        }
        try:
            resp = self._s.request(method, API + path, headers=headers, timeout=self._timeout, **kw)
        except requests.RequestException as e:
            raise GitHubAPIError(None, f"Could not reach GitHub: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise GitHubAPIError(resp.status_code, _explain(resp, self._source))
        return resp.json() if resp.content else None

    def default_branch(self, owner: str, repo: str) -> str:
        return self._call("GET", f"/repos/{owner}/{repo}")["default_branch"]

    def find_pull(self, owner: str, repo: str, branch: str) -> str | None:
        pulls = self._call(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={"head": f"{owner}:{branch}", "state": "open"},
        )
        return pulls[0]["html_url"] if pulls else None

    def create_pull(
        self, owner: str, repo: str, *, title: str, body: str, head: str, base: str
    ) -> str:
        """Open a PR and return its URL. If one is already open for `head`, return that one
        (so retrying a PR job is safe)."""
        try:
            pr = self._call(
                "POST",
                f"/repos/{owner}/{repo}/pulls",
                json={"title": title, "body": body, "head": head, "base": base},
            )
        except GitHubAPIError as e:
            if e.status == 422 and "already exists" in e.message:
                existing = self.find_pull(owner, repo, head)
                if existing:
                    return existing
            raise
        return pr["html_url"]


def _explain(resp, source: str = "token") -> str:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    detail = str(data.get("message") or "")[:300]
    errors = data.get("errors") or []
    extra = "; ".join(
        str(e.get("message") or e.get("code") or "")[:200] for e in errors if isinstance(e, dict)
    )
    if extra:
        detail = f"{detail} ({extra})"
    if source == "app":
        if resp.status_code == 401:
            return "GitHub rejected Thinair's short-lived key. Try again."
        if resp.status_code in (403, 404):
            return (
                "GitHub refused the request. Thinair's GitHub App needs access to this "
                "repository with Pull requests: Read and write. "
                f"GitHub said: {detail or resp.status_code}"
            )
    if resp.status_code == 401:
        return "GitHub rejected THINAIR_GITHUB_TOKEN (expired or revoked?)."
    if resp.status_code in (403, 404):
        return (
            'GitHub refused the request. The token needs "Pull requests: Read and write" '
            f"access to this repository. GitHub said: {detail or resp.status_code}"
        )
    return f"GitHub said ({resp.status_code}): {detail or 'no details'}"
