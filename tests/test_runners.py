"""Remote runners without Docker: registration and secrets, the runner protocol through the real
API (one lifespan TestClient), routing/fallback in JobService, and the runner program against a
live uvicorn server with a fake profiler."""

import asyncio
import socket
import threading
import time

import pytest
import requests
import uvicorn
from fastapi.testclient import TestClient
from sqlmodel import select

from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.config import get_settings
from thinair.db.migrate import upgrade
from thinair.db.models import Job, JobKind, JobStatus, ProfileReport, ProfileRun
from thinair.db.session import get_engine, session_scope
from thinair.jobs.events import TERMINAL_STATUSES, EventStore
from thinair.jobs.service import ExecResult, JobService
from thinair.profiler.models import ProfileResult
from thinair.runner_client import (
    Profiled,
    RunnerClient,
    RunnerSetupError,
    check_server_url,
    check_token,
)
from thinair.runners import (
    RunnerError,
    RunnerHub,
    create_runner,
    disable_runner,
    make_remote_executor,
    verify_runner,
)

OWNER = "o" * 32
URL = "https://github.com/octo/demo"
GiB = 1024**3
RUN = {"cpus": 1.0, "mem_bytes": GiB // 4, "outcome": "passed", "wall_s": 1.5}
RESULT = {
    "status": "ok",
    "runs": [RUN],
    "levels": [{"cpus": 1.0, "status": "found", "min_mem_bytes": GiB // 4}],
    "min_config": {"cpus": 1.0, "mem_bytes": GiB // 4},
    "recommended": {"cpus": 2.0, "mem_bytes": GiB // 2},
}
local_runs: list[str] = []


async def local_profile(ctx, _mgr) -> ExecResult:
    local_runs.append(ctx.job.id)
    with session_scope() as s:
        s.add(ProfileReport(job_id=ctx.job.id, report={"runs": []}, report_md="# local"))
        s.commit()
    return ExecResult()


async def agent(ctx, _mgr) -> ExecResult:
    return ExecResult()


def finished(c, job_id: str, timeout: float = 10) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["finished"]:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def runner_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", OWNER)
    monkeypatch.setenv("THINAIR_RUNNER_PLACEMENT", "runners")
    get_settings.cache_clear()
    api_auth._failures.clear()
    local_runs.clear()
    a = create_app({JobKind.PROFILE: local_profile, JobKind.AGENT: agent})
    with TestClient(a) as c:
        c.headers["Authorization"] = f"Bearer {OWNER}"
        yield a, c


def new_runner(c, name: str = "big-pc") -> str:
    r = c.post("/api/runners", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def claim(c, token: str, wait: float = 5):
    return c.post(f"/runner/v1/claim?wait={wait}", json={"cpus": 8}, headers=runner_headers(token))


# ---- registration -----------------------------------------------------------------------------
def test_runner_secrets_are_hashed_separate_and_revocable() -> None:
    upgrade(get_engine())
    with session_scope() as s:
        runner, token = create_runner(s, "box")
        assert token.startswith("thinair_runner_") and token not in runner.token_hash
        assert verify_runner(s, token).id == runner.id
        assert verify_runner(s, OWNER) is None and verify_runner(s, token + "x") is None
        for bad in ("Box", "a.b", "", "x" * 33):
            with pytest.raises(RunnerError):
                create_runner(s, bad)
        with pytest.raises(RunnerError):
            create_runner(s, "box")
        disable_runner(s, "box")
        assert verify_runner(s, token) is None


def test_only_admins_manage_runners_and_secrets_stay_in_their_lane(app) -> None:
    a, c = app
    token = new_runner(c)
    assert c.post("/api/runners", json={"name": "big-pc"}).status_code == 409
    member = c.post("/api/users", json={"name": "bob"}).json()["token"]
    h = runner_headers(member)
    assert c.get("/api/runners", headers=h).status_code == 403
    assert c.post("/api/runners", json={"name": "x"}, headers=h).status_code == 403
    # A runner secret is no API token, and an API token is no runner secret.
    assert c.get("/api/jobs", headers=runner_headers(token)).status_code == 401
    assert claim(c, OWNER, 0).status_code == 401
    assert claim(c, member, 0).status_code == 401
    listed = c.get("/api/runners").json()
    assert [r["name"] for r in listed] == ["big-pc"] and "token" not in listed[0]


# ---- protocol through the API ------------------------------------------------------------------
def test_a_profile_job_runs_on_a_runner_end_to_end(app) -> None:
    a, c = app
    token = new_runner(c)
    job = c.post("/api/jobs", json={"repo_url": URL, "ref": "main"}).json()
    r = claim(c, token)
    assert r.status_code == 200, r.text
    got = r.json()["job"]
    assert got == {
        "id": job["id"],
        "kind": "profile",
        "repo_url": URL,
        "ref": "main",
        "github_auth": True,  # the owner's job; the runner uses ITS OWN token, if any
    }
    assert c.get(f"/api/jobs/{job['id']}").json()["status"] == "running"

    events = {
        "events": [{"type": "log", "line": "<b>cloning</b>"}, {"type": "profile_run", "run": RUN}]
    }
    r = c.post(f"/runner/v1/jobs/{job['id']}/events", json=events, headers=runner_headers(token))
    assert r.json() == {"cancel": False}
    body = {"outcome": "profiled", "result": RESULT, "commit_sha": "a" * 40, "stack": "python"}
    r = c.post(f"/runner/v1/jobs/{job['id']}/finish", json=body, headers=runner_headers(token))
    assert r.json() == {"accepted": True}

    done = finished(c, job["id"])
    assert done["status"] == "succeeded" and done["commit_sha"] == "a" * 40
    report = c.get(f"/api/jobs/{job['id']}/profile").json()
    assert report["min_config"] == {"cpus": 1.0, "mem_bytes": GiB // 4}
    assert "python" in c.get(f"/api/jobs/{job['id']}/profile.md").text
    lines = [e.data.get("line") for e in EventStore().fetch(job["id"])]
    assert "<b>cloning</b>" in lines and "[thinair] running on runner 'big-pc'" in lines
    with session_scope() as s:
        assert s.get(Job, job["id"]).runner_id is not None
        assert len(s.exec(select(ProfileRun).where(ProfileRun.job_id == job["id"])).all()) == 1
    assert local_runs == []
    # A second report for the same job is ignored.
    r = c.post(f"/runner/v1/jobs/{job['id']}/finish", json=body, headers=runner_headers(token))
    assert r.json() == {"accepted": False}


def test_runner_failures_and_baseline_failures_are_reported(app) -> None:
    a, c = app
    token = new_runner(c)
    for body, code in [
        (
            {"outcome": "failed", "error_code": "clone_failed", "error_message": "nope"},
            "clone_failed",
        ),
        (
            {
                "outcome": "profiled",
                "result": {"status": "baseline_failed", "notes": ["tests fail"]},
            },
            "baseline_failed",
        ),
    ]:
        job = c.post("/api/jobs", json={"repo_url": URL}).json()
        assert claim(c, token).json()["job"]["id"] == job["id"]
        c.post(f"/runner/v1/jobs/{job['id']}/finish", json=body, headers=runner_headers(token))
        done = finished(c, job["id"])
        assert done["status"] == "failed" and done["error_code"] == code


def test_cancel_reaches_the_runner(app) -> None:
    a, c = app
    token = new_runner(c)
    job = c.post("/api/jobs", json={"repo_url": URL}).json()
    claim(c, token)
    assert c.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
    assert finished(c, job["id"])["status"] == "cancelled"
    r = c.post(
        f"/runner/v1/jobs/{job['id']}/events", json={"events": []}, headers=runner_headers(token)
    )
    assert r.json() == {"cancel": True}


def test_runners_cannot_touch_jobs_they_do_not_hold(app) -> None:
    a, c = app
    mine, other = new_runner(c, "one"), new_runner(c, "two")
    job = c.post("/api/jobs", json={"repo_url": URL}).json()
    assert claim(c, mine).json()["job"]["id"] == job["id"]
    h = runner_headers(other)
    spoof = {"events": [{"type": "log", "line": "spoofed"}]}
    assert c.post(f"/runner/v1/jobs/{job['id']}/events", json=spoof, headers=h).json() == {
        "cancel": True
    }
    fin = {"outcome": "failed", "error_code": "x"}
    assert c.post(f"/runner/v1/jobs/{job['id']}/finish", json=fin, headers=h).json() == {
        "accepted": False
    }
    assert "spoofed" not in [e.data.get("line") for e in EventStore().fetch(job["id"])]
    # Malformed and oversized bodies are refused before they reach anything.
    h = runner_headers(mine)
    bad = {"events": [{"type": "status", "line": "x"}]}
    assert c.post(f"/runner/v1/jobs/{job['id']}/events", json=bad, headers=h).status_code == 422
    huge = b'{"events": [], "pad": "' + b"x" * (5 * 1024 * 1024) + b'"}'
    r = c.post(f"/runner/v1/jobs/{job['id']}/events", content=huge, headers=h)
    assert r.status_code == 413
    fin = {"outcome": "failed", "error_code": "Bad Code!"}
    assert c.post(f"/runner/v1/jobs/{job['id']}/finish", json=fin, headers=h).status_code == 422
    # A disabled runner is locked out at once.
    c.post("/api/runners/one/disable")
    assert claim(c, mine, 0).status_code == 401
    c.post(f"/api/jobs/{job['id']}/cancel")


def test_old_protocol_is_refused_and_idle_claims_return_204(app) -> None:
    a, c = app
    token = new_runner(c)
    assert claim(c, token, 0).status_code == 204
    r = c.post("/runner/v1/claim?wait=0", json={"protocol": 99}, headers=runner_headers(token))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "protocol_mismatch"
    listed = c.get("/api/runners").json()[0]
    assert listed["online"] and listed["info"]["cpus"] == 8


def test_member_jobs_never_get_github_auth_on_runners(app) -> None:
    a, c = app
    token = new_runner(c)
    member = c.post("/api/users", json={"name": "bob"}).json()["token"]
    job = c.post("/api/jobs", json={"repo_url": URL}, headers=runner_headers(member)).json()
    got = claim(c, token).json()["job"]
    assert got["id"] == job["id"] and got["github_auth"] is False
    c.post(f"/api/jobs/{job['id']}/cancel")


def test_agent_jobs_never_go_to_runners(app) -> None:
    a, c = app
    token = new_runner(c)
    job = c.post(
        "/api/jobs", json={"repo_url": URL, "kind": "agent", "task": "x", "agent_id": "claude"}
    ).json()
    assert finished(c, job["id"])["status"] == "succeeded"
    assert claim(c, token, 0).status_code == 204


# ---- JobService routing ------------------------------------------------------------------------
async def wait_status(job_id: str, timeout: float = 10) -> Job:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job.status in TERMINAL_STATUSES:
                return job
        await asyncio.sleep(0.02)
    raise AssertionError("not finished")


async def test_auto_placement_falls_back_to_this_server(monkeypatch) -> None:
    monkeypatch.setenv("THINAIR_RUNNER_PLACEMENT", "auto")
    monkeypatch.setenv("THINAIR_RUNNER_CLAIM_TIMEOUT_S", "0.3")
    get_settings.cache_clear()
    upgrade(get_engine())
    local_runs.clear()
    svc = JobService(
        {JobKind.PROFILE: local_profile},
        sweep_on_start=False,
        remote_executor=make_remote_executor(RunnerHub()),
        goes_remote=lambda job: True,  # pretend a runner was idle when it was queued
    )
    await svc.start()
    try:
        job = svc.submit(URL)
        done = await wait_status(job.id)
        assert done.status == JobStatus.SUCCEEDED and local_runs == [job.id]
        lines = [e.data.get("line") for e in EventStore().fetch(job.id)]
        assert "[thinair] no runner picked it up; running it on this server instead" in lines
        statuses = [e.data["status"] for e in EventStore().fetch(job.id) if e.type == "status"]
        assert statuses == ["queued", "running", "queued", "running", "succeeded"]
    finally:
        await svc.stop()


async def test_a_silent_runner_loses_the_job(monkeypatch) -> None:
    monkeypatch.setenv("THINAIR_RUNNER_PLACEMENT", "runners")
    monkeypatch.setenv("THINAIR_RUNNER_LEASE_S", "0.4")
    get_settings.cache_clear()
    upgrade(get_engine())
    hub = RunnerHub()
    svc = JobService(
        {JobKind.PROFILE: local_profile},
        sweep_on_start=False,
        remote_executor=make_remote_executor(hub),
        goes_remote=lambda job: True,
    )
    await svc.start()
    try:
        with session_scope() as s:
            runner, _ = create_runner(s, "box")
        job = svc.submit(URL)
        lease = await hub.claim(runner.id, "box", 5)
        assert lease is not None and lease.job_id == job.id
        # It claimed but never reported back (a restart mid-request): offered again.
        again = await hub.claim(runner.id, "box", 5)
        assert again is lease
        assert hub.lease_for(job.id, runner.id) is lease  # now it has really started
        done = await wait_status(job.id)
        assert done.status == JobStatus.FAILED and done.error_code == "runner_lost"
        assert done.runner_id == runner.id
        assert hub.lease_for(job.id, runner.id) is None  # withdrawn
    finally:
        await svc.stop()


async def test_hub_reports_idle_runners() -> None:
    hub = RunnerHub()
    assert not hub.has_idle_runner()
    waiting = asyncio.create_task(hub.claim("r1", "box", 5))
    await asyncio.sleep(0.05)
    assert hub.has_idle_runner() and "r1" in hub.online()
    await hub.offer("j1", github_auth=False)
    lease = await waiting
    assert lease.job_id == "j1" and lease.runner_name == "box"
    assert not hub.has_idle_runner()


# ---- the runner program ------------------------------------------------------------------------
def test_runner_setup_checks() -> None:
    assert check_server_url("https://t.example.com/") == "https://t.example.com"
    assert check_server_url("http://localhost:8080") == "http://localhost:8080"
    for bad in ("http://t.example.com", "ftp://x", "t.example.com"):
        with pytest.raises(RunnerSetupError):
            check_server_url(bad)
    assert check_server_url("http://10.0.0.2:8080", allow_http=True)
    with pytest.raises(RunnerSetupError):
        check_token(OWNER)  # a user API token is not a runner secret
    with pytest.raises(RunnerSetupError):
        check_token(None)


@pytest.fixture
def live(monkeypatch):
    """The real app on a real port, so the runner's `requests` client is exercised for real."""
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", OWNER)
    monkeypatch.setenv("THINAIR_RUNNER_PLACEMENT", "runners")
    get_settings.cache_clear()
    api_auth._failures.clear()
    app = create_app({JobKind.PROFILE: local_profile, JobKind.AGENT: agent})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    end = time.monotonic() + 10
    while not server.started and time.monotonic() < end:
        time.sleep(0.02)
    api = requests.Session()
    api.headers["Authorization"] = f"Bearer {OWNER}"
    base = f"http://127.0.0.1:{port}"
    try:
        yield base, api
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


def test_runner_program_runs_a_job_against_a_live_server(live) -> None:
    base, api = live
    token = api.post(f"{base}/api/runners", json={"name": "box"}).json()["token"]
    seen: dict = {}

    async def fake_profile(source, job_id, log_fn, on_run, github_auth) -> Profiled:
        seen.update(url=source.url, ref=source.ref, github_auth=github_auth)
        log_fn("[thinair] cloning (fake)")
        on_run(ProfileResult.model_validate(RESULT).runs[0])
        await asyncio.sleep(1.2)  # long enough for a periodic flush
        return Profiled(ProfileResult.model_validate(RESULT), "b" * 40, "node")

    job = api.post(f"{base}/api/jobs", json={"repo_url": URL}).json()
    client = RunnerClient(base, token, profile=fake_profile, wait_s=5)
    got = client.claim()
    assert got["id"] == job["id"]
    asyncio.run(client.run_job(got))
    assert seen == {"url": URL, "ref": None, "github_auth": True}
    done = api.get(f"{base}/api/jobs/{job['id']}").json()
    assert done["status"] == "succeeded" and done["commit_sha"] == "b" * 40
    lines = [e.data.get("line") for e in EventStore().fetch(job["id"])]
    assert "[thinair] cloning (fake)" in lines
    assert api.get(f"{base}/api/jobs/{job['id']}/profile").json()["recommended"]["cpus"] == 2.0


def test_runner_program_stops_a_cancelled_job(live) -> None:
    base, api = live
    token = api.post(f"{base}/api/runners", json={"name": "box"}).json()["token"]
    state = {"started": threading.Event(), "cancelled": False}

    async def slow_profile(source, job_id, log_fn, on_run, github_auth) -> Profiled:
        state["started"].set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        raise AssertionError("not reached")

    job = api.post(f"{base}/api/jobs", json={"repo_url": URL}).json()
    client = RunnerClient(base, token, profile=slow_profile, wait_s=5)
    got = client.claim()
    worker = threading.Thread(target=lambda: asyncio.run(client.run_job(got)))
    worker.start()
    assert state["started"].wait(10)
    api.post(f"{base}/api/jobs/{job['id']}/cancel")
    worker.join(30)
    assert not worker.is_alive() and state["cancelled"]
    assert api.get(f"{base}/api/jobs/{job['id']}").json()["status"] == "cancelled"


def test_runner_program_refuses_bad_payloads_and_wrong_secrets(live) -> None:
    base, api = live
    with pytest.raises(RunnerSetupError):
        asyncio.run(RunnerClient(base, "thinair_runner_wrong", wait_s=0).run_forever())
    token = api.post(f"{base}/api/runners", json={"name": "box"}).json()["token"]
    called = []

    async def never(*args) -> Profiled:
        called.append(args)
        raise AssertionError

    client = RunnerClient(base, token, profile=never)
    for job in [
        {"id": "x", "kind": "agent", "repo_url": URL},
        {"id": "x", "kind": "profile", "repo_url": "https://evil.example.com/a/b"},
        {"id": "x", "kind": "profile", "repo_url": URL, "ref": "--upload-pack=sh"},
    ]:
        body = asyncio.run(client._work(job, lambda _: None, lambda _: None))
        assert body["outcome"] == "failed", body
    assert called == []
