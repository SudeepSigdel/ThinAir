"""Signing in from the phone through real containers with a terminal (DockerLoginBackend).

A fake login program plays the CLI (coloured link, prompt, a refused code, then a credential
file in the login volume). The real Claude Code and Codex CLIs run only up to the link, with
`-test` ids and throwaway volumes: never a real account (CLAUDE.md).
"""

import asyncio
import time

import docker
import pytest

from thinair.agents.config import AgentConfig, load_agents
from thinair.agents.login import DockerLoginBackend, LoginHub
from thinair.sandbox.manager import SandboxManager

pytestmark = pytest.mark.docker

LOGIN = r"""
printf 'Opening browser\r\nIf the browser did not open, visit: \033]8;;https://login.example.com/a?code_challenge=abc&state=s1\007\033[94mhttps://login.example.com/a?code_challenge=abc&state=s1\033[39m\033]8;;\007\r\n'
tries=0
while [ "$tries" -lt 3 ]; do
  printf 'Paste code here if prompted > '
  IFS= read -r code || exit 3
  if [ "$code" = "good-code" ]; then
    printf '%s' "$code" > /thinair-auth/credentials
    echo 'Login successful.'
    exit 0
  fi
  echo 'OAuth error: invalid code'
  tries=$((tries + 1))
done
exit 1
"""
VOLUME = "thinair-auth-fakelogin-dockertest"


def fake_agent() -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "id": "fakelogin",
            "name": "Fake Login",
            "install": "true",
            "command": ["sh", "-c", "true", "x", "{task}"],
            "network": False,
            "auth": {
                "login_command": ["sh"],
                "check_command": ["sh", "-c", "test -s /thinair-auth/credentials"],
                "login_flow": {
                    "command": ["sh", "-c", LOGIN],
                    "url": r"visit:\s*(https://\S+)",
                    "url_hosts": ["login.example.com"],
                    "code_in": "Paste code here",
                },
            },
        }
    )


@pytest.fixture(scope="module")
def mgr():
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    return SandboxManager(c)


@pytest.fixture
def hub(mgr):
    mgr.remove_volume(VOLUME)
    yield LoginHub(DockerLoginBackend(mgr))
    mgr.remove_volume(VOLUME)


def leftovers(mgr, sid: str) -> list:
    f = {"label": f"thinair.job=login-{sid}"}
    return mgr.client.containers.list(all=True, filters=f) + mgr.client.networks.list(filters=f)


async def until(s, *states: str, timeout: float = 240) -> None:
    deadline = time.monotonic() + timeout
    while s.snap.state not in states:
        assert time.monotonic() < deadline, f"stuck in {s.snap.public()}"
        await s.changed(s.version, 1)


async def test_fake_login_through_a_real_terminal(mgr, hub) -> None:
    s = hub.start(fake_agent(), "owner", VOLUME)
    await until(s, "link", "failed")
    snap = s.snap
    assert snap.state == "link", snap.message
    assert snap.url == "https://login.example.com/a?code_challenge=abc&state=s1" and snap.link

    await hub.submit(s, "wrong-code")
    await until(s, "link", "failed", "done")  # asked again
    assert s.snap.state == "link" and "wasn't accepted" in s.snap.message

    await hub.submit(s, "good-code")
    await until(s, "done", "failed")
    assert s.snap.state == "done", s.snap.message
    await s.task
    assert leftovers(mgr, s.id) == []  # container removed
    # The credential landed in the login volume (read back by the agent's own check).
    assert await hub.check(fake_agent(), VOLUME) is True


async def test_cancel_removes_the_container(mgr, hub) -> None:
    s = hub.start(fake_agent(), "owner", VOLUME)
    await until(s, "link")
    assert leftovers(mgr, s.id)  # running now
    await hub.cancel(s)
    assert s.snap.state == "cancelled"
    assert leftovers(mgr, s.id) == []
    assert await hub.check(fake_agent(), VOLUME) is False  # nothing was saved


async def test_expiry_removes_the_container(mgr, hub, monkeypatch) -> None:
    import thinair.agents.login as login

    monkeypatch.setattr(login, "LIFETIME_S", 3)
    s = hub.start(fake_agent(), "owner", VOLUME)
    await until(s, "expired", "failed", timeout=240)
    assert s.snap.state == "expired"
    await asyncio.wait([s.task])
    assert leftovers(mgr, s.id) == []


# ---- the real CLIs, up to the link ------------------------------------------------------------
def test_copy(agent_id: str) -> AgentConfig:
    cfg = load_agents()[0][agent_id]
    copy = cfg.model_copy(update={"id": f"{agent_id}-test"})
    assert copy.auth.shares_login_with is None
    return copy


test_copy.__test__ = False  # a helper, not a test


@pytest.mark.parametrize("agent_id", ["claude", "codex"])
async def test_real_cli_shows_its_link(mgr, agent_id) -> None:
    volume = f"thinair-auth-{agent_id}-test-login"
    mgr.remove_volume(volume)
    hub = LoginHub(DockerLoginBackend(mgr))
    s = hub.start(test_copy(agent_id), "owner", volume)
    try:
        await until(s, "link", "failed", timeout=900)  # first run builds the agent image
        snap = s.snap
        assert snap.state == "link", snap.message
        assert snap.link, snap.url
        if agent_id == "claude":
            assert snap.url.startswith("https://claude.com/cai/oauth/authorize?")
            # PKCE: the code the user pastes is useless without the verifier, which stays in
            # the CLI's memory inside the container.
            assert "code_challenge=" in snap.url and "code_challenge_method=S256" in snap.url
            assert snap.needs_code and snap.user_code is None
        else:
            assert snap.url == "https://auth.openai.com/codex/device"
            assert snap.user_code and "-" in snap.user_code and not snap.needs_code
    finally:
        await hub.cancel(s)
        mgr.remove_volume(volume)
    assert leftovers(mgr, s.id) == []
