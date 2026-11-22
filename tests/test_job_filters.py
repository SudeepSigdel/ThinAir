"""The job list can be filtered by more than one status at once. No Docker."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.config import get_settings
from thinair.db.models import JobKind
from thinair.jobs.service import ExecResult

OWNER = "o" * 40
URL = "https://github.com/octo/alpha"


async def ok(_ctx, _mgr) -> ExecResult:
    return ExecResult()


async def forever(_ctx, _mgr) -> ExecResult:
    await asyncio.sleep(3600)
    return ExecResult()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", OWNER)
    get_settings.cache_clear()
    api_auth._failures.clear()
    with TestClient(create_app({JobKind.PROFILE: ok, JobKind.AGENT: forever})) as c:
        c.headers["Authorization"] = f"Bearer {OWNER}"
        yield c


def wait_for(client, job_id: str, status: str) -> None:
    for _ in range(300):
        if client.get(f"/api/jobs/{job_id}").json()["status"] == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"{job_id} never became {status}")


def test_status_can_be_given_more_than_once(client) -> None:
    done = client.post("/api/jobs", json={"repo_url": URL}).json()["id"]
    wait_for(client, done, "succeeded")
    running = client.post(
        "/api/jobs", json={"repo_url": URL, "kind": "agent", "agent_id": "claude", "task": "x"}
    ).json()["id"]
    wait_for(client, running, "running")
    client.post(f"/api/jobs/{running}/cancel")
    wait_for(client, running, "cancelled")
    queued_like = client.post(
        "/api/jobs", json={"repo_url": URL, "kind": "agent", "agent_id": "claude", "task": "y"}
    ).json()["id"]
    wait_for(client, queued_like, "running")

    def ids(query: str) -> set[str]:
        r = client.get(f"/api/jobs?{query}")
        assert r.status_code == 200, r.text
        return {j["id"] for j in r.json()}

    assert ids("") == {done, running, queued_like}
    assert ids("status=succeeded") == {done}  # one value, as before
    assert ids("status=succeeded&status=cancelled") == {done, running}
    assert ids("status=queued&status=running") == {queued_like}
    assert ids("status=failed&status=timeout") == set()
    client.post(f"/api/jobs/{queued_like}/cancel")


def test_an_unknown_status_is_a_clear_error(client) -> None:
    assert client.get("/api/jobs?status=running&status=bogus").status_code == 422
