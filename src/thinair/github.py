"""GitHub access: the optional token Thinair uses to clone private repositories.

The token is read from `THINAIR_GITHUB_TOKEN` or `THINAIR_GITHUB_TOKEN_FILE` (for Docker secrets)
and never stored in the database. It is handed only to the short-lived clone container, through
git's `GIT_CONFIG_*` environment variables, so it never appears in argv, in `.git/config` or in
the workspace volume, and it never reaches the install, test or agent containers. The header it
becomes is scoped to `https://github.com/`, so git sends it nowhere else.
"""

import base64
import re

from thinair.config import get_settings

# Classic (ghp_...), fine-grained (github_pat_...) and app (ghs_...) tokens: letters, digits, `_`.
# Anything else (spaces, newlines, quotes) is refused, so a value can't inject extra headers.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_]{20,255}$")


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
            "(expected only letters, digits and underscores)."
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

    def __init__(self, token: str, session=None, timeout_s: float = 30) -> None:
        import requests

        self._s = session or requests.Session()
        self._token = token
        self._timeout = timeout_s

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
            raise GitHubAPIError(resp.status_code, _explain(resp))
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


def _explain(resp) -> str:
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
    if resp.status_code == 401:
        return "GitHub rejected THINAIR_GITHUB_TOKEN (expired or revoked?)."
    if resp.status_code in (403, 404):
        return (
            'GitHub refused the request. The token needs "Pull requests: Read and write" '
            f"access to this repository. GitHub said: {detail or resp.status_code}"
        )
    return f"GitHub said ({resp.status_code}): {detail or 'no details'}"
