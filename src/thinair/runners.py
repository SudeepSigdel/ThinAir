"""Remote runners: other machines that run profile jobs for this server.

A runner *connects to the server* (so it can sit behind a home router), proves itself with its
own secret, and long-polls for work: `POST /runner/v1/claim`. The server never connects to it.

What a runner gets: the repository URL and ref of a profile job, and whether the job's owner may
use a GitHub token. It never receives the server's GitHub token, agent logins, API keys or any
other user's data; a private repository works only if the runner has its own
`THINAIR_GITHUB_TOKEN`. What it can do wrong: report a false profile for the jobs it claims.
Only profile jobs are ever handed out (agents need logins; pull requests need the server's token).

Server side, a job sent to runners is "executed" by `remote_executor` on a separate, cheap set of
job workers that just wait: it offers the job to the `RunnerHub`, waits for a runner to claim it,
then for the runner's result. Events the runner posts go straight into the job's event log, so
the phone sees a remote job exactly like a local one. Cancel and the job time limit work as for
local jobs (the runner learns about a cancel in the reply to its next post). A runner that stops
talking for `runner_lease_s` loses the job (`runner_lost`).
"""

import asyncio
import hashlib
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

from sqlmodel import Session, select

from thinair.config import get_settings
from thinair.db.models import Job, JobStatus, Runner, utcnow
from thinair.jobs.service import ExecResult, JobContext
from thinair.sandbox.manager import SandboxManager

RUNNER_TOKEN_PREFIX = "thinair_runner_"  # noqa: S105
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
CLAIM_WAIT_MAX_S = 25.0  # one long-poll; below common proxy timeouts (Caddy, Cloudflare)
PROTOCOL = 1


class RunnerError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


# ---- runner rows -----------------------------------------------------------------------------
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_runner(session: Session, name: str) -> tuple[Runner, str]:
    """Register a runner; returns it and its secret (shown once, only the hash is kept)."""
    name = name.strip()
    if not NAME_RE.match(name):
        raise RunnerError(
            "bad_name", "Runner names are 1-32 lowercase letters, digits, '-' or '_'."
        )
    if session.exec(select(Runner).where(Runner.name == name)).first() is not None:
        raise RunnerError("runner_exists", f"A runner called {name!r} already exists.")
    token = RUNNER_TOKEN_PREFIX + secrets.token_urlsafe(32)
    runner = Runner(name=name, token_hash=_hash(token))
    session.add(runner)
    session.commit()
    session.refresh(runner)
    return runner, token


def list_runners(session: Session) -> list[Runner]:
    return list(session.exec(select(Runner).order_by(Runner.created_at)))  # type: ignore[arg-type]


def disable_runner(session: Session, name: str) -> Runner:
    runner = session.exec(select(Runner).where(Runner.name == name)).first()
    if runner is None:
        raise RunnerError("not_found", f"No runner called {name!r}.")
    if runner.disabled_at is None:
        runner.disabled_at = utcnow()
        session.add(runner)
        session.commit()
    return runner


def verify_runner(session: Session, token: str) -> Runner | None:
    """The active runner owning `token`, or None. User API tokens never match (other table)."""
    if not token.startswith(RUNNER_TOKEN_PREFIX):
        return None
    runner = session.exec(select(Runner).where(Runner.token_hash == _hash(token))).first()
    if runner is None or runner.disabled_at is not None:
        return None
    return runner


# ---- the hub: pending jobs, claims and leases (in memory, single server process) -------------
@dataclass
class Lease:
    job_id: str
    github_auth: bool
    runner_id: str | None = None
    runner_name: str | None = None
    last_contact: float = field(default_factory=time.monotonic)
    contacted: bool = False  # the runner posted something after claiming (it really has it)
    claimed: asyncio.Event = field(default_factory=asyncio.Event)
    done: "asyncio.Future[ExecResult] | None" = None


class RunnerHub:
    def __init__(self, online_window_s: float = 60.0) -> None:
        self._pending: deque[str] = deque()
        self._leases: dict[str, Lease] = {}
        self._wake = asyncio.Condition()
        self._polling: dict[str, int] = {}  # runner id -> open claim requests
        self._seen: dict[str, float] = {}
        self._online_window = online_window_s

    # Server side --------------------------------------------------------------------------
    async def offer(self, job_id: str, *, github_auth: bool) -> Lease:
        lease = Lease(job_id, github_auth, done=asyncio.get_running_loop().create_future())
        self._leases[job_id] = lease
        self._pending.append(job_id)
        async with self._wake:
            self._wake.notify_all()
        return lease

    def withdraw(self, job_id: str) -> None:
        """The job is over on the server (done, cancelled, timed out): forget it. The runner's
        next post for it is answered with `cancel: true`."""
        self._leases.pop(job_id, None)
        try:
            self._pending.remove(job_id)
        except ValueError:
            pass

    def has_idle_runner(self) -> bool:
        """Some runner is waiting for work right now (and not already promised a pending job)."""
        return sum(1 for n in self._polling.values() if n > 0) > len(self._pending)

    def online(self) -> dict[str, float]:
        cutoff = time.monotonic() - self._online_window
        return {rid: t for rid, t in self._seen.items() if t >= cutoff}

    # Runner side (called by the API routes) -----------------------------------------------
    def touch(self, runner_id: str) -> None:
        self._seen[runner_id] = time.monotonic()

    async def claim(self, runner_id: str, runner_name: str, wait_s: float) -> Lease | None:
        """Hand the oldest pending job to this runner, waiting up to `wait_s` for one."""
        self.touch(runner_id)
        self._polling[runner_id] = self._polling.get(runner_id, 0) + 1
        deadline = time.monotonic() + max(0.0, min(wait_s, CLAIM_WAIT_MAX_S))
        try:
            async with self._wake:
                while not self._pending:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return None
                    try:
                        await asyncio.wait_for(self._wake.wait(), left)
                    except TimeoutError:
                        return None
                lease = self._leases[self._pending.popleft()]
                lease.runner_id, lease.runner_name = runner_id, runner_name
                lease.last_contact = time.monotonic()
                lease.claimed.set()
                return lease
        finally:
            self._polling[runner_id] -= 1
            self.touch(runner_id)

    def lease_for(self, job_id: str, runner_id: str) -> Lease | None:
        """The live lease on `job_id` if this runner holds it (touches it)."""
        self.touch(runner_id)
        lease = self._leases.get(job_id)
        if lease is None or lease.runner_id != runner_id:
            return None
        lease.last_contact = time.monotonic()
        lease.contacted = True
        return lease

    async def reoffer(self, lease: Lease) -> None:
        """A claim whose runner never reported back (e.g. it restarted mid-request): put the
        job back at the front of the line."""
        if self._leases.get(lease.job_id) is not lease:
            return
        lease.runner_id = lease.runner_name = None
        lease.claimed.clear()
        self._pending.appendleft(lease.job_id)
        async with self._wake:
            self._wake.notify_all()


# ---- the executor for jobs sent to runners ---------------------------------------------------
def make_remote_executor(hub: RunnerHub):  # noqa: ANN201
    async def remote_executor(ctx: JobContext, _mgr: SandboxManager | None) -> ExecResult:
        from thinair.jobs.executors import owner_github_auth

        settings = get_settings()
        auto = settings.runner_placement == "auto"
        lease = await hub.offer(ctx.job.id, github_auth=owner_github_auth(ctx.job))
        try:
            ctx.log("[thinair] waiting for a runner to pick up this job")
            assert lease.done is not None
            check_every = min(5.0, settings.runner_lease_s / 2)
            while True:  # one pass per claim; a claim that never got going is offered again
                try:
                    if auto:
                        await asyncio.wait_for(
                            lease.claimed.wait(), settings.runner_claim_timeout_s
                        )
                    else:
                        await lease.claimed.wait()
                except TimeoutError:
                    hub.withdraw(ctx.job.id)
                    ctx.log("[thinair] no runner picked it up; running it on this server instead")
                    return ExecResult(JobStatus.QUEUED)  # JobService re-queues it locally
                name = lease.runner_name
                _set_runner(ctx.job.id, lease.runner_id)
                ctx.log(f"[thinair] running on runner {name!r}")
                while True:
                    try:
                        return await asyncio.wait_for(asyncio.shield(lease.done), check_every)
                    except TimeoutError:
                        if time.monotonic() - lease.last_contact > settings.runner_lease_s:
                            break
                if lease.contacted:
                    return ExecResult(
                        JobStatus.FAILED,
                        "runner_lost",
                        f"Runner {name!r} stopped responding. Retry the job.",
                    )
                ctx.log(f"[thinair] runner {name!r} never started it; offering it again")
                _set_runner(ctx.job.id, None)
                await hub.reoffer(lease)
        finally:
            hub.withdraw(ctx.job.id)

    return remote_executor


def _set_runner(job_id: str, runner_id: str | None) -> None:
    from thinair.db.session import session_scope

    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is not None:
            job.runner_id = runner_id
            s.add(job)
            s.commit()
