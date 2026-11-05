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
    ) -> None:
        self.events = events or EventStore()
        self._executors = executors
        self._mgr = mgr or SandboxManager()
        self._n_workers = max(1, workers)
        self._timeout_s = job_timeout_s
        self._sweep = sweep_on_start
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, asyncio.Task[None]] = {}
        self._user_cancelled: set[str] = set()
        self._stopping = False

    # ---- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        if self._sweep:
            try:
                await asyncio.to_thread(janitor.sweep, self._mgr.client)
            except Exception as e:  # Docker may simply not be running yet
                log.warning("startup cleanup skipped: %s", e)
        self._recover()
        self._workers = [
            asyncio.create_task(self._worker(), name=f"thinair-worker-{i}")
            for i in range(self._n_workers)
        ]

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._running.values()):
            task.cancel()
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, *self._running.values(), return_exceptions=True)
        self._workers.clear()

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
                    self._queue.put_nowait(j.id)

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
    ) -> Job:
        """Create a queued job. Raises PrepareError for a bad repo URL or ref."""
        source = GitSource(repo_url.strip().rstrip("/"), ref)
        source.validate()
        if kind not in self._executors:
            raise PrepareError(
                "unsupported_job", f"Jobs of kind {kind.value!r} are not available yet"
            )
        with session_scope() as s:
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
            )
            s.add(job)
            s.commit()
        self.events.emit(job.id, EventType.STATUS, {"status": JobStatus.QUEUED.value})
        self._queue.put_nowait(job.id)
        return job

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
    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                task = asyncio.create_task(self._execute(job_id), name=f"thinair-job-{job_id}")
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
                self._queue.task_done()

    async def _execute(self, job_id: str) -> None:
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
        timeout = asyncio.timeout(self._timeout_s)
        try:
            async with timeout:
                res = await self._executors[job.kind](ctx, self._mgr)
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


_INTERNAL = "Something went wrong inside Thinair. Check the server logs for details."
