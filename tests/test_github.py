import base64

import pytest

from thinair.config import get_settings
from thinair.github import GitHubTokenError, git_auth_env, github_token
from thinair.project.prepare import GitSource, clone_spec
from thinair.sandbox.manager import build_container_args

TOKEN = "github_pat_" + "A1b2C3d4" * 6


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
    "bad", ["short", TOKEN + "\nX-Evil: 1", TOKEN + " x", "tok'en" + "a" * 30, "a" * 300]
)
def test_malformed_token_is_refused_without_echoing_it(env, bad) -> None:
    env(github_token=bad)
    with pytest.raises(GitHubTokenError) as e:
        github_token()
    assert bad.strip() not in str(e.value)


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
