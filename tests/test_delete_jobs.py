"""Deleting finished jobs: DELETE /api/jobs/{id}. No Docker."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.config import get_settings
from thinair.db.models import (
    EventType,
    Job,
    JobEvent,
    JobKind,
    JobResult,
    JobStatus,
    ProfileReport,
    ProfileRun,
    Repo,
    RunOutcome,
)
from thinair.db.session import session_scope
from thinair.jobs.events import EventStore
from thinair.jobs.service import ExecResult

OWNER = "o" * 40
URL = "https://github.com/octo/alpha"


async def quick(ctx, _mgr) -> ExecResult:
    ctx.log("hello")
    return ExecResult()


async def forever(_ctx, _mgr) -> ExecResult:
    await asyncio.sleep(3600)
    return ExecResult()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", OWNER)
    get_settings.cache_clear()
    api_auth._failures.clear()
    with TestClient(create_app({JobKind.PROFILE: quick, JobKind.AGENT: forever})) as c:
        c.headers["Authorization"] = f"Bearer {OWNER}"
        yield c


def h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def finished(client, job_id: str) -> None:
    import time

    for _ in range(200):
        if client.get(f"/api/jobs/{job_id}").json()["finished"]:
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def rows(model, job_id: str) -> int:
    col = model.job_id if hasattr(model, "job_id") else None
    with session_scope() as s:
        return len(list(s.exec(select(model).where(col == job_id))))


def test_a_finished_job_is_deleted_with_everything_it_stored(client) -> None:
    job = client.post("/api/jobs", json={"repo_url": URL}).json()
    finished(client, job["id"])
    with session_scope() as s:  # a profile job's stored results, as the executor would leave them
        s.add(ProfileRun(job_id=job["id"], cpus=1, mem_bytes=1, outcome=RunOutcome.PASSED))
        s.add(ProfileReport(job_id=job["id"], report={}))
        s.add(JobResult(job_id=job["id"], summary="x"))
        s.commit()
    assert rows(JobEvent, job["id"]) > 0
    r = client.delete(f"/api/jobs/{job['id']}")
    assert r.status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert job["id"] not in [j["id"] for j in client.get("/api/jobs").json()]
    for model in (JobEvent, ProfileRun, ProfileReport, JobResult):
        assert rows(model, job["id"]) == 0, model.__name__
    with session_scope() as s:
        assert s.exec(select(Repo).where(Repo.url == URL)).first() is not None  # the project stays
    assert EventStore().fetch(job["id"]) == []


def test_a_running_job_cannot_be_deleted(client) -> None:
    job = client.post(
        "/api/jobs", json={"repo_url": URL, "kind": "agent", "agent_id": "claude", "task": "x"}
    ).json()  # the fake agent never finishes
    r = client.delete(f"/api/jobs/{job['id']}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "still_running"
    assert client.get(f"/api/jobs/{job['id']}").status_code == 200
    client.post(f"/api/jobs/{job['id']}/cancel")


def test_people_delete_only_their_own_jobs_and_admins_any(client) -> None:
    sam = client.post("/api/users", json={"name": "sam"}).json()["token"]
    kim = client.post("/api/users", json={"name": "kim"}).json()["token"]
    mine = client.post("/api/jobs", json={"repo_url": URL}, headers=h(sam)).json()
    theirs = client.post("/api/jobs", json={"repo_url": URL}, headers=h(kim)).json()
    for j in (mine, theirs):
        finished(client, j["id"])
    # Someone else's job is "not found", exactly like every other route.
    other = client.delete(f"/api/jobs/{theirs['id']}", headers=h(sam))
    assert other.status_code == 404 and other.json()["detail"]["code"] == "job_not_found"
    assert client.get(f"/api/jobs/{theirs['id']}").status_code == 200
    assert client.delete(f"/api/jobs/{mine['id']}", headers=h(sam)).status_code == 204
    assert client.delete(f"/api/jobs/{theirs['id']}").status_code == 204  # the owner is an admin


def test_deleting_an_agent_job_takes_its_finished_pull_request_jobs_along(client) -> None:
    with session_scope() as s:
        repo = Repo(url=URL, name="alpha")
        s.add(repo)
        s.commit()
        agent = Job(repo_id=repo.id, kind=JobKind.AGENT, status=JobStatus.SUCCEEDED)
        s.add(agent)
        s.commit()
        pr = Job(repo_id=repo.id, kind=JobKind.PR, status=JobStatus.RUNNING, parent_job_id=agent.id)
        s.add(pr)
        s.add(JobResult(job_id=agent.id, summary="done"))
        s.add(JobEvent(job_id=pr.id, seq=1, type=EventType.LOG, data={"line": "x"}))
        s.commit()
        agent_id, pr_id = agent.id, pr.id
    blocked = client.delete(f"/api/jobs/{agent_id}")  # its pull request is still being opened
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "still_running"
    with session_scope() as s:
        pr = s.get(Job, pr_id)
        pr.status = JobStatus.FAILED
        s.add(pr)
        s.commit()
    assert client.delete(f"/api/jobs/{agent_id}").status_code == 204
    assert client.get(f"/api/jobs/{agent_id}").status_code == 404
    assert client.get(f"/api/jobs/{pr_id}").status_code == 404
    assert rows(JobEvent, pr_id) == 0 and rows(JobResult, agent_id) == 0


def test_deleting_needs_a_login_and_an_unknown_id_is_404(client) -> None:
    assert TestClient(client.app).delete("/api/jobs/x").status_code == 401
    r = client.delete("/api/jobs/nope")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "job_not_found"
