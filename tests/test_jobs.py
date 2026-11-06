"""Job queue, lifecycle and event-stream tests. Executors are fakes: no Docker involved."""

import asyncio
import json

import pytest
from sqlmodel import select

from thinair.db.migrate import upgrade
from thinair.db.models import EventType, Job, JobEvent, JobKind, JobStatus, Repo, utcnow
from thinair.db.session import get_engine, session_scope
from thinair.jobs.events import TERMINAL_STATUSES, EventStore
from thinair.jobs.service import ExecResult, JobContext, JobService
from thinair.project.prepare import PrepareError
from thinair.sandbox.spec import SandboxError

URL = "https://github.com/octo/demo"


@pytest.fixture(autouse=True)
def _db():
    upgrade(get_engine())


def status_of(job_id: str) -> JobStatus:
    with session_scope() as s:
        return s.get(Job, job_id).status


async def until_finished(job_id: str, timeout: float = 5.0) -> Job:
    async def _wait() -> Job:
        while True:
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job.status in TERMINAL_STATUSES:
                    return job
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(_wait(), timeout)


async def until(predicate, timeout: float = 5.0) -> None:
    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout)


def events_of(job_id: str) -> list[JobEvent]:
    return EventStore().fetch(job_id)


async def make_service(executor, **kw):
    svc = JobService({JobKind.PROFILE: executor}, sweep_on_start=False, **kw)
    await svc.start()
    return svc


async def ok_executor(ctx: JobContext, _mgr) -> ExecResult:
    ctx.log("hello")
    ctx.emit(EventType.PROFILE_RUN, {"cpus": 1})
    return ExecResult()


# ---- lifecycle ---------------------------------------------------------------------------
async def test_job_runs_and_events_are_ordered_and_complete() -> None:
    svc = await make_service(ok_executor)
    try:
        job = svc.submit(URL)
        done = await until_finished(job.id)
        assert done.status == JobStatus.SUCCEEDED and done.started_at and done.finished_at
        evs = events_of(job.id)
        assert [e.seq for e in evs] == list(range(1, len(evs) + 1))
        kinds = [(e.type, e.data.get("status") or e.data.get("line")) for e in evs]
        assert kinds == [
            (EventType.STATUS, "queued"),
            (EventType.STATUS, "running"),
            (EventType.LOG, "hello"),
            (EventType.PROFILE_RUN, None),
            (EventType.STATUS, "succeeded"),
        ]
    finally:
        await svc.stop()


async def test_failures_become_clear_job_errors() -> None:
    cases = [
        (
            PrepareError("install_failed", "The install step failed."),
            "install_failed",
            "install step",
        ),
        (SandboxError("Cannot reach the Docker daemon."), "sandbox_error", "Docker"),
        (RuntimeError("secret internal detail /etc/passwd"), "internal_error", "server logs"),
    ]
    for exc, code, fragment in cases:

        async def boom(_ctx, _mgr, exc=exc):
            raise exc

        svc = await make_service(boom)
        try:
            job = await until_finished(svc.submit(URL).id)
            assert job.status == JobStatus.FAILED and job.error_code == code
            assert fragment in job.error_message
            assert "passwd" not in job.error_message  # internals never leak to users
            last = events_of(job.id)[-1]
            assert last.data["status"] == "failed" and last.data["error_code"] == code
        finally:
            await svc.stop()


async def test_executor_can_report_its_own_failure() -> None:
    async def fails(_ctx, _mgr):
        return ExecResult(JobStatus.FAILED, "baseline_failed", "tests fail")

    svc = await make_service(fails)
    try:
        job = await until_finished(svc.submit(URL).id)
        assert (job.status, job.error_code) == (JobStatus.FAILED, "baseline_failed")
    finally:
        await svc.stop()


async def test_cancel_running_job_stops_the_executor() -> None:
    state = {"cancelled": False, "started": asyncio.Event()}

    async def hang(_ctx, _mgr):
        state["started"].set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            state["cancelled"] = True  # the executor's cleanup code (e.g. kill container) runs
            raise
        return ExecResult()

    svc = await make_service(hang)
    try:
        job = svc.submit(URL)
        await asyncio.wait_for(state["started"].wait(), 5)
        assert svc.cancel(job.id) is True
        done = await until_finished(job.id)
        assert done.status == JobStatus.CANCELLED and state["cancelled"]
        assert svc.cancel(job.id) is False  # already finished
    finally:
        await svc.stop()


async def test_cancel_queued_job_never_runs_it() -> None:
    gate, ran = asyncio.Event(), []

    async def gated(ctx, _mgr):
        ran.append(ctx.job.id)
        await gate.wait()
        return ExecResult()

    svc = await make_service(gated, workers=1)
    try:
        a, b = svc.submit(URL), svc.submit(URL)
        await until(lambda: ran == [a.id])
        assert svc.cancel(b.id) is True
        assert status_of(b.id) == JobStatus.CANCELLED
        gate.set()
        await until_finished(a.id)
        await asyncio.sleep(0.1)
        assert ran == [a.id]  # b was skipped
    finally:
        await svc.stop()


async def test_worker_count_limits_concurrency() -> None:
    for workers, expected_peak in ((1, 1), (2, 2)):
        live, peak = 0, 0

        async def work(_ctx, _mgr):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.1)
            live -= 1
            return ExecResult()

        svc = await make_service(work, workers=workers)
        try:
            ids = [svc.submit(URL).id for _ in range(3)]
            for i in ids:
                await until_finished(i)
            assert peak == expected_peak
        finally:
            await svc.stop()


async def test_job_time_limit() -> None:
    async def slow(_ctx, _mgr):
        await asyncio.sleep(30)
        return ExecResult()

    svc = await make_service(slow, job_timeout_s=0.2)
    try:
        job = await until_finished(svc.submit(URL).id)
        assert job.status == JobStatus.TIMEOUT and "time limit" in job.error_message
    finally:
        await svc.stop()


async def test_restart_recovery() -> None:
    with session_scope() as s:
        repo = Repo(url=URL, name="demo")
        s.add(repo)
        s.commit()
        running = Job(repo_id=repo.id, kind=JobKind.PROFILE, status=JobStatus.RUNNING)
        queued = Job(repo_id=repo.id, kind=JobKind.PROFILE, status=JobStatus.QUEUED)
        s.add(running)
        s.add(queued)
        s.commit()
    svc = await make_service(ok_executor)
    try:
        assert (await until_finished(running.id)).error_code == "interrupted"
        assert (await until_finished(queued.id)).status == JobStatus.SUCCEEDED
    finally:
        await svc.stop()


async def test_stopping_marks_running_jobs_interrupted() -> None:
    started = asyncio.Event()

    async def hang(_ctx, _mgr):
        started.set()
        await asyncio.sleep(60)
        return ExecResult()

    svc = await make_service(hang)
    job = svc.submit(URL)
    await asyncio.wait_for(started.wait(), 5)
    await svc.stop()
    assert status_of(job.id) == JobStatus.FAILED
    with session_scope() as s:
        assert s.get(Job, job.id).error_code == "interrupted"


async def test_submit_validates_and_reuses_repos() -> None:
    svc = await make_service(ok_executor)
    try:
        with pytest.raises(PrepareError) as e:
            svc.submit("https://gitlab.com/a/b")
        assert e.value.code == "bad_repo_url"
        with pytest.raises(PrepareError, match="not available"):
            svc.submit(URL, JobKind.AGENT)
        a, b = svc.submit(URL + "/"), svc.submit(URL)
        assert a.repo_id == b.repo_id
        with session_scope() as s:
            assert len(s.exec(select(Repo)).all()) == 1
    finally:
        await svc.stop()


# ---- event store and SSE -----------------------------------------------------------------
async def collect(store: EventStore, job_id: str, after: int = 0, limit: float = 5) -> list[str]:
    out: list[str] = []

    async def _go() -> None:
        async for frame in store.stream(job_id, after):
            out.append(frame)

    await asyncio.wait_for(_go(), limit)
    return out


def parse(frame: str) -> tuple[int, str, dict]:
    lines = dict(ln.split(": ", 1) for ln in frame.strip().splitlines())
    return int(lines["id"]), lines["event"], json.loads(lines["data"])


async def test_sse_replays_everything_for_a_finished_job() -> None:
    svc = await make_service(ok_executor)
    try:
        job = svc.submit(URL)
        await until_finished(job.id)
        frames = await collect(svc.events, job.id)
        ids = [parse(f)[0] for f in frames]
        assert ids == [1, 2, 3, 4, 5]
        assert parse(frames[-1])[1:] == (
            "status",
            {
                "ts": parse(frames[-1])[2]["ts"],
                "stream": None,
                "data": {"status": "succeeded", "error_code": None, "error_message": None},
            },
        )
    finally:
        await svc.stop()


async def test_sse_resumes_after_last_event_id() -> None:
    svc = await make_service(ok_executor)
    try:
        job = svc.submit(URL)
        await until_finished(job.id)
        frames = await collect(svc.events, job.id, after=3)
        assert [parse(f)[0] for f in frames] == [4, 5]
        assert await collect(svc.events, job.id, after=5) == []
    finally:
        await svc.stop()


async def test_sse_streams_live_and_ends_with_the_job() -> None:
    gate = asyncio.Event()

    async def live(ctx, _mgr):
        ctx.log("one")
        await gate.wait()
        ctx.log("two")
        return ExecResult()

    svc = await make_service(live)
    try:
        job = svc.submit(URL)
        reader = asyncio.create_task(collect(svc.events, job.id))
        await until(lambda: any(e.data.get("line") == "one" for e in events_of(job.id)))
        await asyncio.sleep(0.05)
        assert not reader.done()  # still streaming: the job is running
        gate.set()
        frames = await asyncio.wait_for(reader, 5)
        lines = [parse(f)[2]["data"].get("line") for f in frames if parse(f)[1] == "log"]
        assert lines == ["one", "two"]
        assert parse(frames[-1])[2]["data"]["status"] == "succeeded"
    finally:
        await svc.stop()


async def test_sse_sends_keepalives_while_idle() -> None:
    gate = asyncio.Event()

    async def idle(_ctx, _mgr):
        await gate.wait()
        return ExecResult()

    store = EventStore(heartbeat_s=0.05)
    svc = JobService({JobKind.PROFILE: idle}, events=store, sweep_on_start=False)
    await svc.start()
    try:
        job = svc.submit(URL)
        frames: list[str] = []

        async def read() -> None:
            async for f in store.stream(job.id):
                frames.append(f)

        reader = asyncio.create_task(read())
        await until(lambda: ": keepalive\n\n" in frames)
        gate.set()
        await asyncio.wait_for(reader, 5)
    finally:
        await svc.stop()


async def test_unknown_job_stream_ends_immediately() -> None:
    assert await collect(EventStore(), "nope") == []


async def test_event_cap_drops_logs_but_keeps_status() -> None:
    store = EventStore(max_events_per_job=5)

    async def chatty(ctx, _mgr):
        for i in range(50):
            ctx.log(f"line {i}")
        return ExecResult()

    svc = JobService({JobKind.PROFILE: chatty}, events=store, sweep_on_start=False)
    await svc.start()
    try:
        job = await until_finished(svc.submit(URL).id)
        evs = store.fetch(job.id, limit=1000)
        logs = [e for e in evs if e.type == EventType.LOG]
        assert len(logs) <= 5 and any("log limit" in e.data["line"] for e in logs)
        assert evs[-1].data["status"] == "succeeded"  # lifecycle events are never dropped
    finally:
        await svc.stop()


async def test_concurrent_jobs_have_independent_sequences() -> None:
    svc = await make_service(ok_executor, workers=3)
    try:
        ids = [svc.submit(URL).id for _ in range(5)]
        for i in ids:
            await until_finished(i)
        for i in ids:
            assert [e.seq for e in events_of(i)] == [1, 2, 3, 4, 5]
        assert utcnow() is not None
    finally:
        await svc.stop()


# ---- event buffering ----------------------------------------------------------------------
async def test_log_lines_are_batched_and_other_events_flush_them_in_order() -> None:
    with session_scope() as s:
        repo = Repo(url=URL, name="demo")
        s.add(repo)
        s.commit()
        job = Job(repo_id=repo.id, kind=JobKind.PROFILE)
        s.add(job)
        s.commit()
    store = EventStore(flush_s=0.05, flush_lines=5)
    store.emit(job.id, EventType.LOG, {"line": "a"})
    store.emit(job.id, EventType.LOG, {"line": "b"})
    assert events_of(job.id) == []  # buffered, not yet one commit per line
    await asyncio.sleep(0.15)
    assert [e.data["line"] for e in events_of(job.id)] == ["a", "b"]  # the timer flushed

    store.emit(job.id, EventType.LOG, {"line": "c"})
    store.emit(job.id, EventType.STATUS, {"status": "running"})  # flushes "c" first
    assert [(e.seq, e.type) for e in events_of(job.id)][-2:] == [
        (3, EventType.LOG),
        (4, EventType.STATUS),
    ]
    for i in range(5):  # a full batch is written without waiting for the timer
        store.emit(job.id, EventType.LOG, {"line": f"x{i}"})
    assert len(events_of(job.id)) == 9

    capped = EventStore(max_events_per_job=10, flush_s=10)
    for i in range(5):
        capped.emit(job.id, EventType.LOG, {"line": f"y{i}"})
    lines = [e.data.get("line", "") for e in events_of(job.id)]
    assert lines[-1].startswith("[thinair] log limit reached") and len(lines) == 11


# ---- log retention ------------------------------------------------------------------------
def test_old_logs_are_removed_but_results_and_history_stay() -> None:
    from datetime import timedelta

    from thinair.retention import remove_old_logs

    store = EventStore()
    with session_scope() as s:
        repo = Repo(url=URL, name="demo")
        s.add(repo)
        s.commit()
        now = utcnow()
        old = Job(
            repo_id=repo.id,
            kind=JobKind.PROFILE,
            status=JobStatus.SUCCEEDED,
            finished_at=now - timedelta(days=31),
        )
        recent = Job(
            repo_id=repo.id,
            kind=JobKind.PROFILE,
            status=JobStatus.SUCCEEDED,
            finished_at=now - timedelta(days=29),
        )
        running = Job(repo_id=repo.id, kind=JobKind.PROFILE, status=JobStatus.RUNNING)
        s.add_all([old, recent, running])
        s.commit()
    for job in (old, recent, running):
        store.emit(job.id, EventType.LOG, {"line": "pip install ..."})
        store.emit(job.id, EventType.PROFILE_RUN, {"cpus": 1})
        store.emit(job.id, EventType.STATUS, {"status": "succeeded"})

    assert remove_old_logs(0) == 0  # 0 = keep forever
    assert remove_old_logs(30, batch=1) == 1
    assert [e.type for e in events_of(old.id)] == [EventType.PROFILE_RUN, EventType.STATUS]
    assert len(events_of(recent.id)) == 3 and len(events_of(running.id)) == 3
    with session_scope() as s:
        assert s.get(Job, old.id).logs_removed_at is not None
        assert s.get(Job, recent.id).logs_removed_at is None
    assert remove_old_logs(30) == 0  # already cleaned jobs are skipped
