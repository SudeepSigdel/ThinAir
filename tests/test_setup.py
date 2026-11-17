"""First-run: the setup checklist, skip/dismiss, the local funnel, `/api/meta`, `/api/health`,
`thinair stats` and short pairing codes. Fake executors, no Docker."""

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from thinair import doctor, setup_state
from thinair.api import auth as api_auth
from thinair.api import setup_api
from thinair.api.app import create_app
from thinair.auth import create_token
from thinair.cli.main import app as cli_app
from thinair.config import get_settings
from thinair.db.migrate import upgrade
from thinair.db.models import Job, JobKind, JobStatus, PairingCode, SetupEvent, User, utcnow
from thinair.db.session import get_engine, session_scope
from thinair.jobs.service import ExecResult
from thinair.pairing import (
    SHORT_MAX_FAILURES,
    create_pairing,
    create_pairing_code,
    format_short,
    normalise_short,
    redeem,
)

ADMIN = "t" * 32
H = {"Authorization": f"Bearer {ADMIN}"}
URL = "https://github.com/octo/demo"


async def noop(ctx, _mgr):
    return ExecResult()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", "")
    get_settings.cache_clear()
    api_auth._failures.clear()
    setup_api._meta_hits.clear()
    setup_api._building.clear()
    yield
    api_auth._failures.clear()


@pytest.fixture
def client():
    with TestClient(create_app({JobKind.PROFILE: noop, JobKind.AGENT: noop})) as c:
        yield c


def member_headers(name: str = "sam", admin: bool = False) -> dict:
    with session_scope() as s:
        u = User(name=name, is_admin=admin)
        s.add(u)
        s.commit()
        tok = create_token(s, "phone", user_id=u.id)
    return {"Authorization": f"Bearer {tok}"}


def steps(c, headers=H) -> dict:
    r = c.get("/api/setup", headers=headers)
    assert r.status_code == 200, r.text
    return {x["id"]: x for x in r.json()["steps"]} | {"_": r.json()}


# ---- the checklist ----------------------------------------------------------------------------
def test_a_fresh_owner_has_four_open_steps_and_nothing_done(client) -> None:
    st = steps(client)
    assert [k for k in st if k != "_"] == ["github", "agent", "project", "first_task"]
    assert not any(
        st[k]["done"] or st[k]["skipped"] for k in ("github", "agent", "project", "first_task")
    )
    assert (
        st["_"]["next"] == "github" and st["_"]["done_count"] == 0 and st["_"]["complete"] is False
    )


def test_steps_follow_real_state(client) -> None:
    client.post("/api/repos", json={"url": URL}, headers=H)
    st = steps(client)
    assert st["project"]["done"] and "1 repository" in st["project"]["detail"]
    r = client.post("/api/jobs", json={"repo_url": URL}, headers=H)
    assert r.status_code == 201
    assert steps(client)["first_task"]["done"]
    # a token in .env counts as GitHub connected (the existing way keeps working)
    assert steps(client)["github"]["done"] is False
    import os

    os.environ["THINAIR_GITHUB_TOKEN"] = "github_pat_" + "Ab1" * 20
    get_settings.cache_clear()
    try:
        g = steps(client)["github"]
        assert g["done"] and g["detail"] == "A token from .env"
    finally:
        os.environ["THINAIR_GITHUB_TOKEN"] = ""
        get_settings.cache_clear()


def test_agent_step_uses_the_preferred_agent_and_a_real_signal(client) -> None:
    r = client.post("/api/setup", json={"preferred_agent": "claude"}, headers=H)
    assert r.status_code == 200
    ag = steps(client)["agent"]
    assert (
        not ag["done"]
        and "isn't signed in" in ag["detail"]
        and ag["detail"].startswith("Claude Code")
    )
    # a sign-in or check in this server's memory
    client.app.state.logins.known["thinair-auth-claude"] = True
    assert steps(client)["agent"]["done"]
    client.app.state.logins.known.clear()
    assert not steps(client)["agent"]["done"]
    # or an agent task that already succeeded (survives a restart)
    with session_scope() as s:
        from thinair.db.models import Repo

        repo = Repo(url=URL, name="demo")
        s.add(repo)
        s.commit()
        s.add(
            Job(repo_id=repo.id, kind=JobKind.AGENT, agent_id="claude", status=JobStatus.SUCCEEDED)
        )
        s.commit()
    assert steps(client)["agent"]["done"]


def test_preferred_agent_is_validated_and_prepared_once(client, monkeypatch) -> None:
    built: list[str] = []

    async def fake_prebuild(cfg):
        built.append(cfg.id)

    monkeypatch.setattr(setup_api, "_prebuild", fake_prebuild)
    bad = client.post("/api/setup", json={"preferred_agent": "nope"}, headers=H)
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "unknown_agent"
    other = client.post("/api/setup", json={"preferred_agent": "other"}, headers=H).json()
    assert other["preferred_agent"] == "other" and built == []  # nothing to prepare
    out = client.post("/api/setup", json={"preferred_agent": "codex"}, headers=H).json()
    assert out["preferred_agent"] == "codex"
    import time

    for _ in range(50):
        if built:
            break
        time.sleep(0.05)
    assert built == ["codex"]


def test_skip_unskip_and_dismiss_persist(client) -> None:
    out = client.post("/api/setup", json={"skip": "github"}, headers=H).json()
    gh = next(x for x in out["steps"] if x["id"] == "github")
    assert gh["skipped"] and out["next"] == "agent" and out["done_count"] == 1
    assert steps(client)["github"]["skipped"]  # still there on the next request
    bad = client.post("/api/setup", json={"skip": "bogus"}, headers=H)
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "unknown_step"
    assert (
        client.post("/api/setup", json={"unskip": "github"}, headers=H).json()["next"] == "github"
    )
    assert client.post("/api/setup", json={"dismiss": True}, headers=H).json()["dismissed"] is True
    assert steps(client)["_"]["dismissed"] is True
    assert (
        client.post("/api/setup", json={"dismiss": False}, headers=H).json()["dismissed"] is False
    )


def test_all_steps_done_or_skipped_completes_setup(client) -> None:
    for step in ("github", "agent", "project", "first_task"):
        client.post("/api/setup", json={"skip": step}, headers=H)
    st = steps(client)["_"]
    assert st["complete"] and st["next"] is None and st["done_count"] == 4


def test_members_get_their_own_checklist(client) -> None:
    client.post("/api/repos", json={"url": URL}, headers=H)  # the owner's project
    sam = member_headers("sam")
    st = steps(client, sam)
    assert st["github"]["done"] and "admin" in st["github"]["detail"]  # managed by the admin
    assert not st["project"]["done"]  # sam sees only repos they have jobs on
    # skips are per person
    client.post("/api/setup", json={"skip": "agent"}, headers=sam)
    assert steps(client, sam)["agent"]["skipped"] and not steps(client)["agent"]["skipped"]
    client.post("/api/jobs", json={"repo_url": URL}, headers=sam)
    assert steps(client, sam)["project"]["done"] and steps(client, sam)["first_task"]["done"]
    assert not steps(client)["first_task"]["done"]  # the owner has run nothing


def test_setup_needs_a_login(client) -> None:
    assert client.get("/api/setup").status_code == 401
    assert client.post("/api/setup", json={"dismiss": True}).status_code == 401
    assert client.get("/api/health").status_code == 401


# ---- the funnel ---------------------------------------------------------------------------------
def _events(user_key: str = "owner") -> dict[str, SetupEvent]:
    from sqlmodel import select

    with session_scope() as s:
        return {
            r.name: r for r in s.exec(select(SetupEvent).where(SetupEvent.user_key == user_key))
        }


def test_events_are_stamped_once_at_the_moment_they_happen(client) -> None:
    ev = _events()
    assert "installed" in ev  # written at startup
    first = ev["installed"].at
    client.post("/api/repos", json={"url": URL}, headers=H)
    client.post("/api/repos", json={"url": URL}, headers=H)  # again: nothing new
    client.post("/api/repos", json={"url": URL + "2"}, headers=H)  # another project: still once
    client.post("/api/jobs", json={"repo_url": URL}, headers=H)
    client.post("/api/jobs", json={"repo_url": URL}, headers=H)
    ev = _events()
    assert {"project_added", "first_job"} <= set(ev)
    assert ev["installed"].at == first
    assert ev["first_job"].at >= ev["project_added"].at
    from sqlmodel import func, select

    with session_scope() as s:
        n = s.exec(
            select(func.count()).select_from(SetupEvent).where(SetupEvent.name == "first_job")
        ).one()
    assert n == 1


def test_first_agent_success_and_first_pr_are_recorded_when_jobs_end(client) -> None:
    import time

    r = client.post(
        "/api/jobs",
        json={"repo_url": URL, "kind": "agent", "agent_id": "claude", "task": "x"},
        headers=H,
    )
    assert r.status_code == 201
    for _ in range(100):
        if "first_agent_success" in _events():
            break
        time.sleep(0.05)
    job = client.get(f"/api/jobs/{r.json()['id']}", headers=H).json()
    assert "first_agent_success" in _events(), (job["status"], job["kind"], list(_events()))


def test_funnel_helper_orders_events_and_reports_gaps() -> None:
    upgrade(get_engine())
    t0 = utcnow()
    with session_scope() as s:
        s.add(SetupEvent(user_key="owner", name="installed", at=t0))
        s.add(SetupEvent(user_key="owner", name="paired", at=t0 + timedelta(seconds=240)))
        s.add(SetupEvent(user_key="owner", name="first_pr", at=t0 + timedelta(seconds=900)))
        s.add(SetupEvent(user_key="owner", name="skip:github"))  # choices are not funnel events
        s.commit()
        rows = setup_state.funnel(s, None)
    assert [r["name"] for r in rows] == ["installed", "paired", "first_pr"]
    assert [r["after_s"] for r in rows] == [None, 240, 660]


def test_unknown_funnel_events_are_a_programming_error() -> None:
    with pytest.raises(ValueError):
        setup_state.mark("made_up")


def test_a_database_without_tables_never_breaks_a_request() -> None:
    assert setup_state.mark("paired") is False  # no tables yet: swallowed, not raised


def test_stats_command_prints_the_local_funnel() -> None:
    upgrade(get_engine())
    setup_state.mark("installed")
    setup_state.mark("paired")
    out = CliRunner().invoke(cli_app, ["stats"])
    assert out.exit_code == 0, out.output
    assert (
        "never sent anywhere" in out.output and "installed" in out.output and "paired" in out.output
    )
    assert "not yet:" in out.output and "first_pr" in out.output


# ---- meta (open) ----------------------------------------------------------------------------------
def test_meta_is_open_small_and_throttled(client) -> None:
    from thinair import __version__

    r = client.get("/api/meta")  # no token
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "version": __version__,
        "api": 1,
        "features": ["github_app", "agent_login", "setup", "devices", "push"],
        "agent_idle_s": get_settings().agent_idle_s,
    }
    codes = [client.get("/api/meta").status_code for _ in range(70)]
    assert codes[:58].count(200) == 58 and codes[-1] == 429  # 60 a minute per address


# ---- health --------------------------------------------------------------------------------------
def fake_checks(*a, **k):
    return [
        doctor.Check("ok", "Docker", "27.3"),
        doctor.Check(
            "warn",
            "Sandbox images",
            "not prepared yet: node",
            "x",
            id="images",
            action=("post", "/api/health/fix/images", "Prepare now"),
        ),
        doctor.Check("fail", "Phone access", "no https address", "Run the installer again"),
    ]


def test_health_lists_problems_first_with_one_tap_actions(client, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "run_checks", fake_checks)
    setup_state.mark("installed")
    r = client.get("/api/health", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert [c["status"] for c in body["checks"]] == ["fail", "warn", "ok"] and body["problems"] == 2
    img = body["checks"][1]
    assert img["action"] == {
        "kind": "post",
        "target": "/api/health/fix/images",
        "label": "Prepare now",
    }
    assert body["checks"][0]["action"] is None
    assert [e["name"] for e in body["first_run"]][:1] == ["installed"]


def test_health_runs_offline_only(client, monkeypatch) -> None:
    seen = {}

    def spy(mgr=None, *, online=True):
        seen["online"] = online
        return []

    monkeypatch.setattr(doctor, "run_checks", spy)
    client.get("/api/health", headers=H)
    assert seen == {"online": False}  # opening the screen never sends a request anywhere


def test_health_is_for_admins(client, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "run_checks", fake_checks)
    sam = member_headers("sam")
    assert client.get("/api/health", headers=sam).status_code == 403
    assert client.post("/api/health/fix/images", headers=sam).status_code == 403


def test_prepare_images_runs_once_in_the_background(client, monkeypatch) -> None:
    import time

    import thinair.project.prepare as prepare
    import thinair.sandbox.manager as manager

    done: list[str] = []

    async def fake_ensure(mgr, stack, log):
        done.append(stack)
        await asyncio.sleep(0.2)

    monkeypatch.setattr(prepare, "ensure_image", fake_ensure)
    monkeypatch.setattr(manager, "SandboxManager", lambda *a, **k: object())
    first = client.post("/api/health/fix/images", headers=H)
    second = client.post("/api/health/fix/images", headers=H)
    assert first.status_code == 202 and first.json() == {"started": True}
    assert second.json() == {"started": False}  # single flight
    for _ in range(100):
        if len(done) == len(prepare.BUILDABLE_IMAGES):
            break
        time.sleep(0.05)
    assert sorted(done) == sorted(prepare.BUILDABLE_IMAGES)


def test_doctor_checks_carry_actions_for_the_app() -> None:
    checks = doctor._images(type("M", (), {"image_exists": lambda self, t: False})())
    assert checks[0].action == ("post", "/api/health/fix/images", "Prepare now")
    assert doctor._github_app().action == ("route", "#/setup/github", "Connect GitHub")


# ---- short pairing codes ---------------------------------------------------------------------------
def test_short_code_format_and_normalisation() -> None:
    upgrade(get_engine())
    with session_scope() as s:
        long_code, short = create_pairing(s, None, timedelta(minutes=30))
    assert short and len(short) == 9 and short[4] == "-" and long_code.startswith("thinair_pair_")
    assert normalise_short(short.lower()) == short.replace("-", "")
    assert normalise_short("o0il-1l0o") == "00111100"  # Crockford: O->0, I/L->1
    for bad in ("", "ABC", "ABCDEFGHI", "UUUU-UUUU", "ABCD EFG!", "ABCD-EFG"):
        assert normalise_short(bad) is None
    assert format_short("K7QM2XVD") == "K7QM-2XVD"


def test_a_short_code_signs_in_once_and_its_long_twin_dies_with_it() -> None:
    upgrade(get_engine())
    with session_scope() as s:
        long_code, short = create_pairing(s, None, timedelta(minutes=30))
        token = redeem(s, short.lower().replace("-", " "))  # sloppy typing is fine
        assert token and token.startswith("thinair_")
        assert redeem(s, short) is None and redeem(s, long_code) is None  # one pairing, one use
    assert "paired" in _events()


def test_short_codes_expire(client) -> None:
    with session_scope() as s:
        _, short = create_pairing(s, None, timedelta(minutes=1))
        row = s.exec(__import__("sqlmodel").select(PairingCode)).first()
        row.expires_at = utcnow() - timedelta(seconds=1)
        s.add(row)
        s.commit()
        assert redeem(s, short) is None


def test_twenty_wrong_short_codes_burn_the_short_code_but_not_the_qr(client) -> None:
    with session_scope() as s:
        long_code, short = create_pairing(s, None, timedelta(minutes=30))
        for i in range(SHORT_MAX_FAILURES):
            assert redeem(s, f"ZZZZ-{i:04d}".replace("U", "V")[:9]) is None
        assert redeem(s, short) is None  # burned
        assert redeem(s, long_code) is not None  # the long QR code still works


def test_wrong_short_codes_over_the_api_are_throttled_per_address(client) -> None:
    with session_scope() as s:
        _, short = create_pairing(s, None, timedelta(minutes=30))
    codes = [client.post("/api/pair", json={"code": "ZZZZ-ZZZZ"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]
    # while throttled even the right code is refused (the per-address limit, as for long codes)
    assert client.post("/api/pair", json={"code": short}).status_code == 429


def test_the_right_short_code_signs_a_browser_in(client) -> None:
    with session_scope() as s:
        _, short = create_pairing(s, None, timedelta(minutes=30))
    r = client.post("/api/pair", json={"code": short})
    assert r.status_code == 204 and "thinair_session" in r.headers.get("set-cookie", "")
    assert client.get("/api/setup").status_code == 200  # the cookie is a working session


def test_plain_pairing_codes_have_no_short_form() -> None:
    upgrade(get_engine())
    with session_scope() as s:
        code = create_pairing_code(s, None)
        row = s.exec(__import__("sqlmodel").select(PairingCode)).first()
        assert row.short_hash is None and code.startswith("thinair_pair_")


def test_qr_command_can_print_a_short_code() -> None:
    upgrade(get_engine())
    out = CliRunner().invoke(
        cli_app, ["qr", "--url", "https://203-0-113-7.sslip.io", "--ttl", "30", "--short"]
    )
    assert out.exit_code == 0, out.output
    import re

    m = re.search(r"enter the code from `thinair qr`:\s+([0-9A-Z]{4}-[0-9A-Z]{4})", out.output)
    assert m, out.output
    with session_scope() as s:
        assert redeem(s, m.group(1))
