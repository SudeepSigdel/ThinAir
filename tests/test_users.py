"""Multiple users: who sees which jobs, user management, and what a job may use on its owner's
behalf (agent login volume, API keys, the server's GitHub token). No Docker."""

import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from typer.testing import CliRunner

from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.auth import bootstrap_admin_token, create_token
from thinair.config import get_settings
from thinair.db.models import ApiToken, Job, JobKind, JobResult, ProfileReport, User
from thinair.db.session import session_scope
from thinair.jobs.service import ExecResult
from thinair.users import (
    UserError,
    auth_volume_name,
    create_user,
    disable_user,
    github_token_allowed,
    principal_for,
)

OWNER = "o" * 32
URL = "https://github.com/octo/demo"
PATCH = b"diff --git a/x b/x\n"


async def agent(ctx, _mgr) -> ExecResult:
    with session_scope() as s:
        s.add(JobResult(job_id=ctx.job.id, summary="ok", diff_text="+x", patch=PATCH))
        row = s.get(Job, ctx.job.id)
        row.commit_sha = "c" * 40
        s.add(row)
        s.commit()
    return ExecResult()


async def profile(ctx, _mgr) -> ExecResult:
    with session_scope() as s:
        s.add(ProfileReport(job_id=ctx.job.id, report={"runs": []}, report_md="# r"))
        s.commit()
    return ExecResult()


async def pr(ctx, _mgr) -> ExecResult:
    return ExecResult()


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", OWNER)
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", "github_pat_" + "U1" * 20)
    get_settings.cache_clear()
    api_auth._failures.clear()
    a = create_app({JobKind.AGENT: agent, JobKind.PROFILE: profile, JobKind.PR: pr})
    with TestClient(a) as owner:
        owner.headers["Authorization"] = f"Bearer {OWNER}"
        a.state.test_client = owner
        yield a, owner


class As:
    """Requests as another user through the one client whose event loop runs the job workers
    (a second TestClient would run on another loop and never wake them)."""

    def __init__(self, client: TestClient, token: str) -> None:
        self.c, self.h = client, {"Authorization": f"Bearer {token}"}

    def request(self, method: str, path: str, **kw):  # noqa: ANN201
        return self.c.request(method, path, headers=self.h, **kw)

    def get(self, path: str, **kw):  # noqa: ANN201
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw):  # noqa: ANN201
        return self.request("POST", path, **kw)


def as_user(app, token: str) -> As:
    return As(app.state.test_client, token)


def add_user(owner: TestClient, name: str, admin: bool = False) -> str:
    r = owner.post("/api/users", json={"name": name, "is_admin": admin})
    assert r.status_code == 201, r.text
    return r.json()["token"]


def finished(c, job_id: str) -> dict:
    for _ in range(250):
        job = c.get(f"/api/jobs/{job_id}").json()
        if job.get("finished"):
            return job
        time.sleep(0.02)
    raise AssertionError(job)


def agent_job(c, url: str = URL) -> str:
    body = {"repo_url": url, "kind": "agent", "agent_id": "claude", "task": "t"}
    r = c.post("/api/jobs", json=body)
    assert r.status_code == 201, r.text
    finished(c, r.json()["id"])
    return r.json()["id"]


# ---- visibility ----------------------------------------------------------------------------
def test_members_see_only_their_own_jobs_and_admins_see_all(app) -> None:
    a, owner = app
    alice = as_user(a, add_user(owner, "alice"))
    bob = as_user(a, add_user(owner, "bob"))
    carol = as_user(a, add_user(owner, "carol", admin=True))

    mine = agent_job(alice)
    theirs = agent_job(bob, "https://github.com/octo/secret")
    own = agent_job(owner, "https://github.com/octo/owner-only")

    assert [j["id"] for j in alice.get("/api/jobs").json()] == [mine]
    assert {j["id"] for j in owner.get("/api/jobs").json()} == {mine, theirs, own}
    assert {j["id"] for j in carol.get("/api/jobs").json()} == {mine, theirs, own}

    # Someone else's job is indistinguishable from a missing one, on every job route.
    for method, path in [
        ("GET", ""),
        ("GET", "/events"),
        ("GET", "/result"),
        ("GET", "/patch"),
        ("GET", "/profile"),
        ("GET", "/profile.md"),
        ("POST", "/cancel"),
        ("POST", "/retry"),
        ("POST", "/pr"),
    ]:
        r = alice.request(method, f"/api/jobs/{theirs}{path}")
        assert r.status_code == 404, (path, r.text)
        assert r.json()["detail"]["code"] == "job_not_found"
    assert alice.get(f"/api/jobs/{mine}/patch").content == PATCH
    assert carol.get(f"/api/jobs/{theirs}/patch").content == PATCH

    # Repositories: members only see the ones they have jobs for.
    repos = {r["url"]: r["id"] for r in owner.get("/api/repos").json()}
    assert len(repos) == 3
    assert [r["url"] for r in alice.get("/api/repos").json()] == [URL]
    secret = repos["https://github.com/octo/secret"]
    assert alice.get(f"/api/repos/{secret}").status_code == 404
    body = {"repo_id": secret, "kind": "profile"}
    assert alice.post("/api/jobs", json=body).status_code == 404
    assert bob.post("/api/jobs", json=body).status_code == 201


def test_jobs_keep_their_owner_on_retry_and_pr(app, monkeypatch) -> None:
    a, owner = app
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN_FOR_MEMBERS", "true")
    get_settings.cache_clear()
    alice = as_user(a, add_user(owner, "alice"))
    job = agent_job(alice)
    retried = owner.post(f"/api/jobs/{job}/retry").json()["id"]  # an admin retries it
    prj = owner.post(f"/api/jobs/{job}/pr")
    assert prj.status_code == 201, prj.text
    with session_scope() as s:
        uid = s.exec(select(User).where(User.name == "alice")).one().id
        assert s.get(Job, job).user_id == uid
        assert s.get(Job, retried).user_id == uid
        assert s.get(Job, prj.json()["id"]).user_id == uid
    assert {j["id"] for j in alice.get("/api/jobs").json()} >= {job, retried, prj.json()["id"]}


def test_members_cannot_use_the_server_github_token_by_default(app, monkeypatch) -> None:
    a, owner = app
    alice = as_user(a, add_user(owner, "alice"))
    carol = as_user(a, add_user(owner, "carol", admin=True))
    job = agent_job(alice)
    r = alice.post(f"/api/jobs/{job}/pr")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "github_not_allowed"
    assert owner.post(f"/api/jobs/{job}/pr").status_code == 403  # policy follows the owner
    assert carol.post(f"/api/jobs/{agent_job(carol)}/pr").status_code == 201  # admins may

    monkeypatch.setenv("THINAIR_GITHUB_TOKEN_FOR_MEMBERS", "true")
    get_settings.cache_clear()
    assert alice.post(f"/api/jobs/{job}/pr").status_code == 201


# ---- user management -----------------------------------------------------------------------
def test_user_management_is_admin_only_and_disable_locks_out(app) -> None:
    a, owner = app
    alice_token = add_user(owner, "alice")
    alice = as_user(a, alice_token)
    assert alice.get("/api/me").json() == {
        "token_name": "first",
        "user": "alice",
        "is_admin": False,
    }
    for method, path in [
        ("GET", "/api/users"),
        ("POST", "/api/users"),
        ("POST", "/api/users/alice/tokens"),
        ("POST", "/api/users/alice/disable"),
    ]:
        r = alice.request(method, path, json={"name": "mallory"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "admin_only", path

    assert owner.post("/api/users", json={"name": "alice"}).status_code == 409
    for bad in ["admin", "Alice", "a.b", "1x", "x" * 33]:
        assert owner.post("/api/users", json={"name": bad}).status_code == 422, bad
    names = [u["name"] for u in owner.get("/api/users").json()]
    assert names == ["admin", "alice"]

    second = owner.post("/api/users/alice/tokens", json={"name": "laptop"}).json()["token"]
    assert as_user(a, second).get("/api/me").status_code == 200

    r = owner.post("/api/users/alice/disable")
    assert r.status_code == 200 and r.json()["disabled_at"]
    api_auth._failures.clear()
    assert alice.get("/api/me").status_code == 401
    assert as_user(a, second).get("/api/me").status_code == 401
    anon = TestClient(a)
    assert anon.post("/api/login", json={"token": alice_token}).status_code == 401
    assert owner.post("/api/users/nobody/disable").status_code == 404
    assert owner.post("/api/users/alice/tokens", json={}).status_code == 404


def test_a_disabled_user_is_refused_even_with_an_unrevoked_token(engine) -> None:
    from sqlmodel import Session

    with Session(engine) as s:
        user = create_user(s, "dave")
        create_token(s, "x", "thinair_" + "d" * 40, user_id=user.id)
        row = s.exec(select(ApiToken)).one()
        assert principal_for(s, row).name == "dave"
        user.disabled_at = row.created_at  # e.g. set directly in the database
        s.add(user)
        s.commit()
        assert principal_for(s, row) is None


def test_bootstrap_ignores_member_tokens(engine) -> None:
    """A member's token must not count as 'an admin token exists' on first start."""
    from sqlmodel import Session

    with Session(engine) as s:
        user = create_user(s, "erin")
        create_token(s, "first", user_id=user.id)
        printed = bootstrap_admin_token(s, None)
        assert printed is not None
        owner_row = s.exec(select(ApiToken).where(ApiToken.user_id.is_(None))).one()  # type: ignore[union-attr]
        assert principal_for(s, owner_row).is_owner
        disable_user(s, "erin")
        assert s.exec(select(ApiToken).where(ApiToken.user_id == user.id)).one().revoked_at
        with pytest.raises(UserError):
            disable_user(s, "nobody")


def test_policy_helpers() -> None:
    member, admin = User(name="bob"), User(name="carol", is_admin=True)
    assert auth_volume_name("claude", None) == "thinair-auth-claude"
    assert auth_volume_name("claude", member) == "thinair-auth-claude.bob"
    assert auth_volume_name("claude-bob", None) != auth_volume_name("claude", member)
    assert github_token_allowed(None) and github_token_allowed(admin)
    assert not github_token_allowed(member)


# ---- executors act as the job's owner ---------------------------------------------------------
@pytest.mark.parametrize(("member", "for_members"), [(False, False), (True, False), (True, True)])
async def test_executors_use_the_owners_login_volume_keys_and_github_policy(
    monkeypatch, engine, member, for_members
) -> None:
    import thinair.jobs.executors as ex
    from thinair.db.migrate import upgrade
    from thinair.db.models import Repo
    from thinair.db.session import get_engine
    from thinair.jobs.events import EventStore
    from thinair.jobs.service import JobContext

    monkeypatch.setenv("THINAIR_GITHUB_TOKEN_FOR_MEMBERS", str(for_members).lower())
    get_settings.cache_clear()
    upgrade(get_engine())
    seen: dict = {}

    class Prepared:
        volume, commit_sha = "ws", "a" * 40

    async def fake_prepare(_mgr, _src, _job_id, _log, *, github_auth, cache_scope):
        seen["github_auth"], seen["cache_scope"] = github_auth, cache_scope
        return Prepared()

    class FakeAdapter:
        def __init__(self, cfg, mgr, job_id, prepared, *, host_env, auth_volume):
            seen["host_env"], seen["auth_volume"] = host_env, auth_volume

        async def prepare(self, _log):
            raise ex.AgentAuthError("stop here")

    monkeypatch.setattr(ex, "prepare_workspace", fake_prepare)
    monkeypatch.setattr(ex, "CLIAdapter", FakeAdapter)

    class Mgr:
        def remove_volume(self, _v):
            pass

    with session_scope() as s:
        uid = create_user(s, "bob").id if member else None
        repo = Repo(url=URL, name="demo")
        s.add(repo)
        s.commit()
        job = Job(repo_id=repo.id, kind=JobKind.AGENT, agent_id="claude", task="t", user_id=uid)
        s.add(job)
        s.commit()
    res = await ex.agent_executor(JobContext(job, repo, EventStore()), Mgr())
    assert res.error_code == "agent_not_authenticated"
    if member:
        assert seen["auth_volume"] == "thinair-auth-claude.bob"
        assert seen["host_env"] == {}  # the owner's API keys stay with the owner
        assert seen["github_auth"] is for_members
        assert seen["cache_scope"] == "bob"  # their own dependency cache, never the owner's
    else:
        assert seen == {
            "github_auth": True,
            "cache_scope": "admin",
            "host_env": None,
            "auth_volume": "thinair-auth-claude",
        }


def test_member_login_hint_names_their_user(monkeypatch) -> None:
    from thinair.agents.cli_adapter import CLIAdapter
    from thinair.agents.config import load_agents

    cfg = load_agents(None)[0]["claude"]

    class P:
        volume = "ws"

    a = CLIAdapter(cfg, None, "j", P(), host_env={}, auth_volume="thinair-auth-claude.bob")  # type: ignore[arg-type]
    assert "--user bob" in a._auth_hint()
    assert "--user" not in CLIAdapter(cfg, None, "j", P(), host_env={})._auth_hint()  # type: ignore[arg-type]


# ---- CLI -----------------------------------------------------------------------------------
def test_cli_user_commands_and_per_user_login(monkeypatch) -> None:
    import thinair.agents.cli_adapter as adapter_mod
    import thinair.cli.main as cli

    r = CliRunner().invoke(cli.app, ["user", "add", "bob"])
    assert r.exit_code == 0, r.output
    assert "thinair_" in r.output and "--user bob" in r.output
    assert CliRunner().invoke(cli.app, ["user", "add", "bob"]).exit_code == 1
    assert CliRunner().invoke(cli.app, ["user", "add", "admin"]).exit_code == 1
    r = CliRunner().invoke(cli.app, ["token", "create", "--name", "laptop", "--user", "bob"])
    assert r.exit_code == 0 and r.output.strip().startswith("thinair_")
    listing = CliRunner().invoke(cli.app, ["token", "list"]).output
    assert "laptop" in listing and "bob" in listing
    assert "bob" in CliRunner().invoke(cli.app, ["user", "list"]).output

    async def fake_image(_mgr, _cfg, _stack, _log):
        return "img:1"

    volumes = []

    class FakeMgr:
        def ensure_persistent_volume(self, name, labels):
            volumes.append(name)

    monkeypatch.setattr(adapter_mod, "ensure_agent_image", fake_image)
    monkeypatch.setattr(cli, "SandboxManager", FakeMgr)
    r = CliRunner().invoke(cli.app, ["auth", "claude", "--print-command", "--user", "bob"])
    assert r.exit_code == 0, r.output
    assert "-v thinair-auth-claude.bob:/thinair-auth" in r.output
    assert volumes == ["thinair-auth-claude.bob"]
    assert CliRunner().invoke(cli.app, ["auth", "claude", "--user", "nobody"]).exit_code == 1

    assert CliRunner().invoke(cli.app, ["user", "disable", "bob"]).exit_code == 0
    assert "disabled" in CliRunner().invoke(cli.app, ["user", "list"]).output
    r = CliRunner().invoke(cli.app, ["token", "create", "--name", "x", "--user", "bob"])
    assert r.exit_code == 1
