"""Signing an agent in from the phone: state machine (canned output) and API (fake terminal).

The canned outputs are the exact byte shapes `claude auth login` (2.1.311) and
`codex login --device-auth` (0.166.0) printed in a terminal (scratch probe, 2026-11-14), with
the state, PKCE challenge and device code replaced. Docker tests: test_agent_login_docker.py.
"""

import asyncio
import json
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from thinair.agents.config import AgentConfigError, load_agents, parse_agent
from thinair.agents.login import (
    MAX_OUTPUT,
    FlowMachine,
    LoginInputError,
    clean,
    link_allowed,
    sanitize_code,
)
from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.config import get_settings
from thinair.db.models import JobEvent, JobKind
from thinair.db.session import session_scope

CLAUDE_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-"
    "5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth"
    "%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference"
    "&code_challenge=CHALLENGE123&code_challenge_method=S256&state=STATE456"
)
CLAUDE_OUT = (
    b"Opening browser to sign in\xe2\x80\xa6\r\nIf the browser didn't open, visit: "
    b"\x1b]8;;" + CLAUDE_URL.encode() + b"\x07\x1b[94m" + CLAUDE_URL.encode() + b"\x1b[39m"
    b"\x1b]8;;\x07\r\nPaste code here if prompted > "
)
CODEX_OUT = (
    b"\r\nWelcome to Codex [v\x1b[90m0.166.0\x1b[0m]\r\n\x1b[90mOpenAI's command-line coding "
    b"agent\x1b[0m\r\n\r\nFollow these steps to sign in with ChatGPT using device code "
    b"authorization:\r\n\r\n1. Open this link in your browser and sign in to your account\r\n"
    b"   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\r\n\r\n2. Enter this one-time code "
    b"\x1b[90m(expires in 15 minutes)\x1b[0m\r\n   \x1b[94mABCD-EFGH1\x1b[0m\r\n\r\n"
    b"\x1b[90mContinue only if you started this login in Codex. If a website or another person "
    b"gave you this code, cancel.\x1b[0m\r\n\r\n"
)
OWNER = "thinair_" + "o" * 40


def agents():
    return load_agents()[0]


def machine(agent_id: str) -> FlowMachine:
    return FlowMachine(agents()[agent_id].auth.login_flow)


def feed_in_pieces(m: FlowMachine, data: bytes, size: int) -> list[str]:
    """Feed like a terminal would: arbitrary cuts, also inside escapes, the URL and UTF-8."""
    import codecs

    dec = codecs.getincrementaldecoder("utf-8")()
    states = []
    for i in range(0, len(data), size):
        m.feed(dec.decode(data[i : i + size]))
        states.append(m.snap.state)
    return states


# ---- parsing --------------------------------------------------------------------------------
def test_clean_removes_links_colours_and_carriage_returns():
    text = clean(CLAUDE_OUT.decode())
    assert "\x1b" not in text and "\x07" not in text and "\r" not in text
    assert f"visit: {CLAUDE_URL}\nPaste code here if prompted > " in text


@pytest.mark.parametrize("size", [1, 7, 64, 4096])
def test_claude_link_then_prompt_in_any_chunking(size):
    m = machine("claude")
    states = feed_in_pieces(m, CLAUDE_OUT, size)
    assert states[-1] == "link"
    s = m.snap
    assert s.url == CLAUDE_URL  # whole, never a prefix cut at a read boundary
    assert "code_challenge=" in s.url and "code_challenge_method=S256" in s.url  # PKCE
    assert s.link is True and s.needs_code is True and s.user_code is None


@pytest.mark.parametrize("size", [1, 13, 4096])
def test_codex_link_and_device_code_in_any_chunking(size):
    m = machine("codex")
    feed_in_pieces(m, CODEX_OUT, size)
    s = m.snap
    assert (s.state, s.url, s.user_code) == (
        "link",
        "https://auth.openai.com/codex/device",
        "ABCD-EFGH1",
    )
    assert s.link is True and s.needs_code is False


def test_link_waits_for_the_prompt():
    m = machine("claude")
    m.feed(CLAUDE_OUT.decode().split("Paste")[0])
    assert m.snap.state == "preparing"  # the link alone: the CLI isn't ready for a code yet
    m.feed("Paste code here if prompted > ")
    assert m.snap.state == "link"


def test_code_is_typed_with_enter_then_verifying_then_done_on_exit_0():
    m = machine("claude")
    m.feed(CLAUDE_OUT.decode())
    assert m.submit("  abc#def\n") == b"abc#def\r"
    assert m.snap.state == "verifying"
    with pytest.raises(LoginInputError) as e:
        m.submit("again")  # nothing typed unless the CLI is at its prompt
    assert e.value.code == "not_waiting_for_code"
    m.feed("\r\nLogin successful.\r\n")
    m.exited(0)
    assert m.snap.state == "verifying"  # the caller confirms with the check command


def test_a_refused_code_asks_again():
    m = machine("claude")
    m.feed(CLAUDE_OUT.decode())
    m.submit("wrong")
    m.feed("\r\nOAuth error: invalid code\r\nPaste code here if prompted > ")
    assert m.snap.state == "link" and m.snap.needs_code
    assert "wasn't accepted" in m.snap.message
    assert m.submit("right") == b"right\r"


def test_failure_shows_the_last_line():
    m = machine("codex")
    m.feed(CODEX_OUT.decode())
    m.feed("Error: device code login is not enabled for this account\r\n")
    m.exited(1)
    assert m.snap.state == "failed"
    assert m.snap.message == "Error: device code login is not enabled for this account"


def test_endless_output_fails_the_session():
    m = machine("claude")
    m.feed("x" * (MAX_OUTPUT + 1))
    assert m.snap.state == "failed"


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://claude.com/cai/oauth/authorize?x=1", True),
        ("https://platform.claude.com/oauth/authorize", True),
        ("http://claude.com/cai/oauth/authorize", False),  # not https
        ("https://claude.com.evil.example/oauth", False),
        ("https://evil.example/?claude.com", False),
        ("https://user@claude.com/", False),
        ("javascript:alert(1)", False),
    ],
)
def test_only_allowlisted_https_links(url, ok):
    assert link_allowed(url, ["claude.com", "claude.ai", "platform.claude.com"]) is ok


def test_a_foreign_url_is_shown_but_not_as_a_link():
    m = machine("claude")
    m.feed("visit: https://phish.example/login\nPaste code here if prompted > ")
    assert m.snap.url == "https://phish.example/login" and m.snap.link is False


@pytest.mark.parametrize(
    ("raw", "out"),
    [("abc", "abc"), (" a\tb\x1b[2J\x00c \n", "abc"), ("a\rb", "ab")],
)
def test_sanitize_code(raw, out):
    assert sanitize_code(raw) == out


@pytest.mark.parametrize("raw", ["", " \n\t", "x" * 2001])
def test_sanitize_code_refuses(raw):
    with pytest.raises(LoginInputError):
        sanitize_code(raw)


def test_login_flow_config_is_validated():
    base = """
id: x
name: X
install: "true"
command: [x, "{task}"]
auth:
  login_command: [x, login]
  login_flow:
    command: [x, login]
    url: '%s'
    url_hosts: [%s]
"""
    assert parse_agent(base % ("https://\\S+", "example.com")).auth.login_flow is not None
    with pytest.raises(AgentConfigError, match="regular expression"):
        parse_agent(base % ("(unclosed", "example.com"))
    with pytest.raises(AgentConfigError, match="host"):
        parse_agent(base % ("https://\\S+", "'*.example.com'"))


def test_builtin_flows():
    a = agents()
    assert a["claude"].auth.login_flow.command == ["claude", "auth", "login"]
    assert a["codex"].auth.login_flow.command == ["codex", "login", "--device-auth"]
    assert a["gemini"].auth.login_flow is None  # full-screen sign-in: terminal only


# ---- API with a fake terminal ----------------------------------------------------------------
class FakeTerminal:
    """Plays the CLI: prints `out`, then on a typed code prints `reply` and exits `code`."""

    def __init__(self, out: bytes, reply: bytes = b"", code: int = 0, exit_after_out: bool = False):
        self._chunks: list[bytes] = [out]
        self._reply, self._code, self._exit_after = reply, code, exit_after_out
        self.typed: list[bytes] = []
        self.exited: int | None = None
        self.closed = False
        self._lock = threading.Lock()

    def read(self, timeout_s: float = 1.0) -> bytes | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if self._chunks:
                    return self._chunks.pop(0)
                if self.exited is not None or self.closed:
                    return b""
            time.sleep(0.01)
        return None

    def write(self, data: bytes) -> bool:
        with self._lock:
            self.typed.append(data)
            self._chunks.append(self._reply)
            self.exited = self._code
        return True

    def exit_code(self) -> int | None:
        return self.exited

    def finish(self, code: int) -> None:  # e.g. Codex: the provider confirmed, the CLI exits
        with self._lock:
            self.exited = code


class FakeBackend:
    def __init__(self) -> None:
        self.terminals: dict[str, FakeTerminal] = {}
        self.next: FakeTerminal | None = None
        self.opened: list[tuple[str, str]] = []  # (agent, volume)
        self.closed: list[str] = []
        self.check_result: bool | None = True
        self.checks: list[str] = []
        self.hold_open = asyncio.Event()
        self.hold_open.set()

    async def open(self, session):
        await self.hold_open.wait()
        self.opened.append((session.cfg.id, session.volume))
        term = self.next or FakeTerminal(CLAUDE_OUT)
        self.next = None
        self.terminals[session.id] = term
        return term

    async def check(self, cfg, volume):
        self.checks.append(volume)
        return self.check_result

    async def close(self, session):
        self.closed.append(session.id)
        t = self.terminals.get(session.id)
        if t is not None:
            t.closed = True


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def owner(monkeypatch, backend):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", OWNER)
    get_settings.cache_clear()
    api_auth._failures.clear()
    app = create_app({JobKind.AGENT: _never, JobKind.PROFILE: _never}, login_backend=backend)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {OWNER}"
        yield c


async def _never(*_a, **_k):  # executors are never used here
    raise AssertionError


def wait_state(client, sid, want, headers=None, timeout=5.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = client.get(f"/api/agent-logins/{sid}", headers=headers or {})
        assert r.status_code == 200, r.text
        last = r.json()
        if last["state"] == want:
            return last
        time.sleep(0.02)
    raise AssertionError(f"state {last} never became {want}")


def add_user(owner, name, admin=False):
    r = owner.post("/api/users", json={"name": name, "is_admin": admin})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_claude_sign_in_end_to_end(owner, backend, caplog):
    caplog.set_level(logging.DEBUG)
    r = owner.post("/api/agents/claude/login")
    assert r.status_code == 201, r.text
    sid = r.json()["session"]
    s = wait_state(owner, sid, "link")
    assert s["url"] == CLAUDE_URL and s["link"] and s["needs_code"]
    assert backend.opened == [("claude", "thinair-auth-claude")]  # the owner's own volume

    r = owner.post(f"/api/agent-logins/{sid}/input", json={"code": "the-code#123"})
    assert r.status_code == 202, r.text
    wait_state(owner, sid, "done")
    assert backend.terminals[sid].typed == [b"the-code#123\r"]
    assert backend.checks == ["thinair-auth-claude"]
    assert sid in backend.closed  # container gone
    agents_ = {a["id"]: a for a in owner.get("/api/agents").json()}
    assert agents_["claude"]["login"] == {"in_app": True, "state": "ok"}
    assert agents_["claude-acp"]["login"] == {"in_app": True, "state": "ok"}  # shares it

    # Nothing about the session is stored or logged: no job events, no link, no code.
    with session_scope() as db:
        assert db.exec(select(JobEvent)).first() is None
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "STATE456" not in logged and "the-code#123" not in logged and "claude.com" not in logged


def test_codex_device_code_completes_by_itself(owner, backend):
    backend.next = term = FakeTerminal(CODEX_OUT)
    sid = owner.post("/api/agents/codex/login").json()["session"]
    s = wait_state(owner, sid, "link")
    assert s["user_code"] == "ABCD-EFGH1" and s["needs_code"] is False
    r = owner.post(f"/api/agent-logins/{sid}/input", json={"code": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_waiting_for_code"
    term.finish(0)  # the user entered the code at OpenAI; the CLI saw it and exited
    wait_state(owner, sid, "done")


def test_check_says_still_logged_out(owner, backend):
    backend.check_result = False
    sid = owner.post("/api/agents/claude/login").json()["session"]
    wait_state(owner, sid, "link")
    owner.post(f"/api/agent-logins/{sid}/input", json={"code": "c"})
    s = wait_state(owner, sid, "failed")
    assert "still isn't logged in" in s["message"]


def test_cli_failure_is_reported(owner, backend):
    backend.next = FakeTerminal(CLAUDE_OUT, reply=b"\r\nLogin failed: invalid_grant\r\n", code=1)
    sid = owner.post("/api/agents/claude/login").json()["session"]
    wait_state(owner, sid, "link")
    owner.post(f"/api/agent-logins/{sid}/input", json={"code": "c"})
    assert wait_state(owner, sid, "failed")["message"] == "Login failed: invalid_grant"
    assert backend.checks == []


def test_events_stream_states_until_finished(owner, backend):
    backend.next = term = FakeTerminal(CODEX_OUT)
    sid = owner.post("/api/agents/codex/login").json()["session"]
    wait_state(owner, sid, "link")
    term.finish(0)
    wait_state(owner, sid, "done")
    with owner.stream("GET", f"/api/agent-logins/{sid}/events") as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    events = [json.loads(ln[6:]) for ln in body.splitlines() if ln.startswith("data: ")]
    assert events[-1]["state"] == "done"  # replays the current state, then the stream ends
    # Each event carries the same fields as GET (the screen renders from either).
    assert events[-1] == owner.get(f"/api/agent-logins/{sid}").json()
    assert events[-1]["name"] == "Codex" and "device code" in events[-1]["help"]


def test_one_sign_in_per_user_and_cancel(owner, backend):
    first = owner.post("/api/agents/claude/login").json()["session"]
    wait_state(owner, first, "link")
    r = owner.post("/api/agents/codex/login")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "login_running" and r.json()["detail"]["session"] == first
    assert owner.delete(f"/api/agent-logins/{first}").status_code == 204
    s = wait_state(owner, first, "cancelled")
    assert s["needs_code"] is False and first in backend.closed
    assert owner.post("/api/agents/codex/login").status_code == 201


def test_server_wide_cap(owner, backend):
    backend.hold_open.clear()  # sessions stay "preparing"
    users = [add_user(owner, f"u{i}") for i in range(3)]
    for h in users:
        assert owner.post("/api/agents/claude/login", headers=h).status_code == 201
    r = owner.post("/api/agents/claude/login")
    assert r.status_code == 429 and r.json()["detail"]["code"] == "too_many_logins"
    backend.hold_open.set()


def test_sessions_are_private_even_from_admins(owner, backend):
    member = add_user(owner, "sam")
    admin = add_user(owner, "ada", admin=True)
    sid = owner.post("/api/agents/claude/login", headers=member).json()["session"]
    wait_state(owner, sid, "link", headers=member)
    assert backend.opened == [("claude", "thinair-auth-claude.sam")]  # sam's own volume
    for who in ({}, admin):  # the owner and an admin
        assert owner.get(f"/api/agent-logins/{sid}", headers=who).status_code == 404
        assert owner.get(f"/api/agent-logins/{sid}/events", headers=who).status_code == 404
        r = owner.post(f"/api/agent-logins/{sid}/input", json={"code": "mine"}, headers=who)
        assert r.status_code == 404
        assert owner.delete(f"/api/agent-logins/{sid}", headers=who).status_code == 404
    assert backend.terminals[sid].typed == []
    assert wait_state(owner, sid, "link", headers=member)["state"] == "link"


def test_unauthenticated_clients_cannot_start_or_see_sign_ins(owner, backend):
    sid = owner.post("/api/agents/claude/login").json()["session"]
    anon = {"Authorization": ""}
    bad = {"Authorization": "Bearer thinair_" + "x" * 40}
    for h in (anon, bad):
        assert owner.post("/api/agents/codex/login", headers=h).status_code == 401
        assert owner.get(f"/api/agent-logins/{sid}", headers=h).status_code == 401
        assert owner.get(f"/api/agent-logins/{sid}/events", headers=h).status_code == 401
        r = owner.post(f"/api/agent-logins/{sid}/input", json={"code": "c"}, headers=h)
        assert r.status_code == 401
        assert owner.delete(f"/api/agent-logins/{sid}", headers=h).status_code == 401
        assert owner.post("/api/agents/claude/login/check", headers=h).status_code == 401
    assert [a for a, _ in backend.opened] == ["claude"]


def test_agents_without_in_app_sign_in(owner):
    r = owner.post("/api/agents/gemini/login")
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["code"] == "no_in_app_login" and d["command"] == "thinair auth gemini"
    assert owner.post("/api/agents/nope/login").status_code == 404
    gemini = next(a for a in owner.get("/api/agents").json() if a["id"] == "gemini")
    assert gemini["login"] == {"in_app": False, "state": "unknown"}


def test_explicit_check(owner, backend):
    backend.check_result = False
    assert owner.post("/api/agents/codex/login/check").json() == {"logged_in": False}
    codex = next(a for a in owner.get("/api/agents").json() if a["id"] == "codex")
    assert codex["login"]["state"] == "missing"
    assert backend.checks == ["thinair-auth-codex"]


def test_code_input_limits(owner, backend):
    sid = owner.post("/api/agents/claude/login").json()["session"]
    wait_state(owner, sid, "link")
    assert (
        owner.post(f"/api/agent-logins/{sid}/input", json={"code": "x" * 2001}).status_code == 422
    )
    r = owner.post(f"/api/agent-logins/{sid}/input", json={"code": "\x1b[2J\r\n"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "empty_code"
    assert backend.terminals[sid].typed == []


def test_expiry(owner, backend, monkeypatch):
    import thinair.agents.login as login

    monkeypatch.setattr(login, "LIFETIME_S", 0.3)
    sid = owner.post("/api/agents/claude/login").json()["session"]
    s = wait_state(owner, sid, "expired")
    assert "timed out" in s["message"] and sid in backend.closed


async def test_cancel_while_the_container_is_being_created_closes_it():
    """The worker thread creating the container outlives a cancel: it must close what it made."""
    from thinair.agents.login import DockerLoginBackend, LoginHub

    release = threading.Event()
    made: list[FakeTerminal] = []

    class SlowMgr:
        def open_terminal(self, spec):
            release.wait(5)
            t = FakeTerminal(CLAUDE_OUT)
            t.close = lambda: setattr(t, "closed", True)
            made.append(t)
            return t

    backend = DockerLoginBackend(SlowMgr())

    async def no_image(cfg):
        return "img"

    async def no_volume(cfg, volume):
        return None

    backend._image, backend._volume = no_image, no_volume
    hub = LoginHub(backend)
    s = hub.start(agents()["codex"], "owner", "thinair-auth-codex-x")  # codex: no egress proxy
    await asyncio.sleep(0.2)  # inside open_terminal now
    await hub.cancel(s)
    assert s.snap.state == "cancelled"
    release.set()
    for _ in range(100):
        if made and made[0].closed:
            break
        await asyncio.sleep(0.02)
    assert made and made[0].closed  # not left running for the janitor
