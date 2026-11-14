import base64

import pytest

from thinair.config import get_settings
from thinair.github import GitHubTokenError, git_auth_env, github_token
from thinair.project.prepare import GitSource, clone_spec
from thinair.sandbox.manager import build_container_args

TOKEN = "github_pat_" + "A1b2C3d4" * 6


def stateless_installation_token(length: int = 520) -> str:
    """A token shaped like GitHub's stateless installation tokens (since 2026-04-27):
    `ghs_<app id>_<JWT>`, a JWT being three base64url parts joined by dots."""

    def b64url(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    head = b64url(b'{"alg":"RS256","typ":"JWT"}')
    body = b64url(b'{"iss":"1234567","iat":1789000000,"exp":1789003600,"repos":[42]}')
    prefix = f"ghs_1234567_{head}.{body}."
    sig = b64url(bytes(range(256)) * 16)[: length - len(prefix)]
    token = prefix + sig
    assert len(token) == length and token.count(".") == 2
    return token


APP_TOKEN = stateless_installation_token()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("THINAIR_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("THINAIR_GITHUB_TOKEN_FILE", raising=False)

    def set_(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(f"THINAIR_{k.upper()}", v)
        get_settings.cache_clear()

    set_()
    return set_


def test_no_token_configured(env) -> None:
    assert github_token() is None
    env(github_token="  ")
    assert github_token() is None


def test_blank_values_as_in_env_example_mean_no_token(env) -> None:
    """Regression: a blank THINAIR_GITHUB_TOKEN_FILE became Path(".") and broke every clone."""
    env(github_token="", github_token_file="")
    assert get_settings().github_token_file is None
    assert github_token() is None


def test_token_from_env_and_file(env, tmp_path) -> None:
    env(github_token=TOKEN)
    assert github_token() == TOKEN
    f = tmp_path / "gh_token"
    f.write_text(TOKEN + "\n")  # trailing newline from `echo` is fine
    env(github_token="", github_token_file=str(f))
    assert github_token() == TOKEN


def test_token_file_is_reread_after_rotation(env, tmp_path) -> None:
    f = tmp_path / "gh_token"
    f.write_text(TOKEN)
    env(github_token_file=str(f))
    assert github_token() == TOKEN
    f.write_text("ghp_" + "z" * 36)
    assert github_token() == "ghp_" + "z" * 36


@pytest.mark.parametrize(
    "bad",
    [
        "short",
        TOKEN + "\nX-Evil: 1",
        TOKEN + "\r\nX-Evil: 1",
        TOKEN + " x",
        TOKEN + "\tx",
        TOKEN + "\x7fx",
        TOKEN + "é",  # not ASCII: can't be sent in an HTTP header as is
        "a" * 4097,
    ],
)
def test_malformed_token_is_refused_without_echoing_it(env, bad) -> None:
    env(github_token=bad)
    with pytest.raises(GitHubTokenError) as e:
        github_token()
    assert bad.strip() not in str(e.value)


@pytest.mark.parametrize(
    "token",
    [APP_TOKEN, stateless_installation_token(4096), "ghs_" + "x" * 36, "ghp_" + "z" * 36],
)
def test_tokens_are_opaque_any_length_and_alphabet_github_uses(env, tmp_path, token) -> None:
    env(github_token=token)
    assert github_token() == token
    f = tmp_path / "tok"
    f.write_text(token + "\n")
    env(github_token="", github_token_file=str(f))
    assert github_token() == token


def test_stateless_app_token_in_git_env_clone_spec_and_rest_header() -> None:
    e, redact = git_auth_env(APP_TOKEN)
    basic = e["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(basic).decode() == f"x-access-token:{APP_TOKEN}"
    assert "\n" not in e["GIT_CONFIG_VALUE_0"] and set(redact) == {APP_TOKEN, basic}

    spec = clone_spec("j1", "vol", GitSource("https://github.com/o/r"), APP_TOKEN)
    assert not any(APP_TOKEN in a or basic in a for a in spec.command)
    assert {APP_TOKEN, basic} <= set(spec.redact)

    from thinair.github import GitHubAPIError, GitHubClient

    sent = {}

    class Session:
        def request(self, method, url, headers, timeout, **kw):
            sent.update(headers)
            return type("R", (), {"status_code": 401, "content": b"{}", "json": lambda s: {}})()

    with pytest.raises(GitHubAPIError) as err:
        GitHubClient(APP_TOKEN, session=Session()).default_branch("o", "r")
    assert sent["Authorization"] == f"Bearer {APP_TOKEN}"
    assert APP_TOKEN not in err.value.message


def test_long_tokens_are_masked_even_where_a_long_line_is_cut() -> None:
    """Regression: lines were cut to MAX_LINE_CHARS before masking, so a token across the cut
    kept its first part visible. With ~520/715-char tokens that is a realistic leak."""
    import threading

    from thinair.sandbox import manager as m
    from thinair.sandbox.spec import SandboxSpec

    e, redact = git_auth_env(APP_TOKEN)
    basic = e["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    pad = "x" * (m.MAX_LINE_CHARS - 300)  # each secret straddles the cut point
    lines = [f"{pad}{APP_TOKEN} tail", f"{pad}{basic} tail", f"plain {APP_TOKEN} and {basic}"]

    class Container:
        def logs(self, **_):
            data = ("\n".join(lines) + "\n").encode()
            return [data[i : i + 333] for i in range(0, len(data), 333)]  # split mid-token too

    spec = SandboxSpec(image="i", command=["c"], job_id="j", redact=redact)
    out: list[str] = []
    m._stream_logs(Container(), spec, out.append, threading.Event())
    assert len(out) == 3
    for line in out:
        assert APP_TOKEN[:12] not in line and basic[:12] not in line, line[-400:]
    assert out[2] == "plain *** and ***"


def test_control_characters_in_a_token_file_are_refused(env, tmp_path) -> None:
    f = tmp_path / "tok"
    for bad in (TOKEN + "\x00x", TOKEN + "\nX-Evil: 1", "\x1b[2J" + TOKEN):
        f.write_bytes(bad.encode())
        env(github_token="", github_token_file=str(f))
        with pytest.raises(GitHubTokenError) as e:
            github_token()
        assert TOKEN not in str(e.value)


def test_unreadable_token_file(env, tmp_path) -> None:
    env(github_token_file=str(tmp_path / "missing"))
    with pytest.raises(GitHubTokenError, match="THINAIR_GITHUB_TOKEN_FILE"):
        github_token()


def test_auth_env_is_scoped_to_github_and_masks_both_forms() -> None:
    e, redact = git_auth_env(TOKEN)
    assert e["GIT_CONFIG_COUNT"] == "1"
    assert e["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    basic = e["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(basic).decode() == f"x-access-token:{TOKEN}"
    assert set(redact) == {TOKEN, basic}


def test_clone_spec_keeps_the_token_out_of_argv() -> None:
    spec = clone_spec("j1", "vol", GitSource("https://github.com/o/r"), TOKEN)
    args = build_container_args(spec)
    assert not any(TOKEN in a for a in spec.command)
    assert "https://github.com/o/r" in spec.command  # the URL carries no credentials
    assert spec.network is True
    assert TOKEN in spec.redact
    assert args["environment"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"


def test_clone_spec_without_token_has_no_auth() -> None:
    spec = clone_spec("j1", "vol", GitSource("https://github.com/o/r"), None)
    assert not any(k.startswith("GIT_CONFIG") for k in spec.env)
    assert spec.redact == []


def test_git_author_blank_means_default_and_bad_values_are_refused(env, monkeypatch) -> None:
    from pydantic import ValidationError

    from thinair.config import Settings

    env(git_author_name="", git_author_email="  ")
    s = get_settings()
    assert (s.git_author_name, s.git_author_email) == ("Thinair", "thinair@localhost")
    monkeypatch.setenv("THINAIR_GIT_AUTHOR_EMAIL", "not an email")
    with pytest.raises(ValidationError):
        Settings()
