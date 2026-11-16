"""The GitHub App: manifest, conversion, JWT, short-lived tokens, the open callbacks and how the
clone and PR steps use them. A fake GitHub (a `requests`-style session) stands in for the network;
no Docker."""

import asyncio
import base64
import json
import os
import re
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from thinair import github_app as ga
from thinair.api import auth as api_auth
from thinair.api import github_api
from thinair.api.app import create_app
from thinair.config import get_settings
from thinair.db.migrate import upgrade
from thinair.db.models import GitHubInstallation, Job, JobKind, JobResult, JobStatus, Repo, User
from thinair.db.session import get_engine, session_scope
from thinair.github import (
    Credential,
    GitHubAccess,
    GitHubAPIError,
    GitHubClient,
    GitHubTokenError,
    commit_identity,
    git_auth_env,
)
from thinair.project import prepare
from thinair.sandbox.manager import SandboxResult
from thinair.secrets import secret_mode_problem

ADMIN = "t" * 32
PUBLIC = "https://203-0-113-7.sslip.io"
URL = "https://github.com/octo/demo"
PAT = "github_pat_" + "Ab1" * 20
LONG_APP_TOKEN = (
    "ghs_123456_" + ".".join(["eyJhbGciOiJSUzI1NiJ9" * 6] * 4) + "x" * 40
)  # ~540 chars, dots

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()


class Resp:
    def __init__(self, status: int, data=None) -> None:
        self.status_code, self._data = status, data
        self.content = b"x" if data is not None else b""

    def json(self):
        return self._data


class FakeGitHub:
    """Just enough of api.github.com. `covered` = repos the installation can see."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.covered = {"octo/demo"}
        self.installations = [
            {
                "id": 42,
                "account": {"login": "octocat", "type": "User"},
                "repository_selection": "selected",
            }
        ]
        self.minted: list[dict] = []
        self.revoked: list[str] = []
        self.repos = [
            {
                "id": 1,
                "full_name": "octo/demo",
                "html_url": URL,
                "private": True,
                "default_branch": "main",
                "pushed_at": "2026-11-01T10:00:00Z",
                "description": "demo",
                "language": "Python",
            },
            {
                "id": 2,
                "full_name": "octo/newer",
                "html_url": "https://github.com/octo/newer",
                "private": False,
                "default_branch": "main",
                "pushed_at": "2026-11-10T10:00:00Z",
                "description": None,
                "language": None,
            },
        ]
        self.fail_mint = False

    def request(self, method, url, headers=None, timeout=None, json=None, params=None, **kw):
        path = url.removeprefix("https://api.github.com")
        auth = (headers or {}).get("Authorization", "")
        self.calls.append((method, path, {"auth": auth, "json": json, "params": params}))
        if method == "POST" and re.fullmatch(r"/app-manifests/[\w-]+/conversions", path):
            return Resp(
                201,
                {
                    "id": 777,
                    "slug": "thinair-3fa9c1",
                    "client_id": "Iv1.abc",
                    "client_secret": "cs" * 20,
                    "pem": PEM,
                    "webhook_secret": "ws",
                    "html_url": "https://github.com/apps/thinair-3fa9c1",
                    "owner": {"login": "octocat", "id": 583231, "type": "User"},
                },
            )
        if method == "GET" and path == "/app/installations":
            return Resp(200, self.installations)
        m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/installation", path)
        if method == "GET" and m:
            if f"{m.group(1)}/{m.group(2)}" in self.covered:
                return Resp(200, {"id": 42})
            return Resp(404, {"message": "Not Found"})
        if method == "POST" and path == "/app/installations/42/access_tokens":
            if self.fail_mint:
                return Resp(422, {"message": "not accessible"})
            self.minted.append(json)
            return Resp(201, {"token": f"ghs_{len(self.minted):04d}_" + "x" * 30})
        if method == "DELETE" and path == "/installation/token":
            self.revoked.append(auth.removeprefix("Bearer "))
            return Resp(204)
        if method == "GET" and path == "/installation/repositories":
            return Resp(200, {"total_count": len(self.repos), "repositories": self.repos})
        if method == "DELETE" and path == "/app/installations/42":
            return Resp(204)
        return Resp(404, {"message": f"unexpected {method} {path}"})

    def outbound(self) -> int:
        return len(self.calls)


@pytest.fixture
def gh(monkeypatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(ga, "_session", lambda session=None: session or fake)
    return fake


@pytest.fixture(autouse=True)
def _fresh_throttles(monkeypatch):
    api_auth._failures.clear()
    github_api._setup_hits.clear()
    github_api._last_sync[0] = 0.0
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", "")
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("THINAIR_PUBLIC_URL", PUBLIC)
    get_settings.cache_clear()
    yield
    api_auth._failures.clear()


def connect_app() -> ga.AppCredentials:
    creds = ga.convert("code123", FakeGitHub())
    ga.save_credentials(creds)
    return creds


# ---- manifest -------------------------------------------------------------------------------
def test_manifest_shape_webhooks_off_least_privilege() -> None:
    m = ga.build_manifest(PUBLIC + "/", "thinair-3fa9c1")
    assert m["public"] is False and m["default_events"] == []
    assert m["hook_attributes"]["active"] is False
    assert m["redirect_url"] == f"{PUBLIC}/api/github/callback/app"
    assert m["setup_url"] == f"{PUBLIC}/api/github/callback/setup"
    assert m["default_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "metadata": "read",
    }
    assert "workflows" not in m["default_permissions"]  # a deliberate omission
    assert re.fullmatch(r"thinair-[0-9a-f]{6}", ga.new_app_name())


def test_org_names_are_checked_before_they_reach_a_url() -> None:
    assert ga.check_org("") is None and ga.check_org(None) is None
    assert ga.manifest_action("acme", "S").startswith(
        "https://github.com/organizations/acme/settings/apps/new?state=S"
    )
    for bad in ("a/b", "../x", "a b", "-x", "x" * 60, "a?b"):
        with pytest.raises(ValueError):
            ga.check_org(bad)


# ---- credentials and the secret file ----------------------------------------------------------
def test_conversion_stores_the_key_in_a_0600_file_and_never_prints_it(gh) -> None:
    creds = ga.convert("abc_DEF-123")
    ga.save_credentials(creds)
    assert "BEGIN" not in repr(creds) and creds.pem not in repr(creds)
    assert ga.load_credentials() == creds
    path = get_settings().data_dir / "secrets" / "github-app.json"
    assert json.loads(path.read_bytes())["pem"] == PEM
    if os.name == "posix":
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
        assert secret_mode_problem("github-app.json") is None
        path.chmod(0o644)
        assert "readable by other" in (secret_mode_problem("github-app.json") or "")


def test_conversion_refuses_odd_codes_without_calling_github(gh) -> None:
    for bad in ("", "a/b", "../x", "a b", "x" * 300, "a?b=1"):
        with pytest.raises(GitHubAPIError):
            ga.convert(bad)
    assert gh.outbound() == 0


def test_damaged_key_file_is_reported_not_treated_as_disconnected() -> None:
    from thinair.secrets import write_secret

    write_secret("github-app.json", b"{not json")
    with pytest.raises(GitHubAPIError, match="damaged"):
        ga.load_credentials()


# ---- JWT ----------------------------------------------------------------------------------
def test_jwt_claims_and_signature_verify_with_the_public_key(gh) -> None:
    creds = connect_app()
    now = 1_800_000_000
    token = ga.app_jwt(creds, now=now)
    head, body, sig = token.split(".")

    def dec(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    assert json.loads(dec(head)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(dec(body))
    assert claims == {"iat": now - 60, "exp": now + 540, "iss": "Iv1.abc"}
    assert claims["exp"] - now <= 600  # GitHub's limit is 10 minutes
    _KEY.public_key().verify(
        dec(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_jwt_with_a_bad_key_says_reconnect(gh) -> None:
    creds = connect_app()
    broken = ga.AppCredentials(**{**creds.__dict__, "pem": "not a key"})
    with pytest.raises(GitHubAPIError, match="Reconnect"):
        ga.app_jwt(broken)


# ---- minting ------------------------------------------------------------------------------
def test_mint_body_is_one_repo_and_the_permission_the_step_needs(gh) -> None:
    app = ga.GitHubApp(connect_app())
    for need, perms in (
        ("clone", {"contents": "read"}),
        ("push", {"contents": "write"}),
        ("pr", {"pull_requests": "write", "metadata": "read"}),
    ):
        app.mint_token(42, ga.PERMISSIONS[need], repo="demo")
        assert gh.minted[-1] == {"permissions": perms, "repositories": ["demo"]}
    # the mint call is signed as the app (a JWT), not with a token
    assert gh.calls[-1][2]["auth"].startswith("Bearer ") and gh.calls[-1][2]["auth"].count(".") == 2


def test_a_token_with_a_line_break_is_refused(gh, monkeypatch) -> None:
    app = ga.GitHubApp(connect_app())
    monkeypatch.setattr(
        gh, "request", lambda *a, **k: Resp(201, {"token": "ghs_" + "a" * 30 + "\nX-Evil: 1"})
    )
    with pytest.raises(GitHubAPIError, match="unexpected format"):
        app.mint_token(42, {"contents": "read"}, repo="demo")


def test_long_dotted_app_token_is_accepted_and_masked(gh, monkeypatch) -> None:
    assert len(LONG_APP_TOKEN) > 500
    app = ga.GitHubApp(connect_app())
    monkeypatch.setattr(gh, "request", lambda *a, **k: Resp(201, {"token": LONG_APP_TOKEN}))
    assert app.mint_token(42, {"contents": "read"}, repo="demo") == LONG_APP_TOKEN
    env, redact = git_auth_env(LONG_APP_TOKEN)
    assert LONG_APP_TOKEN in redact and "GIT_CONFIG_VALUE_0" in env


# ---- resolution order: app > PAT > anonymous -------------------------------------------------
def test_app_wins_over_the_pat_and_is_scoped_to_the_repo(gh, monkeypatch) -> None:
    connect_app()
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", PAT)
    get_settings.cache_clear()
    cred = asyncio.run(GitHubAccess().token_for(URL, "clone"))
    assert cred is not None and cred.source == "app" and cred.token.startswith("ghs_")
    assert gh.minted == [{"permissions": {"contents": "read"}, "repositories": ["demo"]}]
    assert PAT not in repr(cred) and cred.token not in repr(cred)


def test_pat_when_the_app_does_not_cover_the_repo(gh, monkeypatch) -> None:
    connect_app()
    gh.covered = set()
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", PAT)
    get_settings.cache_clear()
    access = GitHubAccess()
    cred = asyncio.run(access.token_for(URL, "clone"))
    assert cred is not None and cred.source == "token" and cred.token == PAT
    assert access.note == "not_installed" and gh.minted == []


def test_anonymous_when_nothing_is_configured(gh) -> None:
    assert asyncio.run(GitHubAccess().token_for(URL, "clone")) is None
    assert gh.outbound() == 0  # no app, so no GitHub call at all


def test_pat_only_install_behaves_as_before(gh, monkeypatch) -> None:
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", PAT)
    get_settings.cache_clear()
    cred = asyncio.run(GitHubAccess().token_for(URL, "push"))
    assert cred is not None and cred.source == "token" and gh.outbound() == 0


def test_a_malformed_pat_is_still_an_error(gh, monkeypatch) -> None:
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", "short")
    get_settings.cache_clear()
    with pytest.raises(GitHubTokenError):
        asyncio.run(GitHubAccess().token_for(URL, "clone"))


def test_github_outage_falls_back_instead_of_failing_public_clones(gh) -> None:
    connect_app()
    gh.fail_mint = True
    access = GitHubAccess()
    assert asyncio.run(access.token_for(URL, "clone")) is None
    assert access.note and "not accessible" in access.note


# ---- the clone step: revoke in finally, hints ------------------------------------------------
class FakeMgr:
    def __init__(self, exit_code: int) -> None:
        self.exit_code, self.specs = exit_code, []

    async def run(self, spec, on_line=None):
        self.specs.append(spec)
        if on_line:
            on_line("fatal: repository not found")
        return SandboxResult(exit_code=self.exit_code)


@pytest.fixture
def no_images(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(prepare, "ensure_image", _noop)


def test_clone_failure_still_revokes_the_key_and_hints_at_the_app(gh, no_images) -> None:
    connect_app()
    mgr = FakeMgr(exit_code=128)
    src = prepare.GitSource(URL)
    with pytest.raises(prepare.PrepareError) as ei:
        asyncio.run(prepare._load_source(mgr, src, "j1", "vol", lambda _l: None, True))
    assert ei.value.code == "clone_failed" and "GitHub App" in ei.value.message
    assert len(gh.minted) == 1 and len(gh.revoked) == 1  # revoked although the clone failed
    spec = mgr.specs[0]
    assert gh.revoked[0] in spec.redact  # the key was masked in the container's output
    assert spec.env["GIT_CONFIG_COUNT"] == "1"


def test_clone_success_revokes_too_and_logs_which_credential(gh, no_images) -> None:
    connect_app()
    logs: list[str] = []

    async def go():
        mgr = FakeMgr(exit_code=0)

        async def fake_read_only(*a, **k):
            return 0, ["a" * 40]

        prepare._read_only = fake_read_only
        return await prepare._load_source(
            mgr, prepare.GitSource(URL), "j1", "vol", logs.append, True
        )

    orig = prepare._read_only
    try:
        sha = asyncio.run(go())
    finally:
        prepare._read_only = orig
    assert sha == "a" * 40 and len(gh.revoked) == 1
    assert any("GitHub App" in ln for ln in logs)
    assert not any(gh.revoked[0] in ln for ln in logs)


def test_not_allowed_users_clone_anonymously_and_never_touch_github(gh, no_images) -> None:
    connect_app()
    mgr = FakeMgr(exit_code=128)
    with pytest.raises(prepare.PrepareError) as ei:
        asyncio.run(prepare._load_source(mgr, prepare.GitSource(URL), "j1", "vol", print, False))
    assert "only admins" in ei.value.message
    assert gh.outbound() == 0 and "GIT_CONFIG_COUNT" not in mgr.specs[0].env


def test_anonymous_hint_points_at_connect_github(gh, no_images) -> None:
    mgr = FakeMgr(exit_code=128)
    with pytest.raises(prepare.PrepareError) as ei:
        asyncio.run(prepare._load_source(mgr, prepare.GitSource(URL), "j1", "vol", print, True))
    assert "Connect GitHub" in ei.value.message


# ---- the PR step: separate keys, none in the wrong container ---------------------------------
def _seed_pr_job() -> tuple[Job, Job]:
    upgrade(get_engine())
    with session_scope() as s:
        repo = Repo(url=URL, name="demo")
        s.add(repo)
        s.commit()
        parent = Job(repo_id=repo.id, kind=JobKind.AGENT, status=JobStatus.SUCCEEDED, task="fix")
        parent.commit_sha = "a" * 40
        s.add(parent)
        s.add(JobResult(job_id=parent.id, summary="done", patch=b"diff --git a/x b/x\n"))
        s.commit()
        pr_job = Job(repo_id=repo.id, kind=JobKind.PR, parent_job_id=parent.id)
        s.add(pr_job)
        s.commit()
        s.expunge_all()
        return parent, pr_job


def test_pr_executor_uses_a_push_key_and_a_separate_pr_key(gh, monkeypatch) -> None:
    from thinair.jobs import pr as prmod
    from thinair.jobs.events import EventStore
    from thinair.jobs.service import JobContext

    connect_app()
    parent, pr_job = _seed_pr_job()
    pushed: dict = {}
    pr_calls: list = []

    async def fake_noop(*a, **k):
        return None

    async def fake_build(mgr, job_id, volume, **kw):
        pushed["build_token"] = kw["token"]

    async def fake_push(mgr, job_id, volume, branch, cred, log):
        pushed["push_token"], pushed["source"] = cred.token, cred.source

    class FakeClient:
        def __init__(self, token, source="token", **kw):
            pr_calls.append(token)

        def default_branch(self, o, r):
            return "main"

        def create_pull(self, o, r, **kw):
            return f"https://github.com/{o}/{r}/pull/1"

    class Mgr:
        def create_volume(self, *a, **k):
            pass

        def remove_volume(self, *a, **k):
            pass

    monkeypatch.setattr(prmod, "ensure_image", fake_noop)
    monkeypatch.setattr(prmod, "build_commit", fake_build)
    monkeypatch.setattr(prmod, "push_branch", fake_push)
    monkeypatch.setattr(prmod, "GitHubClient", FakeClient)
    ctx = JobContext(job=pr_job, repo=None, events=EventStore())  # type: ignore[arg-type]
    res = asyncio.run(prmod.pr_executor(ctx, Mgr()))  # type: ignore[arg-type]
    assert res.status == JobStatus.SUCCEEDED, res.error_message

    assert [m["permissions"] for m in gh.minted] == [
        {"contents": "write"},
        {"pull_requests": "write", "metadata": "read"},
    ]
    push_tok, pr_tok = f"ghs_0001_{'x' * 30}", f"ghs_0002_{'x' * 30}"
    assert pushed["push_token"] == push_tok and pushed["build_token"] == push_tok
    assert pr_calls == [pr_tok]
    assert pr_tok != push_tok
    assert sorted(gh.revoked) == sorted([push_tok, pr_tok])  # both revoked when done


def test_the_pr_key_never_appears_in_any_container_spec(gh) -> None:
    """Drive the real `_git` helper: whatever it is given ends up in a SandboxSpec, so a key
    that is never passed to it cannot be in one."""
    from thinair.jobs import pr as prmod

    mgr = FakeMgr(exit_code=0)
    asyncio.run(
        prmod._git(mgr, "j", "vol", ["push"], lambda _l: None, token="ghs_push_" + "p" * 20)
    )
    spec = mgr.specs[0]
    blob = json.dumps(spec.model_dump(mode="json"))
    assert "ghs_push_" in blob.replace("ghs_push_" + "p" * 20, "ghs_push_")  # push key is present
    assert "ghs_pr_" not in blob and spec.network is True


def test_a_workflow_file_push_is_explained_for_the_app(gh) -> None:
    from thinair.jobs import pr as prmod

    class Mgr:
        async def run(self, spec, on_line=None):
            on_line("remote: refusing to allow a GitHub App to create or update workflow")
            return SandboxResult(exit_code=1)

    with pytest.raises(prmod.PRError, match="Workflows permission"):
        asyncio.run(
            prmod.push_branch(
                Mgr(), "j", "vol", "thinair/x", Credential("ghs_" + "a" * 30, "app"), print
            )
        )


def test_pr_errors_name_the_source() -> None:
    from thinair.github import _explain

    r = Resp(404, {"message": "Not Found"})
    assert "GitHub App" in _explain(r, "app") and "THINAIR_GITHUB_TOKEN" not in _explain(r, "app")
    assert "token needs" in _explain(r, "token")
    assert "THINAIR_GITHUB_TOKEN" in _explain(Resp(401, {}), "token")
    c = GitHubClient("t" * 30, session=object(), source="app")
    assert c._source == "app"


def test_commit_identity_defaults_to_the_account_noreply_address(gh, monkeypatch) -> None:
    assert commit_identity()[1] == "thinair@localhost"
    connect_app()
    assert commit_identity() == ("Thinair", "583231+octocat@users.noreply.github.com")
    monkeypatch.setenv("THINAIR_GIT_AUTHOR_EMAIL", "me@example.com")
    get_settings.cache_clear()
    assert commit_identity()[1] == "me@example.com"  # an explicit setting always wins


def test_early_check_needs_some_credential(gh, monkeypatch) -> None:
    from thinair.jobs.pr import PRError, require_credentials

    with pytest.raises(PRError) as ei:
        require_credentials()
    assert ei.value.code == "pr_needs_token" and "Connect GitHub" in ei.value.message
    connect_app()
    require_credentials()  # the app counts; coverage is checked when a key is minted


# ---- members -------------------------------------------------------------------------------
def test_members_need_the_setting_to_use_the_servers_github(monkeypatch) -> None:
    from thinair.users import github_token_allowed

    member, admin = User(name="sam"), User(name="amy", is_admin=True)
    assert github_token_allowed(None) and github_token_allowed(admin)
    assert not github_token_allowed(member)
    monkeypatch.setenv("THINAIR_GITHUB_FOR_MEMBERS", "true")  # the clearer alias
    get_settings.cache_clear()
    assert github_token_allowed(member)
    monkeypatch.delenv("THINAIR_GITHUB_FOR_MEMBERS")
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN_FOR_MEMBERS", "true")  # the old name still works
    get_settings.cache_clear()
    assert github_token_allowed(member)


# ---- the API ------------------------------------------------------------------------------
H = {"Authorization": f"Bearer {ADMIN}"}


def test_full_connect_flow_through_the_api(gh) -> None:
    with TestClient(create_app(), follow_redirects=False) as c:
        st = c.get("/api/github", headers=H).json()
        assert st["mode"] == "none" and st["can_manage"] and st["public_url_set"]

        start = c.post("/api/github/app", json={}, headers=H).json()
        assert start["action"].startswith("https://github.com/settings/apps/new?state=")
        manifest = json.loads(start["manifest"])
        assert manifest["redirect_url"] == f"{PUBLIC}/api/github/callback/app"
        state = start["action"].split("state=")[1]

        # GitHub sends the browser back (no Thinair session needed on this route)
        r = c.get(f"/api/github/callback/app?code=abc123&state={state}")
        assert r.status_code == 303
        loc = r.headers["location"]
        assert loc.startswith("https://github.com/apps/thinair-3fa9c1/installations/new?state=")
        assert any(p.endswith("/conversions") for _m, p, _k in gh.calls), (
            "the code was exchanged after the state check"
        )
        assert ga.load_credentials() is not None

        # the state is single use
        again = c.get(f"/api/github/callback/app?code=abc123&state={state}")
        assert again.headers["location"].endswith("result=bad_state")

        # back from the installation page, carrying a spoofed installation id
        inst_state = loc.split("state=")[1]
        r = c.get(f"/api/github/callback/setup?installation_id=999&state={inst_state}")
        assert r.status_code == 303 and r.headers["location"].endswith("result=connected")
        st = c.get("/api/github", headers=H).json()
        assert st["mode"] == "app" and st["app"]["slug"] == "thinair-3fa9c1"
        assert [i["id"] for i in st["installations"]] == [42]  # 999 was never trusted
        assert (
            st["installations"][0]["manage_url"] == "https://github.com/settings/installations/42"
        )
        assert "pem" not in json.dumps(st) and "client_secret" not in json.dumps(st)

        # the picker
        repos = c.get("/api/github/repos", headers=H).json()
        assert [r["full_name"] for r in repos["repos"]] == [
            "octo/newer",
            "octo/demo",
        ]  # newest first
        assert repos["repos"][1]["private"] is True and repos["repos"][0]["added"] is False
        assert gh.minted[-1]["permissions"] == {"metadata": "read"}
        assert "repositories" not in gh.minted[-1]
        assert len(gh.revoked) == 1  # the listing key was revoked right after

        # adding a project from the picker records the lock, and flags it as added
        c.post("/api/repos", json={"url": URL, "private": True}, headers=H)
        assert c.get("/api/repos", headers=H).json()[0]["private"] is True
        listed = c.get("/api/github/repos", headers=H).json()["repos"]
        assert [r["added"] for r in listed] == [False, True]

        # a second "Connect" is refused until disconnected
        assert c.post("/api/github/app", json={}, headers=H).status_code == 409

        out = c.delete("/api/github/app", headers=H).json()
        assert out["delete_url"] == "https://github.com/settings/apps/thinair-3fa9c1/advanced"
        assert ga.load_credentials() is None
        assert c.get("/api/github", headers=H).json()["mode"] == "none"


def test_start_needs_a_public_url_and_a_valid_org(monkeypatch) -> None:
    with TestClient(create_app()) as c:
        r = c.post("/api/github/app", json={"org": "a/b"}, headers=H)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_org"
    monkeypatch.setenv("THINAIR_PUBLIC_URL", "")
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        r = c.post("/api/github/app", json={}, headers=H)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "no_public_url"


def test_authenticated_routes_refuse_anonymous_callers(gh) -> None:
    with TestClient(create_app()) as c:
        for method, path in (
            ("get", "/api/github"),
            ("post", "/api/github/app"),
            ("post", "/api/github/sync"),
            ("get", "/api/github/repos"),
            ("delete", "/api/github/app"),
        ):
            assert getattr(c, method)(path).status_code == 401, path
    assert gh.outbound() == 0


def test_members_cannot_manage_or_list(gh) -> None:
    from thinair.auth import create_token

    with TestClient(create_app()) as c:
        with session_scope() as s:
            u = User(name="sam")
            s.add(u)
            s.commit()
            tok = create_token(s, "phone", user_id=u.id)
        M = {"Authorization": f"Bearer {tok}"}
        assert c.post("/api/github/app", json={}, headers=M).status_code == 403
        assert c.post("/api/github/sync", headers=M).status_code == 403
        assert c.delete("/api/github/app", headers=M).status_code == 403
        r = c.get("/api/github/repos", headers=M)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "github_not_allowed"
        st = c.get("/api/github", headers=M).json()
        assert st["can_manage"] is False and st["allowed"] is False


# ---- the open callbacks: abuse -------------------------------------------------------------
def test_callbacks_without_a_live_state_make_no_outbound_call(gh) -> None:
    with TestClient(create_app(), follow_redirects=False) as c:
        for url in (
            "/api/github/callback/app?code=abc",
            "/api/github/callback/app?code=abc&state=made-up",
            "/api/github/callback/app?state=" + "x" * 200,
        ):
            r = c.get(url)
            assert r.status_code == 303 and r.headers["location"].endswith("result=bad_state")
        assert gh.outbound() == 0 and ga.load_credentials() is None


def test_state_for_another_purpose_or_expired_is_refused(gh) -> None:
    from thinair.db.models import OneTimeState, utcnow
    from thinair.onetime import consume_state, issue_state

    with TestClient(create_app(), follow_redirects=False) as c:
        with session_scope() as s:
            install = issue_state(s, "github_install")
            old = issue_state(s, "github_app")
            import hashlib

            row = s.get(OneTimeState, hashlib.sha256(old.encode()).hexdigest())
            row.expires_at = utcnow().replace(year=2020)
            s.add(row)
            s.commit()
        r = c.get(f"/api/github/callback/app?code=abc&state={install}")  # wrong purpose
        assert r.headers["location"].endswith("result=bad_state")
        r = c.get(f"/api/github/callback/app?code=abc&state={old}")  # expired
        assert r.headers["location"].endswith("result=bad_state")
        assert gh.outbound() == 0
        with session_scope() as s:
            assert consume_state(s, install, "github_install") is True
            assert consume_state(s, install, "github_install") is False


def test_bad_states_are_throttled_per_address(gh) -> None:
    with TestClient(create_app(), follow_redirects=False) as c:
        codes = [c.get("/api/github/callback/app?code=a&state=nope").status_code for _ in range(12)]
        assert codes[:10] == [303] * 10 and codes[10:] == [429, 429]
        # throttled: even the setup route now refuses
        assert c.get("/api/github/callback/setup").status_code == 429
    assert gh.outbound() == 0


def test_setup_callback_does_nothing_without_an_app(gh) -> None:
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.get("/api/github/callback/setup?installation_id=5")
        assert r.headers["location"].endswith("result=no_app")
    assert gh.outbound() == 0


def test_setup_callback_is_rate_limited_and_cools_down_between_github_calls(gh) -> None:
    connect_app()
    gh.calls.clear()
    with TestClient(create_app(), follow_redirects=False) as c:
        first = c.get("/api/github/callback/setup")
        assert first.headers["location"].endswith("result=connected")
        n = gh.outbound()
        assert n == 1  # one GET /app/installations
        for _ in range(5):  # a burst of anonymous hits within the cooldown costs GitHub nothing
            r = c.get("/api/github/callback/setup")
            assert r.status_code == 303
        assert gh.outbound() == n
        for _ in range(30):
            r = c.get("/api/github/callback/setup")
        assert r.status_code == 429


def test_conversion_failure_stores_nothing(gh, monkeypatch) -> None:
    monkeypatch.setattr(
        gh,
        "request",
        lambda *a, **k: Resp(404, {"message": "Not Found"}),
    )
    with TestClient(create_app(), follow_redirects=False) as c:
        state = c.post("/api/github/app", json={}, headers=H).json()["action"].split("state=")[1]
        r = c.get(f"/api/github/callback/app?code=abc&state={state}")
        assert r.headers["location"].endswith("result=failed")
        assert ga.load_credentials() is None


def test_installations_the_app_lost_are_marked_removed(gh) -> None:
    creds = connect_app()
    upgrade(get_engine())
    app = ga.GitHubApp(creds)
    with session_scope() as s:
        assert [i.installation_id for i in ga.sync_installations(s, app)] == [42]
        gh.installations = []
        assert ga.sync_installations(s, app) == []
        row = s.get(GitHubInstallation, 42)
        assert row is not None and row.removed_at is not None
        gh.installations = [{"id": 42, "account": {"login": "octocat", "type": "User"}}]
        assert len(ga.sync_installations(s, app)) == 1  # reinstalled: active again


def test_the_server_data_volume_can_never_be_mounted(monkeypatch) -> None:
    from pydantic import ValidationError

    from thinair.sandbox.spec import VolumeMount

    for name in ("thinair-data", "thinair_thinair-data", "myproj_thinair-data"):
        with pytest.raises(ValidationError):
            VolumeMount(volume=name, path="/x")
    monkeypatch.setenv("THINAIR_DATA_VOLUME", "custom-data")
    with pytest.raises(ValidationError):
        VolumeMount(volume="custom-data", path="/x")
    VolumeMount(volume="thinair-ws-abc", path="/workspace")  # normal volumes are fine
    VolumeMount(volume="thinair-auth-claude.sam", path="/x")


def test_nothing_in_the_log_contains_a_key(gh, caplog) -> None:
    connect_app()
    caplog.set_level("DEBUG")
    gh.fail_mint = True
    asyncio.run(GitHubAccess().token_for(URL, "clone"))
    text = caplog.text
    assert "BEGIN" not in text and PEM[40:80] not in text
    _ = time  # (kept for clock-based tests above)
