"""Agent conversations through the API: starting one, replying, answering approvals, ending it.
The executor is a fake that records what reaches it (the real path is covered by test_acp.py
for the driver and test_conversation_docker.py for the sandbox's stdin). No Docker."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.config import get_settings
from thinair.db.models import JobKind
from thinair.jobs.service import ExecResult, JobContext

OWNER = "o" * 40
URL = "https://github.com/octo/demo"
received: list[dict] = []


async def chatty(ctx: JobContext, _mgr) -> ExecResult:
    """Takes input only if the job is a conversation; finishes on {"type": "end"}."""
    if not ctx.job.interactive:
        await asyncio.sleep(3600)
        return ExecResult()
    ended = asyncio.Event()

    async def on_input(cmd: dict) -> bool:
        received.append(cmd)
        if cmd["type"] == "end":
            ended.set()
        return True

    await asyncio.sleep(0.05)  # "starting": not ready for input yet
    ctx.on_input = on_input
    await ended.wait()
    return ExecResult()


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", OWNER)
    get_settings.cache_clear()
    api_auth._failures.clear()
    received.clear()
    app = create_app({JobKind.PROFILE: chatty, JobKind.AGENT: chatty})
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {OWNER}"
        yield client


def start(c, **kw):
    body = {"repo_url": URL, "kind": "agent", "agent_id": "claude-acp", "task": "Fix it", **kw}
    return c.post("/api/jobs", json=body, headers=kw.pop("headers", None))


def send_when_ready(c, job_id, body, headers=None, timeout=5):
    end = time.monotonic() + timeout
    while True:
        r = c.post(f"/api/jobs/{job_id}/input", json=body, headers=headers)
        if r.status_code != 409 or r.json()["detail"]["code"] != "not_ready":
            return r
        assert time.monotonic() < end, "never became ready"
        time.sleep(0.02)


def wait_status(c, job_id, status, timeout=5):
    end = time.monotonic() + timeout
    while c.get(f"/api/jobs/{job_id}").json()["status"] != status:
        assert time.monotonic() < end
        time.sleep(0.02)


def test_a_conversation_takes_replies_and_approvals_then_ends(c) -> None:
    job = start(c, interactive=True, ask_permissions=True).json()
    assert job["interactive"] is True and job["ask_permissions"] is True

    r = send_when_ready(c, job["id"], {"type": "message", "text": "  also the README  "})
    assert r.status_code == 202 and r.json() == {"delivered": True}
    ok = c.post(
        f"/api/jobs/{job['id']}/input", json={"type": "permission", "request": 1, "option": "yes"}
    )
    assert ok.status_code == 202
    deny = c.post(f"/api/jobs/{job['id']}/input", json={"type": "permission", "request": 2})
    assert deny.status_code == 202
    assert c.post(f"/api/jobs/{job['id']}/input", json={"type": "stop"}).status_code == 202
    assert c.post(f"/api/jobs/{job['id']}/input", json={"type": "end"}).status_code == 202
    wait_status(c, job["id"], "succeeded")
    assert received == [
        {"type": "message", "text": "also the README"},
        {"type": "permission", "request": 1, "option": "yes"},
        {"type": "permission", "request": 2, "option": None},
        {"type": "stop"},
        {"type": "end"},
    ]
    late = c.post(f"/api/jobs/{job['id']}/input", json={"type": "message", "text": "hi"})
    assert late.status_code == 409 and late.json()["detail"]["code"] == "finished"

    again = c.post(f"/api/jobs/{job['id']}/retry").json()  # a retry is a conversation too
    assert again["interactive"] is True and again["ask_permissions"] is True


def test_bad_input_is_refused_with_a_reason(c) -> None:
    job = start(c, interactive=True).json()
    assert job["ask_permissions"] is False
    path = f"/api/jobs/{job['id']}/input"
    assert send_when_ready(c, job["id"], {"type": "message", "text": "   "}).status_code == 422
    assert c.post(path, json={"type": "permission"}).json()["detail"]["code"] == "missing_request"
    assert c.post(path, json={"type": "dance"}).status_code == 422
    assert c.post(path, json={"type": "message", "text": "x" * 10_001}).status_code == 422
    c.post(path, json={"type": "end"})

    one_shot = start(c).json()
    r = c.post(f"/api/jobs/{one_shot['id']}/input", json={"type": "end"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_conversational"
    c.post(f"/api/jobs/{one_shot['id']}/cancel")


def test_only_acp_agents_and_agent_tasks_can_converse(c) -> None:
    r = start(c, agent_id="claude", interactive=True)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "not_conversational"
    assert "ACP" in r.json()["detail"]["message"]
    r = c.post("/api/jobs", json={"repo_url": URL, "kind": "profile", "interactive": True})
    assert r.status_code == 422
    # ask_permissions without a conversation means nothing and is not stored
    plain = start(c, agent_id="claude-acp", ask_permissions=True).json()
    assert plain["interactive"] is False and plain["ask_permissions"] is False
    c.post(f"/api/jobs/{plain['id']}/cancel")
    agents = {a["id"]: a for a in c.get("/api/agents").json()}
    assert agents["claude-acp"]["conversational"] is True
    assert agents["claude"]["conversational"] is False


def test_only_the_person_whose_job_it_is_can_answer(c) -> None:
    bob = c.post("/api/users", json={"name": "bob"}).json()["token"]
    bh = {"Authorization": f"Bearer {bob}"}
    mine = start(c, interactive=True).json()  # the owner's
    r = c.post(f"/api/jobs/{mine['id']}/input", json={"type": "end"}, headers=bh)
    assert r.status_code == 404  # members don't even see it
    bobs = c.post(
        "/api/jobs",
        json={
            "repo_url": URL,
            "kind": "agent",
            "agent_id": "claude-acp",
            "task": "t",
            "interactive": True,
        },
        headers=bh,
    ).json()
    r = c.post(f"/api/jobs/{bobs['id']}/input", json={"type": "end"})  # admin, but not bob
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_your_job"
    send_when_ready(c, mine["id"], {"type": "end"})  # one job slot: bob's runs after this
    assert send_when_ready(c, bobs["id"], {"type": "end"}, headers=bh).status_code == 202
