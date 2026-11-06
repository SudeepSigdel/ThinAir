"""HTTP API tests: the real FastAPI app and job service, with fake executors (no Docker)."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.config import get_settings
from thinair.db.models import JobKind, JobStatus, ProfileReport
from thinair.db.session import session_scope
from thinair.jobs.service import ExecResult, JobContext
from thinair.project.prepare import PrepareError

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "https://github.com/octo/demo"
MiB = 1024**2


class Fakes:
    """Controllable fake executors shared by a test."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.gate.set()  # open by default; tests close it to hold a job in "running"
        self.calls: list[str] = []
        self.fail_with: Exception | None = None

    async def agent(self, ctx: JobContext, _mgr) -> ExecResult:
        from thinair.db.models import EventType, JobResult

        ctx.emit(EventType.AGENT_MSG, {"kind": "text", "text": f"Working on: {ctx.job.task}"})
        ctx.emit(EventType.AGENT_MSG, {"kind": "tool", "text": "Bash: npm test"})
        with session_scope() as s:
            s.add(
                JobResult(
                    job_id=ctx.job.id,
                    exit_status=0,
                    summary="Fixed it.",
                    diff_text="--- a/x\n+++ b/x\n+fixed",
                    diff_stat={"files": 1, "insertions": 1, "deletions": 0},
                )
            )
            s.commit()
        return ExecResult()

    async def profile(self, ctx: JobContext, _mgr) -> ExecResult:
        import asyncio

        self.calls.append(ctx.job.id)
        ctx.log("cloning")
        await asyncio.to_thread(self.gate.wait, 10)
        if self.fail_with:
            raise self.fail_with
        with session_scope() as s:
            s.add(
                ProfileReport(
                    job_id=ctx.job.id,
                    min_config={"cpus": 0.5, "mem_bytes": 256 * MiB},
                    recommended={"cpus": 1, "mem_bytes": 384 * MiB},
                    report={"status": "ok", "runs": []},
                    report_md="# Resource profile\n\n**Minimum passing:** 0.5 CPU / 256 MiB",
                )
            )
            s.commit()
        ctx.log("done")
        return ExecResult()


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
def client(monkeypatch, fakes):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", TOKEN)
    get_settings.cache_clear()
    api_auth._failures.clear()
    with TestClient(create_app({JobKind.PROFILE: fakes.profile, JobKind.AGENT: fakes.agent})) as c:
        c.headers.update(AUTH)
        yield c
    fakes.gate.set()


def wait_finished(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["finished"]:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {job}")


def wait_status(client: TestClient, job_id: str, status: str, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if client.get(f"/api/jobs/{job_id}").json()["status"] == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"job never reached {status}")


# ---- auth ----------------------------------------------------------------------------------
ROUTES = [
    ("GET", "/api/repos"),
    ("POST", "/api/repos"),
    ("GET", "/api/repos/x"),
    ("GET", "/api/jobs"),
    ("POST", "/api/jobs"),
    ("GET", "/api/jobs/x"),
    ("POST", "/api/jobs/x/cancel"),
    ("POST", "/api/jobs/x/retry"),
    ("GET", "/api/jobs/x/events"),
    ("GET", "/api/jobs/x/profile"),
    ("GET", "/api/jobs/x/profile.md"),
    ("GET", "/api/agents"),
    ("GET", "/api/jobs/x/result"),
    ("POST", "/api/jobs/x/pr"),
    ("GET", "/api/jobs/x/patch"),
    ("GET", "/api/users"),
    ("POST", "/api/users"),
    ("POST", "/api/users/x/tokens"),
    ("POST", "/api/users/x/disable"),
    ("GET", "/api/runners"),
    ("POST", "/api/runners"),
    ("POST", "/api/runners/x/disable"),
    ("POST", "/runner/v1/claim"),
    ("POST", "/runner/v1/jobs/x/events"),
    ("POST", "/runner/v1/jobs/x/finish"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_requires_a_token(client, method, path) -> None:
    anon = TestClient(client.app)  # same app, no credentials
    api_auth._failures.clear()
    assert anon.request(method, path, json={}).status_code == 401


def test_docs_do_not_leak_data_and_health_stays_open(client) -> None:
    anon = TestClient(client.app)
    assert anon.get("/healthz").status_code == 200


def test_cookie_login_works_for_the_event_stream(client) -> None:
    anon = TestClient(client.app)
    job = client.post("/api/jobs", json={"repo_url": URL}).json()
    wait_finished(client, job["id"])
    assert anon.get(f"/api/jobs/{job['id']}/events").status_code == 401
    assert anon.post("/api/login", json={"token": TOKEN}).status_code == 204
    r = anon.get(f"/api/jobs/{job['id']}/events")  # EventSource sends only the cookie
    assert r.status_code == 200 and "text/event-stream" in r.headers["content-type"]


# ---- repos ---------------------------------------------------------------------------------
def test_repos_are_validated_and_idempotent(client) -> None:
    a = client.post("/api/repos", json={"url": URL + "/"})
    b = client.post("/api/repos", json={"url": URL})
    assert a.status_code == 201 and a.json()["id"] == b.json()["id"]
    assert a.json()["name"] == "demo"
    assert len(client.get("/api/repos").json()) == 1
    assert client.get(f"/api/repos/{a.json()['id']}").json()["url"] == URL
    bad = client.post("/api/repos", json={"url": "https://gitlab.com/a/b"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_repo_url"
    assert client.get("/api/repos/nope").json()["detail"]["code"] == "repo_not_found"


# ---- jobs ----------------------------------------------------------------------------------
def test_create_poll_and_fetch_the_profile(client, fakes) -> None:
    r = client.post("/api/jobs", json={"repo_url": URL})
    assert r.status_code == 201
    job = r.json()
    assert job["kind"] == "profile" and job["repo_name"] == "demo"
    done = wait_finished(client, job["id"])
    assert done["status"] == "succeeded" and done["started_at"] and done["finished_at"]

    prof = client.get(f"/api/jobs/{job['id']}/profile").json()
    assert prof["min_config"] == {"cpus": 0.5, "mem_bytes": 256 * MiB}
    assert prof["recommended"]["cpus"] == 1
    md = client.get(f"/api/jobs/{job['id']}/profile.md")
    assert md.headers["content-type"].startswith("text/markdown")
    assert "**Minimum passing:**" in md.text


def test_job_creation_validation(client) -> None:
    assert client.post("/api/jobs", json={}).status_code == 422  # needs a repo
    both = client.post("/api/jobs", json={"repo_url": URL, "repo_id": "x"})
    assert both.status_code == 422
    bad = client.post("/api/jobs", json={"repo_url": "file:///etc"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_repo_url"
    no_agent = client.post("/api/jobs", json={"repo_url": URL, "kind": "agent", "task": "fix it"})
    assert no_agent.status_code == 422 and no_agent.json()["detail"]["code"] == "unknown_agent"
    bogus = {"repo_url": URL, "kind": "agent", "agent_id": "nope", "task": "fix it"}
    assert client.post("/api/jobs", json=bogus).json()["detail"]["code"] == "unknown_agent"
    blank = {"repo_url": URL, "kind": "agent", "agent_id": "claude", "task": "   "}
    assert client.post("/api/jobs", json=blank).json()["detail"]["code"] == "missing_task"
    missing = client.post("/api/jobs", json={"repo_id": "nope"})
    assert missing.status_code == 404
    huge = client.post("/api/jobs", json={"repo_url": URL, "task": "x" * 20_000})
    assert huge.status_code == 422


def test_create_job_from_a_registered_repo(client) -> None:
    repo = client.post("/api/repos", json={"url": URL}).json()
    job = client.post("/api/jobs", json={"repo_id": repo["id"]}).json()
    assert job["repo_id"] == repo["id"]
    wait_finished(client, job["id"])


def test_listing_filtering_and_paging(client) -> None:
    ids = []
    for _ in range(5):
        j = client.post("/api/jobs", json={"repo_url": URL}).json()
        wait_finished(client, j["id"])
        ids.append(j["id"])
    listed = client.get("/api/jobs").json()
    assert [j["id"] for j in listed] == ids[::-1]  # newest first
    page1 = client.get("/api/jobs", params={"limit": 2}).json()
    page2 = client.get("/api/jobs", params={"limit": 2, "before": page1[-1]["id"]}).json()
    assert [j["id"] for j in page1 + page2] == ids[::-1][:4]
    assert client.get("/api/jobs", params={"status": "failed"}).json() == []
    assert len(client.get("/api/jobs", params={"status": "succeeded"}).json()) == 5
    assert client.get("/api/jobs", params={"limit": 0}).status_code == 422
    assert client.get("/api/jobs", params={"status": "bogus"}).status_code == 422


def test_failed_job_reports_a_readable_error(client, fakes) -> None:
    fakes.fail_with = PrepareError("install_failed", "The install step failed: exit 7.")
    job = client.post("/api/jobs", json={"repo_url": URL}).json()
    done = wait_finished(client, job["id"])
    assert done["status"] == "failed" and done["error_code"] == "install_failed"
    assert "exit 7" in done["error_message"]
    assert client.get(f"/api/jobs/{job['id']}/profile").json()["detail"]["code"] == "no_profile"


def test_cancel_a_running_job(client, fakes) -> None:
    fakes.gate.clear()
    job = client.post("/api/jobs", json={"repo_url": URL}).json()
    wait_status(client, job["id"], "running")
    r = client.post(f"/api/jobs/{job['id']}/cancel")
    assert r.status_code == 200
    assert wait_finished(client, job["id"])["status"] == "cancelled"
    again = client.post(f"/api/jobs/{job['id']}/cancel")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_cancellable"
    assert client.post("/api/jobs/nope/cancel").status_code == 404


def test_retry_makes_a_new_job_only_after_the_first_finished(client, fakes) -> None:
    fakes.gate.clear()
    first = client.post("/api/jobs", json={"repo_url": URL, "ref": "main"}).json()
    wait_status(client, first["id"], "running")
    early = client.post(f"/api/jobs/{first['id']}/retry")
    assert early.status_code == 409 and early.json()["detail"]["code"] == "still_running"
    fakes.gate.set()
    wait_finished(client, first["id"])
    retry = client.post(f"/api/jobs/{first['id']}/retry")
    assert retry.status_code == 201
    new = retry.json()
    assert new["id"] != first["id"] and new["repo_id"] == first["repo_id"] and new["ref"] == "main"
    wait_finished(client, new["id"])


def test_unknown_job_is_404_everywhere(client) -> None:
    for path in ("", "/events", "/profile", "/profile.md"):
        r = client.get(f"/api/jobs/nope{path}")
        assert r.status_code == 404, path
        assert r.json()["detail"]["code"] in {"job_not_found", "no_profile"}


# ---- event stream --------------------------------------------------------------------------
def sse_frames(client: TestClient, job_id: str, **kw) -> list[dict]:
    frames = []
    with client.stream("GET", f"/api/jobs/{job_id}/events", **kw) as r:
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-cache"
        block: dict[str, str] = {}
        for line in r.iter_lines():
            if line == "":
                if block:
                    frames.append(block)
                block = {}
            elif not line.startswith(":"):
                k, _, v = line.partition(": ")
                block[k] = v
    return frames


def test_event_stream_replays_resumes_and_ends(client) -> None:
    job = client.post("/api/jobs", json={"repo_url": URL}).json()
    wait_finished(client, job["id"])
    frames = sse_frames(client, job["id"])
    assert [f["id"] for f in frames] == [str(i) for i in range(1, len(frames) + 1)]
    lines = [json.loads(f["data"])["data"]["line"] for f in frames if f["event"] == "log"]
    assert lines == ["cloning", "done"]
    assert json.loads(frames[-1]["data"])["data"]["status"] == "succeeded"

    # a browser reconnects with Last-Event-ID and gets only what it missed
    resumed = sse_frames(client, job["id"], headers={"Last-Event-ID": "3"})
    assert [f["id"] for f in resumed] == [str(i) for i in range(4, len(frames) + 1)]
    assert sse_frames(client, job["id"], params={"after": len(frames)}) == []
    # garbage in the header just means "from the start"
    assert len(sse_frames(client, job["id"], headers={"Last-Event-ID": "abc"})) == len(frames)


def test_event_stream_follows_a_running_job_live(client, fakes) -> None:
    fakes.gate.clear()
    job = client.post("/api/jobs", json={"repo_url": URL}).json()
    wait_status(client, job["id"], "running")
    threading.Timer(0.3, fakes.gate.set).start()  # job finishes while the stream is open
    frames = sse_frames(client, job["id"])
    kinds = [
        json.loads(f["data"])["data"].get("status") or json.loads(f["data"])["data"].get("line")
        for f in frames
    ]
    assert kinds[0] == "queued" and kinds[-1] == "succeeded" and "done" in kinds


def test_agents_endpoint_lists_the_builtin_claude_config(client) -> None:
    agents = {a["id"]: a for a in client.get("/api/agents").json()}
    assert agents["claude"]["name"] == "Claude Code"
    assert "thinair auth claude" in agents["claude"]["login_hint"]
    assert JobStatus.QUEUED.value == "queued"


def test_agent_job_end_to_end_through_the_api(client) -> None:
    body = {"repo_url": URL, "kind": "agent", "agent_id": "claude", "task": "fix the tests"}
    job = client.post("/api/jobs", json=body).json()
    assert job["kind"] == "agent" and job["agent_id"] == "claude" and job["task"] == "fix the tests"
    assert wait_finished(client, job["id"])["status"] == "succeeded"
    result = client.get(f"/api/jobs/{job['id']}/result").json()
    assert result["summary"] == "Fixed it." and "+fixed" in result["diff"]
    assert result["diff_stat"]["files"] == 1
    events = [
        json.loads(f["data"]) for f in sse_frames(client, job["id"]) if f["event"] == "agent_msg"
    ]
    assert [e["data"]["text"] for e in events] == ["Working on: fix the tests", "Bash: npm test"]
    # retry keeps the agent and the task
    wait_finished(client, job["id"])
    again = client.post(f"/api/jobs/{job['id']}/retry").json()
    assert (again["agent_id"], again["task"]) == ("claude", "fix the tests")
    wait_finished(client, again["id"])


def test_profile_job_has_no_agent_result(client) -> None:
    job = client.post("/api/jobs", json={"repo_url": URL}).json()
    wait_finished(client, job["id"])
    r = client.get(f"/api/jobs/{job['id']}/result")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "no_result"


def test_openapi_docs_offer_token_login(client) -> None:
    """/docs must show an Authorize button: the schema declares the bearer scheme."""
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert any(s["type"] == "http" and s["scheme"] == "bearer" for s in schemes.values())
    assert schema["paths"]["/api/jobs"]["get"]["security"]
