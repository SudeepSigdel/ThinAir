"""Background job execution: an in-memory queue, a few workers, and clean state transitions.

Idle cost: workers block on `queue.get()`; there are no timers or polling loops. The queue lives
in memory, but jobs are rows in SQLite, so on restart queued jobs are re-queued and jobs that
were running are marked failed ("interrupted") rather than silently lost.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlmodel import select

from thinair.db.models import EventType, Job, JobKind, JobStatus, Repo, utcnow
from thinair.db.session import session_scope
from thinair.jobs.events import TERMINAL_STATUSES, EventStore
from thinair.project.prepare import GitSource, PrepareError
from thinair.sandbox import janitor
from thinair.sandbox.manager import SandboxManager
from thinair.sandbox.spec import SandboxError

log = logging.getLogger("thinair.jobs")


@dataclass
class ExecResult:
    status: JobStatus = JobStatus.SUCCEEDED
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class JobContext:
    job: Job
    repo: Repo
    events: EventStore

    def log(self, line: str, stream: str = "output") -> None:
        self.events.emit(self.job.id, EventType.LOG, {"line": line}, stream)

    def emit(self, type_: EventType, data: dict[str, Any]) -> None:
        self.events.emit(self.job.id, type_, data)


Executor = Callable[[JobContext, SandboxManager | None], Awaitable[ExecResult]]


class JobService:
    def __init__(
        self,
        executors: dict[JobKind, Executor],
        *,
        events: EventStore | None = None,
        mgr: SandboxManager | None = None,
        workers: int = 1,
        job_timeout_s: float = 3600,
        sweep_on_start: bool = True,
        notifier: "Callable[[str], Awaitable[None]] | None" = None,
        remote_executor: Executor | None = None,
        goes_remote: Callable[[Job], bool] | None = None,
        remote_workers: int = 8,
    ) -> None:
        """`remote_executor` runs the jobs for which `goes_remote(job)` is true, on their own
        `remote_workers` (they only wait for a remote runner, so they don't count against
        `workers`). It may return status QUEUED to send the job back to the local queue."""
        self.events = events or EventStore()
        self._executors = executors
        self._mgr = mgr or SandboxManager()
        self._n_workers = max(1, workers)
        self._timeout_s = job_timeout_s
        self._sweep = sweep_on_start
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._remote_queue: asyncio.Queue[str] = asyncio.Queue()
        self._remote_executor = remote_executor
        self._goes_remote = goes_remote
        self._n_remote = max(1, remote_workers) if remote_executor else 0
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, asyncio.Task[None]] = {}
        self._user_cancelled: set[str] = set()
        self._stopping = False
        self._notifier = notifier
        self._notifications: set[asyncio.Task[None]] = set()

    # ---- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        if self._sweep:
            try:
                await asyncio.to_thread(janitor.sweep, self._mgr.client)
            except Exception as e:  # Docker may simply not be running yet
                log.warning("startup cleanup skipped: %s", e)
        self._recover()
        self._workers = [
            asyncio.create_task(self._worker(self._queue), name=f"thinair-worker-{i}")
            for i in range(self._n_workers)
        ] + [
            asyncio.create_task(self._worker(self._remote_queue), name=f"thinair-remote-{i}")
            for i in range(self._n_remote)
        ]

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._running.values()):
            task.cancel()
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, *self._running.values(), return_exceptions=True)
        self._workers.clear()
        self.events.flush()

    def _recover(self) -> None:
        with session_scope() as s:
            jobs = list(
                s.exec(select(Job).where(Job.status.in_([JobStatus.RUNNING, JobStatus.QUEUED])))
            )  # type: ignore[attr-defined]
            jobs.sort(key=lambda j: j.created_at)
            for j in jobs:
                if j.status == JobStatus.RUNNING:
                    self._finish_row(
                        j.id,
                        JobStatus.FAILED,
                        "interrupted",
                        "Thinair restarted while this job was running. Retry it.",
                    )
                else:
                    self._route(j)

    # ---- API used by routes --------------------------------------------------------------
    def submit(
        self,
        repo_url: str,
        kind: JobKind = JobKind.PROFILE,
        *,
        task: str | None = None,
        agent_id: str | None = None,
        ref: str | None = None,
        limits: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> Job:
        """Create a queued job owned by `user_id` (None = the owner). Raises PrepareError for a
        bad repo URL or ref."""
        source = GitSource(repo_url.strip().rstrip("/"), ref)
        source.validate()
        if kind not in self._executors:
            raise PrepareError(
                "unsupported_job", f"Jobs of kind {kind.value!r} are not available yet"
            )
        from thinair.users import too_many_jobs

        with session_scope() as s:
            refusal = too_many_jobs(s, user_id)
            if refusal:
                raise PrepareError("too_many_jobs", refusal)
            repo = s.exec(select(Repo).where(Repo.url == source.url)).first()
            if repo is None:
                name = source.url.split("/")[-1].removesuffix(".git")
                repo = Repo(url=source.url, name=name)
                s.add(repo)
                s.commit()
            job = Job(
                repo_id=repo.id,
                kind=kind,
                task=task,
                agent_id=agent_id,
                ref=ref,
                limits=limits or {},
                user_id=user_id,
            )
            s.add(job)
            s.commit()
        return self._enqueue(job)

    def submit_pr(self, agent_job_id: str) -> Job:
        """Queue a `pr` job for a finished agent job. Raises PRError if that isn't possible."""
        from thinair.db.models import JobResult
        from thinair.jobs.pr import PRError, check_pr_possible, require_token
        from thinair.users import github_token_allowed, job_user, too_many_jobs

        if JobKind.PR not in self._executors:
            raise PRError("unsupported_job", "Opening pull requests is not available.")
        require_token()  # fail fast on the phone instead of in the job log
        with session_scope() as s:
            parent = s.get(Job, agent_job_id)
            result = s.get(JobResult, agent_job_id)
            check_pr_possible(parent, result)
            assert parent is not None and result is not None
            if not github_token_allowed(job_user(s, parent)):
                raise PRError("github_not_allowed", GITHUB_NOT_ALLOWED)
            refusal = too_many_jobs(s, parent.user_id)
            if refusal:
                raise PRError("too_many_jobs", refusal)
            if result.pr_url:
                raise PRError("pr_exists", f"A pull request is already open: {result.pr_url}")
            active = s.exec(
                select(Job).where(
                    Job.parent_job_id == agent_job_id,
                    Job.kind == JobKind.PR,
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),  # type: ignore[attr-defined]
                )
            ).first()
            if active is not None:
                raise PRError("pr_in_progress", "A pull request is already being opened.")
            job = Job(
                repo_id=parent.repo_id,
                kind=JobKind.PR,
                parent_job_id=parent.id,
                agent_id=parent.agent_id,
                task=parent.task,
                ref=parent.ref,
                commit_sha=parent.commit_sha,
                user_id=parent.user_id,
            )
            s.add(job)
            s.commit()
        return self._enqueue(job)

    def _enqueue(self, job: Job) -> Job:
        self.events.emit(job.id, EventType.STATUS, {"status": JobStatus.QUEUED.value})
        self._route(job)
        return job

    def _route(self, job: Job) -> None:
        remote = self._remote_executor is not None and self._goes_remote is not None
        if remote and self._goes_remote(job):  # type: ignore[misc]
            self._remote_queue.put_nowait(job.id)
        else:
            self._queue.put_nowait(job.id)

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job. Returns False if it is unknown or already finished."""
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return False
            status = job.status
        if status == JobStatus.QUEUED:
            self._finish(job_id, JobStatus.CANCELLED, "cancelled", "Cancelled before it started")
            return True
        task = self._running.get(job_id)
        if task is None:
            return False
        self._user_cancelled.add(job_id)
        task.cancel()
        return True

    # ---- internals -----------------------------------------------------------------------
    async def _worker(self, queue: "asyncio.Queue[str]") -> None:
        remote = queue is self._remote_queue
        while True:
            job_id = await queue.get()
            try:
                task = asyncio.create_task(
                    self._execute(job_id, remote), name=f"thinair-job-{job_id}"
                )
                self._running[job_id] = task
                try:
                    # `wait`, not `await task`: awaiting would forward our own cancellation into the
                    # job task, which swallows it to record the final status, so we would never
                    # learn that we were cancelled.
                    await asyncio.wait([task])
                    if not task.cancelled() and task.exception():
                        log.error("job %s task failed", job_id, exc_info=task.exception())
                except asyncio.CancelledError:  # the worker itself was cancelled (shutdown)
                    task.cancel()
                    await asyncio.wait([task])  # let the job record that it was interrupted
                    raise
                finally:
                    self._running.pop(job_id, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job %s crashed the worker loop", job_id)
            finally:
                queue.task_done()

    async def _execute(self, job_id: str, remote: bool = False) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None or job.status != JobStatus.QUEUED:
                return  # cancelled while waiting in the queue
            repo = s.get(Repo, job.repo_id)
            job.status = JobStatus.RUNNING
            job.started_at = utcnow()
            s.add(job)
            s.commit()
        self.events.emit(job_id, EventType.STATUS, {"status": JobStatus.RUNNING.value})
        ctx = JobContext(job=job, repo=repo, events=self.events)  # type: ignore[arg-type]
        executor = self._remote_executor if remote else self._executors[job.kind]
        timeout = asyncio.timeout(self._timeout_s)
        requeued = False
        try:
            async with timeout:
                res = await executor(ctx, self._mgr)  # type: ignore[misc]
            if remote and res.status == JobStatus.QUEUED:
                requeued = self._requeue_locally(job_id)
            else:
                self._finish(job_id, res.status, res.error_code, res.error_message)
        except TimeoutError:
            if timeout.expired():
                self._finish(
                    job_id,
                    JobStatus.TIMEOUT,
                    "timeout",
                    f"The job exceeded its time limit of {int(self._timeout_s)} s.",
                )
            else:
                log.exception("job %s raised TimeoutError", job_id)
                self._finish(job_id, JobStatus.FAILED, "internal_error", _INTERNAL)
        except asyncio.CancelledError:
            if job_id in self._user_cancelled and not self._stopping:
                self._finish(job_id, JobStatus.CANCELLED, "cancelled", "Cancelled by user")
            else:
                self._finish(
                    job_id,
                    JobStatus.FAILED,
                    "interrupted",
                    "Thinair was stopped while this job was running. Retry it.",
                )
        except PrepareError as e:
            self._finish(job_id, JobStatus.FAILED, e.code, e.message)
        except SandboxError as e:
            self._finish(job_id, JobStatus.FAILED, "sandbox_error", str(e))
        except Exception:
            log.exception("job %s failed unexpectedly", job_id)
            self._finish(job_id, JobStatus.FAILED, "internal_error", _INTERNAL)
        finally:
            self._user_cancelled.discard(job_id)
            if not self._stopping and not requeued:
                self._announce(job_id)

    def _requeue_locally(self, job_id: str) -> bool:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                return False
            job.status, job.started_at, job.runner_id = JobStatus.QUEUED, None, None
            s.add(job)
            s.commit()
        self.events.emit(job_id, EventType.STATUS, {"status": JobStatus.QUEUED.value})
        self._queue.put_nowait(job_id)
        return True

    def _announce(self, job_id: str) -> None:
        """Fire-and-forget notification; it can never delay or fail the job."""
        if self._notifier is None:
            return
        task = asyncio.create_task(self._notify_safely(job_id), name=f"thinair-notify-{job_id}")
        self._notifications.add(task)
        task.add_done_callback(self._notifications.discard)

    async def _notify_safely(self, job_id: str) -> None:
        try:
            await self._notifier(job_id)  # type: ignore[misc]
        except Exception:
            log.exception("job %s: notification failed", job_id)

    def _finish(
        self, job_id: str, status: JobStatus, code: str | None, message: str | None
    ) -> None:
        # Event first, then the row: a stream that sees the job as finished is guaranteed to find
        # the final event already stored.
        self.events.emit(
            job_id,
            EventType.STATUS,
            {"status": status.value, "error_code": code, "error_message": message},
        )
        self._finish_row(job_id, status, code, message)
        self.events.forget(job_id)

    @staticmethod
    def _finish_row(job_id: str, status: JobStatus, code: str | None, message: str | None) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            job.status = status
            job.error_code = code
            job.error_message = message
            job.finished_at = utcnow()
            s.add(job)
            s.commit()


GITHUB_NOT_ALLOWED = (
    "Pull requests use the server's GitHub token, which is for admins only here. Download the "
    "patch instead, or ask an admin (THINAIR_GITHUB_TOKEN_FOR_MEMBERS)."
)
_INTERNAL = "Something went wrong inside Thinair. Check the server logs for details."
