"""Projects: the repo list with job counts and latest job, and per-project defaults. No Docker."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.config import get_settings
from thinair.db.models import JobKind
from thinair.jobs.service import ExecResult
from thinair.project.prepare import cache_volume

OWNER = "o" * 40
A = "https://github.com/octo/alpha"
B = "https://github.com/octo/beta"


async def quick(_ctx, _mgr) -> ExecResult:
    return ExecResult()


async def forever(_ctx, _mgr) -> ExecResult:
    await asyncio.sleep(3600)
    return ExecResult()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", OWNER)
    get_settings.cache_clear()
    api_auth._failures.clear()
    app = create_app({JobKind.PROFILE: quick, JobKind.AGENT: forever})
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {OWNER}"
        yield c


def h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_projects_list_counts_and_orders_by_last_use(client) -> None:
    c = client
    empty = c.post("/api/repos", json={"url": "https://github.com/octo/empty"}).json()
    a1 = c.post("/api/jobs", json={"repo_url": A}).json()
    c.post("/api/jobs", json={"repo_url": B})
    a3 = c.post(
        "/api/jobs",
        json={"repo_url": A, "kind": "agent", "agent_id": "claude", "task": "Fix it"},
    ).json()

    repos = c.get("/api/repos").json()
    assert [r["name"] for r in repos] == ["alpha", "beta", "empty"]  # most recently used first
    alpha = repos[0]
    assert alpha["jobs"] == 2 and alpha["active_jobs"] == 1  # the agent job never finishes
    assert alpha["last_job"]["id"] == a3["id"] and alpha["last_job"]["task"] == "Fix it"
    assert repos[2]["jobs"] == 0 and repos[2]["last_job"] is None

    one = c.get(f"/api/repos/{a1['repo_id']}").json()
    assert one["jobs"] == 2 and one["last_job"]["kind"] == "agent"
    assert c.get(f"/api/repos/{empty['id']}").json()["jobs"] == 0


def test_members_count_only_their_own_jobs(client) -> None:
    c = client
    c.post("/api/jobs", json={"repo_url": A})
    c.post("/api/jobs", json={"repo_url": B})
    bob = c.post("/api/users", json={"name": "bob"}).json()["token"]
    mine = c.post("/api/jobs", json={"repo_url": A}, headers=h(bob)).json()

    repos = c.get("/api/repos", headers=h(bob)).json()
    assert [r["name"] for r in repos] == ["alpha"]  # beta has only the owner's jobs
    assert repos[0]["jobs"] == 1 and repos[0]["last_job"]["id"] == mine["id"]
    assert [r["jobs"] for r in c.get("/api/repos").json()] == [2, 1]  # the owner sees all


def test_project_defaults_fill_in_new_jobs(client) -> None:
    c = client
    repo = c.post("/api/repos", json={"url": A}).json()
    r = c.patch(
        f"/api/repos/{repo['id']}", json={"default_agent_id": "codex", "default_ref": "dev"}
    )
    assert r.status_code == 200
    assert (r.json()["default_agent_id"], r.json()["default_ref"]) == ("codex", "dev")

    job = c.post("/api/jobs", json={"repo_id": repo["id"], "kind": "agent", "task": "t"}).json()
    assert (job["agent_id"], job["ref"]) == ("codex", "dev")
    by_url = c.post("/api/jobs", json={"repo_url": A + "/", "kind": "profile"}).json()
    assert by_url["ref"] == "dev" and by_url["repo_id"] == repo["id"]
    picked = c.post(
        "/api/jobs",
        json={
            "repo_id": repo["id"],
            "kind": "agent",
            "task": "t",
            "agent_id": "claude",
            "ref": "x",
        },
    ).json()
    assert (picked["agent_id"], picked["ref"]) == ("claude", "x")  # explicit choices win

    cleared = c.patch(
        f"/api/repos/{repo['id']}", json={"default_agent_id": "", "default_ref": None}
    )
    assert cleared.json()["default_agent_id"] is None and cleared.json()["default_ref"] is None
    no_agent = c.post("/api/jobs", json={"repo_id": repo["id"], "kind": "agent", "task": "t"})
    assert no_agent.status_code == 422 and no_agent.json()["detail"]["code"] == "unknown_agent"


def test_project_defaults_are_checked_and_admin_only(client) -> None:
    c = client
    repo = c.post("/api/repos", json={"url": A}).json()
    url = f"/api/repos/{repo['id']}"
    assert (
        c.patch(url, json={"default_agent_id": "nope"}).json()["detail"]["code"] == "unknown_agent"
    )
    assert c.patch(url, json={"default_ref": "-x"}).json()["detail"]["code"] == "bad_ref"
    assert c.patch("/api/repos/missing", json={}).status_code == 404
    bob = c.post("/api/users", json={"name": "bob"}).json()["token"]
    assert c.patch(url, json={"default_ref": "dev"}, headers=h(bob)).status_code == 403


def test_dependency_caches_are_per_user_and_per_repo() -> None:
    assert cache_volume(A, "admin") != cache_volume(A, "bob")
    assert cache_volume(A, "bob") != cache_volume(B, "bob")
    assert cache_volume(A, "bob") == cache_volume(A, "bob")
    assert cache_volume(A, "bob").startswith("thinair-cache-")


def test_one_repository_in_any_spelling_is_one_project(client) -> None:
    """Regression (audit A9): GitHub names are case-insensitive and `.git` is optional, but each
    spelling became its own project (split history, defaults and visibility)."""
    c = client
    ids = {
        c.post("/api/repos", json={"url": u}).json()["id"]
        for u in (
            "https://github.com/Octo/Gamma",
            "https://github.com/octo/gamma",
            "https://github.com/octo/gamma.git",
            "https://github.com/OCTO/gamma/",
        )
    }
    assert len(ids) == 1
    job = c.post("/api/jobs", json={"repo_url": "https://github.com/octo/GAMMA.git"}).json()
    assert job["repo_id"] in ids
    assert [r["name"] for r in c.get("/api/repos").json()] == ["Gamma"]  # the first spelling stays


def test_dot_segments_are_not_repository_names(client) -> None:
    for bad in ("https://github.com/../x", "https://github.com/x/..", "https://github.com/./x"):
        r = client.post("/api/repos", json={"url": bad})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_repo_url", bad


def test_an_open_event_stream_ends_when_its_token_is_revoked(client, monkeypatch) -> None:
    """Audit A15: the token was checked only when the stream opened, so a revoked phone (or a
    disabled user) kept receiving a running job's output until the job ended."""
    import threading

    from sqlmodel import select

    from thinair.api import routes
    from thinair.auth import create_token, revoke_token
    from thinair.db.models import ApiToken
    from thinair.db.session import session_scope

    monkeypatch.setattr(routes, "STREAM_RECHECK_S", 0.1, raising=False)
    client.app.state.jobs.events.heartbeat_s = 0.1
    with session_scope() as s:
        phone = create_token(s, "phone")
    job = client.post(
        "/api/jobs", json={"repo_url": A, "kind": "agent", "agent_id": "claude", "task": "x"}
    ).json()  # runs forever
    opened, ended = threading.Event(), threading.Event()

    def read() -> None:
        with client.stream(
            "GET", f"/api/jobs/{job['id']}/events", headers=h(phone), timeout=10
        ) as r:
            assert r.status_code == 200
            for _ in r.iter_lines():
                opened.set()
        ended.set()

    t = threading.Thread(target=read, daemon=True)
    t.start()
    assert opened.wait(10)
    assert not ended.wait(0.5)  # the job is still running: the stream stays open
    with session_scope() as s:
        row = s.exec(select(ApiToken).where(ApiToken.name == "phone")).one()
        revoke_token(s, row.id)
    assert ended.wait(10), "the stream kept running after the token was revoked"
