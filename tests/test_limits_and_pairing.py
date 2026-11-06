"""Per-member job limits and QR pairing (one-time codes that sign a phone in). No Docker."""

import asyncio
import hashlib
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from typer.testing import CliRunner

from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.cli.main import app as cli
from thinair.config import get_settings
from thinair.db.migrate import upgrade
from thinair.db.models import JobKind, PairingCode, User
from thinair.db.session import get_engine, session_scope
from thinair.jobs.service import ExecResult
from thinair.pairing import create_pairing_code, redeem
from thinair.users import create_user, disable_user, job_limit

OWNER = "o" * 40
URL = "https://github.com/octo/demo"


async def forever(ctx, _mgr) -> ExecResult:
    await asyncio.sleep(3600)  # stays unfinished until cancelled
    return ExecResult()


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", OWNER)
    get_settings.cache_clear()
    api_auth._failures.clear()
    a = create_app({JobKind.PROFILE: forever, JobKind.AGENT: forever})
    with TestClient(a) as c:
        c.headers["Authorization"] = f"Bearer {OWNER}"
        yield c


def h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def wait_status(c, job_id, status, timeout=5) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if c.get(f"/api/jobs/{job_id}").json()["status"] == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"{job_id} never became {status}")


# ---- job limits ---------------------------------------------------------------------------------
def test_who_is_limited_and_by_how_much(monkeypatch) -> None:
    assert job_limit(None) is None  # the owner
    assert job_limit(User(name="a", is_admin=True, max_active_jobs=1)) is None  # admins never
    assert job_limit(User(name="m")) == 5  # the default
    assert job_limit(User(name="m", max_active_jobs=2)) == 2
    assert job_limit(User(name="m", max_active_jobs=0)) is None  # 0 = no limit
    monkeypatch.setenv("THINAIR_MEMBER_JOB_LIMIT", "0")
    get_settings.cache_clear()
    assert job_limit(User(name="m")) is None
    assert job_limit(User(name="m", max_active_jobs=3)) == 3  # a personal limit still applies


def test_members_hit_their_limit_and_admins_set_it(app) -> None:
    c = app
    bob = c.post("/api/users", json={"name": "bob"}).json()["token"]
    r = c.post("/api/users/bob/limit", json={"max_active_jobs": 2})
    assert r.status_code == 200 and r.json()["max_active_jobs"] == 2
    assert (
        c.post("/api/users/bob/limit", json={"max_active_jobs": 2}, headers=h(bob)).status_code
        == 403
    )

    first = c.post("/api/jobs", json={"repo_url": URL}, headers=h(bob)).json()
    second = c.post("/api/jobs", json={"repo_url": URL}, headers=h(bob)).json()
    wait_status(c, first["id"], "running")
    third = c.post("/api/jobs", json={"repo_url": URL}, headers=h(bob))
    assert third.status_code == 429
    assert third.json()["detail"]["code"] == "too_many_jobs"
    assert "(2)" in third.json()["detail"]["message"]

    for _ in range(4):  # the owner (and admins) are never limited
        assert c.post("/api/jobs", json={"repo_url": URL}).status_code == 201

    c.post(f"/api/jobs/{second['id']}/cancel", headers=h(bob))
    wait_status(c, second["id"], "cancelled")
    assert c.post("/api/jobs", json={"repo_url": URL}, headers=h(bob)).status_code == 201

    assert (
        c.post("/api/users/bob/limit", json={"max_active_jobs": None}).json()["max_active_jobs"]
        is None
    )
    assert c.post("/api/users/nobody/limit", json={"max_active_jobs": 1}).status_code == 404
    assert c.post("/api/users/bob/limit", json={"max_active_jobs": -1}).status_code == 422


def test_cli_sets_and_shows_limits(monkeypatch) -> None:
    upgrade(get_engine())
    with session_scope() as s:
        create_user(s, "alice")
        create_user(s, "root2", admin=True)
    runner = CliRunner()
    assert "max 5 unfinished jobs (default)" in runner.invoke(cli, ["user", "list"]).output
    assert runner.invoke(cli, ["user", "limit", "alice", "12"]).exit_code == 0
    out = runner.invoke(cli, ["user", "list"]).output
    assert (
        "max 12 unfinished jobs" in out and "(default)" not in out.split("alice")[1].split("\n")[0]
    )
    assert "no limit" in runner.invoke(cli, ["user", "limit", "alice", "0"]).output
    assert "server default" in runner.invoke(cli, ["user", "limit", "alice", "default"]).output
    assert runner.invoke(cli, ["user", "limit", "alice", "lots"]).exit_code == 2
    assert runner.invoke(cli, ["user", "limit", "ghost", "3"]).exit_code == 1


# ---- pairing ------------------------------------------------------------------------------------
def test_a_pairing_code_signs_in_once_as_its_user(app) -> None:
    c = app
    c.post("/api/users", json={"name": "bob"})
    with session_scope() as s:
        bob_id = s.exec(select(User.id).where(User.name == "bob")).one()
        code = create_pairing_code(s, bob_id)
        assert code not in str(s.get(PairingCode, hashlib.sha256(code.encode()).hexdigest()))
    phone = TestClient(c.app)  # no token, like a freshly opened phone browser
    r = phone.post("/api/pair", json={"code": code})
    assert r.status_code == 204 and "thinair_session" in r.headers["set-cookie"]
    me = phone.get("/api/me").json()  # the cookie alone works now
    assert me["user"] == "bob" and me["token_name"].startswith("phone (paired")
    again = TestClient(c.app).post("/api/pair", json={"code": code})
    assert again.status_code == 401 and again.json()["detail"]["code"] == "bad_pairing_code"


def test_expired_wrong_and_disabled_codes_are_refused() -> None:
    upgrade(get_engine())
    with session_scope() as s:
        old = create_pairing_code(s)
        row = s.exec(select(PairingCode)).one()
        row.expires_at = row.created_at - timedelta(seconds=1)
        s.add(row)
        s.commit()
        assert redeem(s, old) is None
        assert redeem(s, "thinair_pair_made-up") is None and redeem(s, OWNER) is None
        user = create_user(s, "carol")
        code = create_pairing_code(s, user.id)
        disable_user(s, "carol")
        assert redeem(s, code) is None
        owner_code = create_pairing_code(s)  # creating a code also clears expired ones
        assert len(s.exec(select(PairingCode)).all()) == 2
        assert redeem(s, owner_code).startswith("thinair_")


def test_qr_command_prints_a_working_link(monkeypatch) -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["qr"])
    assert res.exit_code == 1 and "THINAIR_PUBLIC_URL" in res.output  # nowhere for a phone to go
    monkeypatch.setenv("THINAIR_PUBLIC_URL", "https://box.example.ts.net")
    get_settings.cache_clear()
    res = runner.invoke(cli, ["qr"])
    assert res.exit_code == 0, res.output
    link = next(ln for ln in res.output.splitlines() if ln.startswith("Or open: "))
    assert link.startswith("Or open: https://box.example.ts.net/#pair=thinair_pair_")
    assert "▀" in res.output or "█" in res.output  # the QR code itself
    with session_scope() as s:
        assert redeem(s, link.split("#pair=")[1]).startswith("thinair_")
    assert runner.invoke(cli, ["qr", "--user", "ghost"]).exit_code == 1
