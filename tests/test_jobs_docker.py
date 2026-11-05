"""Real jobs through the real executor and Docker (need Docker). The GitHub clone is swapped for the
local Node fixture so the test needs no internet beyond what the fixture install needs (none)."""

import asyncio
from pathlib import Path

import docker
import pytest
from sqlmodel import select

from thinair.db.migrate import upgrade
from thinair.db.models import EventType, Job, JobKind, JobStatus, ProfileReport, ProfileRun
from thinair.db.session import get_engine, session_scope
from thinair.jobs import executors
from thinair.jobs.events import TERMINAL_STATUSES
from thinair.jobs.service import JobService
from thinair.project.prepare import DirSource
from thinair.sandbox.manager import SandboxManager

MiB = 1024**2
FIXTURE = Path(__file__).parent / "fixtures" / "node_app"
pytestmark = pytest.mark.docker


@pytest.fixture
def mgr():
    try:
        c = docker.from_env()
        c.ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    return SandboxManager(c)


@pytest.fixture
async def service(mgr, monkeypatch):
    upgrade(get_engine())
    real = executors.profile_source

    async def local_fixture(mgr_, _source, job_id, **kw):
        return await real(mgr_, DirSource(FIXTURE), job_id, **kw)

    monkeypatch.setattr(executors, "profile_source", local_fixture)
    svc = JobService(executors.default_executors(), mgr=mgr, sweep_on_start=False)
    await svc.start()
    yield svc
    await svc.stop()
    label = {"label": "thinair.managed=true"}
    assert not mgr.client.containers.list(all=True, filters=label), "container leaked"
    assert not mgr.client.volumes.list(filters=label), "workspace volume leaked"


async def finished(job_id: str, timeout: float = 400) -> Job:
    async def _wait() -> Job:
        while True:
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job.status in TERMINAL_STATUSES:
                    return job
            await asyncio.sleep(0.2)

    return await asyncio.wait_for(_wait(), timeout)


async def test_profile_job_end_to_end(service) -> None:
    job = service.submit("https://github.com/octo/demo", JobKind.PROFILE)
    done = await finished(job.id)
    assert done.status == JobStatus.SUCCEEDED, done.error_message

    with session_scope() as s:
        report = s.get(ProfileReport, job.id)
        runs = list(s.exec(select(ProfileRun).where(ProfileRun.job_id == job.id)))
    assert report.min_config["mem_bytes"] == 256 * MiB
    assert "**Minimum passing:**" in report.report_md
    assert len(runs) == len(report.report["runs"]) > 0

    frames = [f async for f in service.events.stream(job.id)]
    text = "".join(frames)
    assert text.count("event: profile_run") == len(runs)
    assert "[thinair] detected node project" in text  # progress logs reached the stream
    assert frames[-1].count('"status": "succeeded"') == 1


async def test_cancelling_mid_profile_leaves_nothing_behind(service) -> None:
    job = service.submit("https://github.com/octo/demo", JobKind.PROFILE)

    def has_run() -> bool:
        return any(e.type == EventType.PROFILE_RUN for e in service.events.fetch(job.id))

    async def wait_for_first_run() -> None:
        while not has_run():
            await asyncio.sleep(0.1)

    await asyncio.wait_for(wait_for_first_run(), 300)
    assert service.cancel(job.id) is True
    done = await finished(job.id, timeout=60)
    assert done.status == JobStatus.CANCELLED
    # the `service` fixture asserts no container or workspace volume remains
