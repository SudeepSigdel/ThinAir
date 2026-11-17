"""Sign a coding agent in from the phone (onboarding v2, phase 2).

Thinair runs the agent's own login command (`auth.login_flow.command`) in a terminal inside a
hardened container that mounts only that agent's login volume, and relays two things:

* out: the sign-in link (and, for device-code logins, the one-time code to type there);
* in: the code the provider shows after sign-in (Claude Code), typed at the CLI's prompt.

The credential itself is written by the CLI into its own volume; Thinair never reads it. Session
state lives in memory only: no database rows, no job events, and the log records state changes
("link", "done"), never the link or a code. A 16 KiB cap on the output a session keeps; more
than that fails the session (a login prints well under 2 KiB).

Rules (docs/plans/onboarding-v2.md 3.4/4.3): only the user who started a session can see or
answer it (everyone else, admins included, gets "not found"); one session per user, at most
`MAX_SESSIONS` at once; 10 minutes per session; links are tappable only for `https` URLs whose
host is in `login_flow.url_hosts`.
"""

import asyncio
import contextlib
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from thinair.agents.config import AgentConfig, LoginFlow

log = logging.getLogger("thinair.login")

LIFETIME_S = 600  # from the moment the login container starts
PREPARE_S = 1200  # building the agent image the first time can take a while on a small VM
MAX_SESSIONS = 3  # across all users: each one is a container
MAX_OUTPUT = 16 * 1024
MAX_CODE = 2000
KEEP_FINISHED_S = 120  # a finished session stays readable briefly (late SSE connects)
KEEPALIVE_S = 15

FINAL = ("done", "failed", "expired", "cancelled")

# Terminal escape sequences: OSC (e.g. OSC 8 hyperlinks, ended by BEL or ST), CSI (colours,
# cursor moves) and two-byte escapes.
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: str) -> str:
    """Output as a person would read it: escapes removed, CRLF -> LF, no control characters."""
    text = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", text)


def link_allowed(url: str, hosts: list[str]) -> bool:
    """Only https links to the agent's own sign-in hosts are shown as links."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and host in hosts and not parts.username and not parts.password


def sanitize_code(code: str) -> str:
    """What may be typed at the CLI's prompt: no escapes, control characters or whitespace
    (sign-in codes have none; a stray newline would answer the prompt early)."""
    code = re.sub(r"[\x00-\x20\x7f]", "", _ANSI.sub("", code))
    if not code:
        raise LoginInputError("empty_code", "Paste the code the sign-in page showed you.")
    if len(code) > MAX_CODE:
        raise LoginInputError("code_too_long", "That is too long to be a sign-in code.")
    return code


class LoginInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


# ---- pure state machine (unit-tested with canned output) -------------------------------------
@dataclass
class Snapshot:
    state: str = "preparing"  # preparing|link|verifying|done|failed|expired|cancelled
    url: str | None = None
    link: bool = False  # url may be shown as a link (allowlisted https host)
    user_code: str | None = None  # device-code logins: type this at the provider
    needs_code: bool = False  # the CLI waits for the provider's code
    message: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "url": self.url,
            "link": self.link,
            "user_code": self.user_code,
            "needs_code": self.needs_code,
            "message": self.message,
        }


class FlowMachine:
    """Turns the login command's terminal output into states. Pure: no I/O."""

    def __init__(self, flow: LoginFlow) -> None:
        self.flow = flow
        self._url = re.compile(flow.url)
        self._code_out = re.compile(flow.code_out) if flow.code_out else None
        self._code_in = re.compile(flow.code_in) if flow.code_in else None
        self._raw = ""
        self._prompts = 0  # prompts seen so far
        self._answered = 0  # prompts answered
        self.snap = Snapshot()

    @property
    def text(self) -> str:
        return clean(self._raw)

    def feed(self, chunk: str) -> bool:
        """Add output. Returns True when the snapshot changed."""
        if self.snap.state in FINAL:
            return False
        before = self.snap.public()
        self._raw += chunk
        if len(self._raw) > MAX_OUTPUT:
            self.fail("The sign-in printed more than expected, so Thinair stopped it.")
            return True
        text = self.text
        s = self.snap
        if s.url is None:
            s.url = _complete(self._url, text)
            if s.url is not None:
                s.url = s.url.rstrip(").,;'\"")
                s.link = link_allowed(s.url, self.flow.url_hosts)
        if self._code_out is not None and s.user_code is None:
            s.user_code = _complete(self._code_out, text)
        if self._code_in is not None:
            self._prompts = len(self._code_in.findall(text))
        ready = (
            s.url is not None
            and (self._code_out is None or s.user_code is not None)
            and (self._code_in is None or self._prompts > 0)
        )
        if ready and s.state == "preparing":
            s.state = "link"
            s.needs_code = self._code_in is not None
        elif s.state == "verifying" and self._prompts > self._answered:
            # Asked again: the code was refused (or expired) and the CLI wants another one.
            s.state, s.needs_code = "link", True
            s.message = (
                "That code wasn't accepted. Codes expire quickly; open the sign-in page again."
            )
        return before != s.public()

    @property
    def awaiting_code(self) -> bool:
        return self.snap.state == "link" and self.snap.needs_code and self._prompts > self._answered

    def submit(self, code: str) -> bytes:
        """The bytes to type at the prompt (code + Enter); moves to "verifying"."""
        if not self.awaiting_code:
            raise LoginInputError("not_waiting_for_code", "The sign-in isn't waiting for a code.")
        clean_code = sanitize_code(code)
        self._answered = self._prompts
        self.snap.state, self.snap.needs_code, self.snap.message = "verifying", False, None
        return clean_code.encode() + b"\r"

    def exited(self, code: int | None) -> None:
        """The login command ended. 0 -> verifying (the caller then checks the login)."""
        if self.snap.state in FINAL:
            return
        if code == 0:
            self.snap.state, self.snap.needs_code, self.snap.message = "verifying", False, None
        else:
            self.fail(self.last_line() or "The sign-in ended without logging in.")

    def last_line(self) -> str | None:
        lines = [ln.strip() for ln in self.text.splitlines() if ln.strip()]
        return lines[-1][:300] if lines else None

    def fail(self, message: str) -> None:
        self.snap.state, self.snap.needs_code, self.snap.message = "failed", False, message

    def finish(self, state: str, message: str | None = None) -> None:
        self.snap.state, self.snap.needs_code, self.snap.message = state, False, message


def _complete(pattern: re.Pattern[str], text: str) -> str | None:
    """First match that is followed by more output (so a link cut between two reads is never
    taken half). Group 1 if the pattern has one."""
    for m in pattern.finditer(text):
        if m.end() < len(text) and text[m.end()].isspace():
            return m.group(1) if pattern.groups else m.group(0)
    return None


# ---- the terminal behind a session ------------------------------------------------------------
class LoginTerminal(Protocol):
    def read(self, timeout_s: float = 1.0) -> bytes | None: ...
    def write(self, data: bytes) -> bool: ...
    def exit_code(self) -> int | None: ...


class LoginBackend(Protocol):
    """Where login commands run. `DockerLoginBackend` in production; fakes in tests."""

    async def open(self, session: "LoginSession") -> LoginTerminal: ...
    async def check(self, cfg: AgentConfig, volume: str) -> bool | None: ...
    async def close(self, session: "LoginSession") -> None: ...


@dataclass
class LoginSession:
    id: str
    cfg: AgentConfig  # the agent whose image and login flow are used
    owner: str  # "owner" or the user's id
    volume: str  # this user's login volume for the agent
    machine: FlowMachine
    created: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    terminal: LoginTerminal | None = None
    backend_state: dict[str, Any] = field(default_factory=dict)  # for the backend's own use
    task: asyncio.Task[None] | None = None
    version: int = 0
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def snap(self) -> Snapshot:
        return self.machine.snap

    def public(self) -> dict[str, Any]:
        """What the session's user sees (GET and every SSE event): the same fields each time."""
        flow = self.cfg.auth.login_flow
        return {
            "session": self.id,
            "agent": self.cfg.id,
            "name": self.cfg.name,
            "help": flow.help if flow else None,
            **self.snap.public(),
        }

    @property
    def job_label(self) -> str:
        return f"login-{self.id}"

    def publish(self) -> None:
        self.version += 1
        self._changed.set()
        self._changed = asyncio.Event()
        log.info("agent login %s (%s): %s", self.id, self.cfg.id, self.snap.state)

    async def changed(self, version: int, timeout_s: float) -> None:
        if self.version != version:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout_s)

    async def events(self) -> AsyncIterator[str]:
        """SSE: the current state now, then every change, until a final state."""
        sent = -1
        while True:
            if self.version != sent:
                sent = self.version
                yield f"event: state\ndata: {json.dumps(self.public())}\n\n"
                if self.snap.state in FINAL:
                    return
            else:
                yield ": keep-alive\n\n"
            await self.changed(sent, KEEPALIVE_S)


class LoginBusy(Exception):
    def __init__(self, session: LoginSession) -> None:
        super().__init__("a sign-in is already running")
        self.session = session


class TooManyLogins(Exception):
    pass


class LoginHub:
    """All sign-in sessions of this server, in memory (`app.state.logins`)."""

    def __init__(self, backend: LoginBackend) -> None:
        self.backend = backend
        self._sessions: dict[str, LoginSession] = {}
        # Last known login state per login volume: True/False from sessions and checks.
        self.known: dict[str, bool] = {}
        self._checking: set[str] = set()

    def _prune(self) -> None:
        now = time.monotonic()
        for sid, s in list(self._sessions.items()):
            if s.finished_at is not None and now - s.finished_at > KEEP_FINISHED_S:
                del self._sessions[sid]

    def active(self) -> list[LoginSession]:
        return [s for s in self._sessions.values() if s.finished_at is None]

    def start(self, cfg: AgentConfig, owner: str, volume: str) -> LoginSession:
        """Start a sign-in for `owner`. Raises LoginBusy (their running one) or TooManyLogins."""
        if cfg.auth.login_flow is None:
            raise ValueError(f"{cfg.id} has no login_flow")
        self._prune()
        mine = [s for s in self.active() if s.owner == owner]
        if mine:
            raise LoginBusy(mine[0])
        if len(self.active()) >= MAX_SESSIONS:
            raise TooManyLogins
        s = LoginSession(
            id=secrets.token_hex(8),
            cfg=cfg,
            owner=owner,
            volume=volume,
            machine=FlowMachine(cfg.auth.login_flow),
        )
        self._sessions[s.id] = s
        s.task = asyncio.create_task(self._run(s), name=f"thinair-login-{s.id}")
        return s

    def get(self, sid: str, owner: str) -> LoginSession | None:
        """The session, only for the user who started it (None for everyone else)."""
        self._prune()
        s = self._sessions.get(sid)
        return s if s is not None and s.owner == owner else None

    async def submit(self, s: LoginSession, code: str) -> None:
        data = s.machine.submit(code)  # raises LoginInputError
        term = s.terminal
        if term is None or not await asyncio.to_thread(term.write, data):
            s.machine.fail("The sign-in stopped before the code could be sent. Start again.")
        s.publish()

    async def cancel(self, s: LoginSession) -> None:
        if s.task is not None and not s.task.done():
            s.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await s.task

    async def check(self, cfg: AgentConfig, volume: str) -> bool | None:
        """Run the agent's check command once (None = it has none, or it couldn't run)."""
        if volume in self._checking:
            raise TooManyLogins
        self._checking.add(volume)
        try:
            result = await self.backend.check(cfg, volume)
        finally:
            self._checking.discard(volume)
        if result is not None:
            self.known[volume] = result
        return result

    async def close_all(self) -> None:
        for s in self.active():
            await self.cancel(s)

    async def _run(self, s: LoginSession) -> None:
        m = s.machine
        decoder = _Utf8()
        try:
            try:
                s.terminal = await asyncio.wait_for(self.backend.open(s), PREPARE_S)
            except TimeoutError:
                m.fail("Getting the agent ready took too long. Try again.")
                return
            deadline = time.monotonic() + LIFETIME_S
            term = s.terminal
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    m.finish("expired", "This sign-in timed out. Start again.")
                    return
                chunk = await asyncio.to_thread(term.read, min(1.0, left))
                if chunk is None:
                    continue
                if chunk == b"":
                    break
                if m.feed(decoder.decode(chunk)):
                    s.publish()
                if m.snap.state in FINAL:
                    return
            m.exited(await _exit_code(term))
            if m.snap.state == "verifying":
                s.publish()
                ok = await self.backend.check(s.cfg, s.volume)
                if ok is False:
                    m.fail("The sign-in finished, but the agent still isn't logged in. Try again.")
                    self.known[s.volume] = False
                else:
                    m.finish("done")
                    self.known[s.volume] = True
                    from thinair.setup_state import OWNER_KEY, mark

                    mark("agent_connected", None if s.owner == OWNER_KEY else s.owner)
        except asyncio.CancelledError:
            m.finish("cancelled", "Sign-in cancelled.")
            raise
        except Exception as e:  # noqa: BLE001 - shown to the user; details in the server log
            log.warning("agent login %s (%s) failed: %s", s.id, s.cfg.id, type(e).__name__)
            m.fail(_friendly(e))
        finally:
            s.finished_at = time.monotonic()
            with contextlib.suppress(Exception):
                await asyncio.shield(self.backend.close(s))
            s.publish()


async def _exit_code(term: LoginTerminal) -> int | None:
    """The exit code right after the output ended (Docker may need a moment to record it)."""
    for _ in range(20):
        code = await asyncio.to_thread(term.exit_code)
        if code is not None:
            return code
        await asyncio.sleep(0.25)
    return None


def _friendly(e: Exception) -> str:
    from thinair.sandbox.spec import SandboxError

    if isinstance(e, SandboxError):
        return f"Couldn't start the sign-in: {e}"
    return "The sign-in stopped unexpectedly. Try again; the server log has details."


class _Utf8:
    """Decode a byte stream that may split a character between reads."""

    def __init__(self) -> None:
        import codecs

        self._d = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def decode(self, data: bytes) -> str:
        return self._d.decode(data)


# ---- Docker ----------------------------------------------------------------------------------
class DockerLoginBackend:
    """Runs login commands in the agent's image, like `thinair auth`, but with a terminal that
    Thinair holds instead of the user's. Same hardening as every sandbox; only the login volume
    is mounted; `env_passthrough` tokens are not passed; behind the agent's egress allowlist."""

    def __init__(self, mgr: Any = None) -> None:
        self._mgr = mgr
        self._lock = threading.Lock()

    @property
    def mgr(self) -> Any:
        if self._mgr is None:
            from thinair.sandbox.manager import SandboxManager

            self._mgr = SandboxManager()
        return self._mgr

    def _spec(self, cfg: AgentConfig, image: str, volume: str, command: list[str], **kw: Any):  # noqa: ANN202
        from thinair.sandbox.spec import SandboxSpec, VolumeMount

        env = {**cfg.env, "HOME": "/tmp", "TERM": "xterm-256color"}  # noqa: S108
        env.update(kw.pop("env", {}))
        return SandboxSpec(
            image=image,
            command=command,
            workdir="/tmp",  # noqa: S108
            env=env,
            mounts=[VolumeMount(volume=volume, path=cfg.auth.config_dir)],
            cpus=1,
            memory_bytes=768 * 1024**2,
            pids_limit=512,
            tmp_noexec=False,  # the CLIs unpack helpers there, as in agent runs
            **kw,
        )

    async def _image(self, cfg: AgentConfig) -> str:
        from thinair.agents.cli_adapter import ensure_agent_image

        return await ensure_agent_image(self.mgr, cfg, "node", lambda _line: None)

    async def _volume(self, cfg: AgentConfig, volume: str) -> None:
        # Persistent and unmanaged (the janitor never removes logins), labelled like agent runs do.
        await asyncio.to_thread(
            self.mgr.ensure_persistent_volume, volume, {"thinair.auth": cfg.login_id}
        )

    def _gateway(self, cfg: AgentConfig, job: str):  # noqa: ANN202
        from thinair.config import get_settings
        from thinair.sandbox.egress import EgressGateway

        s = get_settings()
        if cfg.egress is None or s.agent_egress != "allowlist" or not cfg.network:
            return None
        extra = [h.strip() for h in s.agent_egress_extra_hosts.split(",") if h.strip()]
        return EgressGateway(self.mgr, job, [*cfg.egress, *extra])

    async def _start(self, gw: Any) -> None:
        """Start the allowlisting proxy (it runs in the python sandbox image, which agent runs
        get from workspace preparation; a sign-in has none, so make sure it exists)."""
        from thinair.project.prepare import ensure_image

        await ensure_image(self.mgr, "python", lambda _line: None)
        await gw.start()

    async def open(self, session: LoginSession) -> LoginTerminal:
        cfg, flow = session.cfg, session.cfg.auth.login_flow
        assert flow is not None
        image = await self._image(cfg)
        await self._volume(cfg, session.volume)
        gw = self._gateway(cfg, session.job_label)
        net: dict[str, Any] = {"network": cfg.network}
        if gw is not None:
            session.backend_state["gateway"] = gw
            await self._start(gw)
            net = {"network_name": gw.network, "env": gw.env}
        spec = self._spec(
            cfg,
            image,
            session.volume,
            flow.command,
            job_id=session.job_label,
            stdin=True,
            tty=True,
            timeout_s=LIFETIME_S + 60,
            **net,
        )
        session.backend_state["spec"] = spec
        return await asyncio.to_thread(self._open_terminal, session, spec)

    def _open_terminal(self, session: LoginSession, spec: Any) -> Any:
        """In a worker thread. If the session was closed meanwhile (cancelled while the container
        was being created), the awaiting task is gone: close the terminal here, or it would run
        until the janitor's next sweep."""
        term = self.mgr.open_terminal(spec)
        with self._lock:
            if not session.backend_state.get("closed"):
                session.backend_state["terminal"] = term
                return term
        term.close()
        return term

    async def check(self, cfg: AgentConfig, volume: str) -> bool | None:
        check = cfg.auth.check_command
        if not check:
            return None
        image = await self._image(cfg)
        await self._volume(cfg, volume)
        job = f"login-check-{secrets.token_hex(6)}"
        gw = self._gateway(cfg, job)
        net: dict[str, Any] = {"network": cfg.network}
        try:
            if gw is not None:
                await self._start(gw)
                net = {"network_name": gw.network, "env": gw.env}
            spec = self._spec(cfg, image, volume, check, job_id=job, timeout_s=90, **net)
            res, _lines = await self.mgr.run_capture(spec)
        finally:
            if gw is not None:
                await gw.stop()
        if res.timed_out or res.exit_code is None:
            return None
        return res.exit_code == 0

    async def close(self, session: LoginSession) -> None:
        with self._lock:
            session.backend_state["closed"] = True
            term = session.backend_state.pop("terminal", None) or session.terminal
        if term is not None and hasattr(term, "close"):
            await asyncio.to_thread(term.close)
        gw = session.backend_state.pop("gateway", None)
        if gw is not None:
            await gw.stop()
