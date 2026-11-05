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
