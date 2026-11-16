"""Clone through the GitHub App path with a real container: the app-minted key is a fake one that
GitHub rejects, so the clone fails, and the failure must be the App-specific one. Needs Docker and
internet (github.com)."""

import docker
import pytest
from test_github_app import FakeGitHub, connect_app

from thinair import github_app as ga
from thinair.config import get_settings
from thinair.project.prepare import GitSource, PrepareError, prepare_workspace
from thinair.sandbox.manager import SandboxManager

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def mgr():
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    return SandboxManager(c)


async def test_clone_with_a_rejected_app_key_fails_with_the_app_hint(mgr, monkeypatch) -> None:
    fake = FakeGitHub()
    fake.covered = {"octocat/Hello-World"}
    monkeypatch.setattr(ga, "_session", lambda session=None: session or fake)
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", "")
    get_settings.cache_clear()
    connect_app()
    logs: list[str] = []
    with pytest.raises(PrepareError) as e:
        await prepare_workspace(
            mgr, GitSource("https://github.com/octocat/Hello-World"), "t5appkey", logs.append
        )
    assert e.value.code == "clone_failed", e.value.message
    assert "GitHub App" in e.value.message and "THINAIR_GITHUB_TOKEN" not in e.value.message
    key = f"ghs_0001_{'x' * 30}"
    assert fake.minted == [{"permissions": {"contents": "read"}, "repositories": ["Hello-World"]}]
    assert fake.revoked == [key]  # invalidated right after the failed clone
    assert not [ln for ln in [e.value.message, *logs] if key in ln]  # and masked in the output
    left = mgr.client.volumes.list(filters={"label": "thinair.managed=true"})
    assert not [v for v in left if v.name.startswith("thinair-ws-t5appkey")], "volume leaked"
