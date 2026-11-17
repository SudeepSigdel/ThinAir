"""Members connect their own GitHub installation of the server's app (phase 7). A fake GitHub
stands in for the network. Covers: nobody claims what their GitHub account doesn't have, the open
callback acts only on a live state, ownership is never taken over, and which installation each
person's jobs may use."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from test_github_app import PAT, PEM, FakeGitHub, Resp

from thinair import github_app as ga
from thinair.api import auth as api_auth
from thinair.api import github_api
from thinair.api.app import create_app
from thinair.auth import create_token
from thinair.config import get_settings
from thinair.db.models import GitHubInstallation, User
from thinair.db.session import session_scope
from thinair.github import GitHubAccess
from thinair.onetime import issue_state
from thinair.users import github_allowed

ADMIN = "t" * 32
H = {"Authorization": f"Bearer {ADMIN}"}
PUBLIC = "https://203-0-113-7.sslip.io"


class FakeMembers(FakeGitHub):
    """The server's installation (42, octo/demo) and one on sam's own account (77, sam/proj)."""

    def __init__(self) -> None:
        super().__init__()
        self.repo_install = {"octo/demo": 42, "sam/proj": 77}
        self.installations = [
            {
                "id": 42,
                "account": {"login": "octocat", "type": "User"},
                "repository_selection": "selected",
            },
            {
                "id": 77,
                "account": {"login": "sam-gh", "type": "User"},
                "repository_selection": "all",
            },
        ]
        # authorization code -> (GitHub login, installation ids that account may use)
        self.codes: dict[str, tuple[str, list[int]]] = {"sam-code": ("sam-gh", [77])}
        self.exchanged: list[dict] = []

    def request(
        self, method, url, headers=None, timeout=None, json=None, params=None, data=None, **kw
    ):
        if url == "https://github.com/login/oauth/access_token":
            self.calls.append((method, url, {"auth": "", "json": None, "params": None}))
            self.exchanged.append(data)
            code = (data or {}).get("code")
            if code not in self.codes:
                return Resp(200, {"error": "bad_verification_code"})
            return Resp(200, {"access_token": f"ghu_{code}_" + "u" * 30, "expires_in": 28800})
        path = url.removeprefix("https://api.github.com")
        auth = (headers or {}).get("Authorization", "")
        if path in ("/user", "/user/installations"):
            self.calls.append((method, path, {"auth": auth, "json": None, "params": params}))
            token = auth.removeprefix("Bearer ")
            who = next((v for c, v in self.codes.items() if f"ghu_{c}_" in token), None)
            if who is None:
                return Resp(401, {"message": "Bad credentials"})
            if path == "/user":
                return Resp(200, {"login": who[0], "id": 1})
            return Resp(
                200,
                {
                    "total_count": len(who[1]),
                    "installations": [i for i in self.installations if i["id"] in who[1]]
                    or [{"id": n, "account": {"login": who[0]}} for n in who[1]],
                },
            )
        import re

        m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/installation", path)
        if method == "GET" and m:
            self.calls.append((method, path, {"auth": auth, "json": None, "params": None}))
            iid = self.repo_install.get(f"{m.group(1)}/{m.group(2)}")
            return Resp(200, {"id": iid}) if iid else Resp(404, {"message": "Not Found"})
        m = re.fullmatch(r"/app/installations/(\d+)/access_tokens", path)
        if method == "POST" and m:
            self.calls.append((method, path, {"auth": auth, "json": json, "params": None}))
            self.minted.append({**(json or {}), "installation": int(m.group(1))})
            return Resp(201, {"token": f"ghs_{m.group(1)}_{len(self.minted):04d}_" + "x" * 24})
        return super().request(
            method, url, headers=headers, timeout=timeout, json=json, params=params, **kw
        )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("THINAIR_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", "")
    get_settings.cache_clear()
    api_auth._failures.clear()
    github_api._setup_hits.clear()
    github_api._last_sync[0] = 0.0


@pytest.fixture
def gh(monkeypatch) -> FakeMembers:
    fake = FakeMembers()
    monkeypatch.setattr(ga, "_session", lambda session=None: session or fake)
    return fake


@pytest.fixture
def client(gh):
    with TestClient(create_app({}), follow_redirects=False) as c:
        yield c


def connected_app() -> ga.AppCredentials:
    creds = ga.convert("code123", FakeGitHub())
    ga.save_credentials(creds)
    return creds


def person(name: str, admin: bool = False) -> tuple[str, dict]:
    with session_scope() as s:
        u = User(name=name, is_admin=admin)
        s.add(u)
        s.commit()
        tok = create_token(s, "phone", user_id=u.id)
        return u.id, {"Authorization": f"Bearer {tok}"}


def seed_server_installation() -> None:
    """What the admin's own connection recorded: installation 42 is the server's."""
    with session_scope() as s:
        s.add(
            GitHubInstallation(
                installation_id=42,
                account_login="octocat",
                account_type="User",
                repository_selection="selected",
            )
        )
        s.commit()


def start(client, headers) -> str:
    r = client.post("/api/github/mine/start", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["action"].split("state=")[1].split("&")[0]


# ---- the manifest and the stored app ----------------------------------------------------------------
def test_new_apps_carry_the_callback_url_and_old_ones_say_reconnect(client) -> None:
    m = ga.build_manifest(PUBLIC, "thinair-aaaaaa")
    assert m["callback_urls"] == [f"{PUBLIC}/api/github/callback/user"]
    assert connected_app().callback_ready is True
    legacy = json.loads(ga.AppCredentials.to_json(connected_app()))
    del legacy["callback_ready"]  # an app made before phase 7
    assert ga.AppCredentials.from_json(json.dumps(legacy).encode()).callback_ready is False


def test_a_legacy_app_cannot_start_the_member_flow(client) -> None:
    c = connected_app()
    legacy = json.loads(c.to_json())
    legacy["callback_ready"] = False
    from thinair.secrets import write_secret

    write_secret("github-app.json", json.dumps(legacy).encode())
    _, sam = person("sam")
    r = client.post("/api/github/mine/start", headers=sam)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "members_not_ready"
    assert client.get("/api/github", headers=sam).json()["mine"]["can_connect"] is False


def test_start_needs_a_member_a_login_and_an_app(client) -> None:
    assert client.post("/api/github/mine/start").status_code == 401
    _, sam = person("sam")
    r = client.post("/api/github/mine/start", headers=sam)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_app"
    connected_app()
    r = client.post("/api/github/mine/start", headers=H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "use_server_connection"


def test_start_sends_the_browser_to_github_with_our_state_and_never_the_secret(client) -> None:
    connected_app()
    _, sam = person("sam")
    url = client.post("/api/github/mine/start", headers=sam).json()["action"]
    assert url.startswith("https://github.com/login/oauth/authorize?client_id=Iv1.abc&state=")
    assert "redirect_uri=https%3A%2F%2F203-0-113-7.sslip.io%2Fapi%2Fgithub%2Fcallback%2Fuser" in url
    assert "cs" * 20 not in url and "client_secret" not in url


# ---- the open callback: abuse ---------------------------------------------------------------------------
def test_callback_without_a_live_state_makes_no_outbound_call(client, gh) -> None:
    connected_app()
    for q in ("code=sam-code", "code=sam-code&state=forged", "state=" + "x" * 200, ""):
        r = client.get(f"/api/github/callback/user?{q}")
        assert r.status_code == 303 and r.headers["location"].endswith("result=bad_state")
    assert gh.outbound() == 0 and gh.exchanged == []


def test_a_state_for_another_purpose_is_refused_and_states_work_once(client, gh) -> None:
    connected_app()
    _, sam = person("sam")
    with session_scope() as s:
        wrong = issue_state(s, "github_app", None)  # the admin's flow, not the member's
    r = client.get(f"/api/github/callback/user?code=sam-code&state={wrong}")
    assert r.headers["location"].endswith("result=bad_state") and gh.outbound() == 0
    state = start(client, sam)
    first = client.get(f"/api/github/callback/user?code=sam-code&state={state}")
    assert first.headers["location"].endswith("result=mine_connected")
    again = client.get(f"/api/github/callback/user?code=sam-code&state={state}")
    assert again.headers["location"].endswith("result=bad_state")  # single use


def test_bad_states_are_throttled_per_address(client, gh) -> None:
    connected_app()
    codes = [
        client.get("/api/github/callback/user?code=a&state=nope").status_code for _ in range(12)
    ]
    assert codes[:10] == [303] * 10 and codes[10:] == [429, 429]
    assert gh.outbound() == 0


def test_a_denied_authorization_changes_nothing(client, gh) -> None:
    connected_app()
    _, sam = person("sam")
    state = start(client, sam)
    r = client.get(f"/api/github/callback/user?error=access_denied&state={state}")
    assert r.headers["location"].endswith("result=denied") and gh.outbound() == 0


# ---- claiming: only what GitHub says is theirs ----------------------------------------------------------
def test_a_member_claims_their_own_installation_and_nothing_else(client, gh) -> None:
    connected_app()
    seed_server_installation()
    uid, sam = person("sam")
    state = start(client, sam)
    r = client.get(f"/api/github/callback/user?code=sam-code&state={state}")
    assert r.headers["location"] == "/#/setup/github?result=mine_connected"
    with session_scope() as s:
        row = s.get(GitHubInstallation, 77)
        assert row is not None and row.user_id == uid and row.account_login == "sam-gh"
        assert s.get(GitHubInstallation, 42).user_id is None  # the server's stays the server's
        assert [i.installation_id for i in ga.active_installations(s)] == [
            42
        ]  # not in the server's list
    mine = client.get("/api/github", headers=sam).json()["mine"]
    assert [i["id"] for i in mine["installations"]] == [77] and mine["installations"][0][
        "account"
    ] == "sam-gh"
    # sam's token exchange used the stored client secret, as a form body, over TLS
    assert gh.exchanged[0]["client_secret"] == "cs" * 20 and gh.exchanged[0][
        "redirect_uri"
    ].startswith(PUBLIC)


def test_a_member_cannot_claim_an_installation_their_github_account_does_not_have(
    client, gh
) -> None:
    """The user's token lists installation 42 or nothing; they ask for 77 (someone else's)."""
    connected_app()
    gh.codes["mallory"] = ("mallory-gh", [])  # an account with no installation of this app
    uid, mal = person("mallory")
    state = start(client, mal)
    r = client.get(f"/api/github/callback/user?code=mallory&state={state}")
    assert r.status_code == 303 and r.headers["location"].startswith(
        "https://github.com/apps/thinair-3fa9c1/"
    )
    with session_scope() as s:
        assert s.get(GitHubInstallation, 77) is None  # nothing claimed: they have to install first


def test_ids_the_user_is_not_verified_for_or_the_app_does_not_list_claim_nothing(
    client, gh
) -> None:
    connected_app()
    gh.codes["x"] = ("x-gh", [999])  # an installation the app doesn't have
    _, x = person("x")
    state = start(client, x)
    r = client.get(f"/api/github/callback/user?code=x&state={state}")
    assert r.headers["location"].endswith("result=mine_taken")
    with session_scope() as s:
        assert s.get(GitHubInstallation, 999) is None


def test_the_servers_and_other_members_installations_are_never_taken_over(client, gh) -> None:
    connected_app()
    seed_server_installation()
    gh.codes["greedy"] = (
        "greedy-gh",
        [42, 77],
    )  # their account can see both (e.g. an org they share)
    uid_sam, sam = person("sam")
    uid_g, greedy = person("greedy")
    client.get(
        f"/api/github/callback/user?code=sam-code&state={start(client, sam)}"
    )  # sam claims 77
    r = client.get(f"/api/github/callback/user?code=greedy&state={start(client, greedy)}")
    assert r.headers["location"].endswith("result=mine_taken")  # 42 is the server's, 77 is sam's
    with session_scope() as s:
        assert s.get(GitHubInstallation, 42).user_id is None
        assert s.get(GitHubInstallation, 77).user_id == uid_sam


def test_the_state_binds_the_claim_to_the_person_who_started_it(client, gh) -> None:
    connected_app()
    uid_a, a = person("amy")
    _, b = person("bob")
    gh.codes["amy-code"] = ("amy-gh", [77])
    state_for_amy = start(client, a)
    # bob's browser can't redirect the claim to himself: the state names amy
    client.get(f"/api/github/callback/user?code=amy-code&state={state_for_amy}")
    with session_scope() as s:
        assert s.get(GitHubInstallation, 77).user_id == uid_a
    assert client.get("/api/github", headers=b).json()["mine"]["installations"] == []


def test_a_failed_token_exchange_stores_nothing(client, gh) -> None:
    connected_app()
    _, sam = person("sam")
    state = start(client, sam)
    r = client.get(f"/api/github/callback/user?code=unknown&state={state}")
    assert r.headers["location"].endswith("result=failed")
    with session_scope() as s:
        assert s.get(GitHubInstallation, 77) is None


def test_the_user_token_is_never_stored_or_logged(client, gh, caplog) -> None:
    caplog.set_level("DEBUG")
    connected_app()
    _, sam = person("sam")
    client.get(f"/api/github/callback/user?code=sam-code&state={start(client, sam)}")
    assert "ghu_" not in caplog.text and "cs" * 20 not in caplog.text
    blob = b"".join(p.read_bytes() for p in (get_settings().data_dir).rglob("*") if p.is_file())
    assert b"ghu_" not in blob  # not in the database, not in the secrets folder


def test_a_member_can_forget_their_installation_and_owners_cannot_use_the_route(client, gh) -> None:
    connected_app()
    uid, sam = person("sam")
    client.get(f"/api/github/callback/user?code=sam-code&state={start(client, sam)}")
    assert client.delete("/api/github/mine", headers=H).status_code == 409
    assert client.delete("/api/github/mine", headers=sam).status_code == 204
    assert client.get("/api/github", headers=sam).json()["mine"]["installations"] == []
    assert client.delete("/api/github/mine").status_code == 401


# ---- which installation each person's jobs may use ---------------------------------------------------------
def covered_by(access: GitHubAccess, repo: str):
    cred = asyncio.run(access.token_for(f"https://github.com/{repo}", "clone"))
    return cred, access.note


@pytest.fixture
def world(gh, monkeypatch):
    """The server's app with installation 42 (octo/demo, the server's) and 77 (sam/proj, sam's)."""
    from thinair.db.migrate import upgrade
    from thinair.db.session import get_engine

    upgrade(get_engine())
    connected_app()
    seed_server_installation()
    sam_id, _ = person("sam")
    alex_id, _ = person("alex")
    with session_scope() as s:
        s.add(
            GitHubInstallation(
                installation_id=77,
                account_login="sam-gh",
                account_type="User",
                repository_selection="all",
                user_id=sam_id,
            )
        )
        s.commit()
    return {"sam": sam_id, "alex": alex_id}


def test_resolution_matrix(world, monkeypatch) -> None:
    sam, alex = world["sam"], world["alex"]
    # the owner and admins (allowed to use the server's connection): the server's installation only
    for uid in (None, alex):
        cred, _ = covered_by(GitHubAccess(user_id=uid, use_server=True), "octo/demo")
        assert cred and cred.source == "app" and "ghs_42_" in cred.token
        cred, note = covered_by(GitHubAccess(user_id=uid, use_server=True), "sam/proj")
        assert cred is None and note == "not_yours"  # sam's installation is not the server's
    # a member without the setting and without an installation of their own: nothing
    cred, note = covered_by(GitHubAccess(user_id=alex, use_server=False), "octo/demo")
    assert cred is None and note == "not_yours"
    # a member with their own installation (no setting): their repository yes, the server's no
    cred, _ = covered_by(GitHubAccess(user_id=sam, use_server=False), "sam/proj")
    assert cred and "ghs_77_" in cred.token and cred.source == "app"
    cred, note = covered_by(GitHubAccess(user_id=sam, use_server=False), "octo/demo")
    assert cred is None and note == "not_yours"
    # with the setting on, both
    for repo, tag in (("sam/proj", "ghs_77_"), ("octo/demo", "ghs_42_")):
        cred, _ = covered_by(GitHubAccess(user_id=sam, use_server=True), repo)
        assert cred and tag in cred.token
    # another member can never use sam's installation
    cred, note = covered_by(GitHubAccess(user_id=alex, use_server=True), "sam/proj")
    assert cred is None and note == "not_yours"


def test_the_servers_token_is_never_used_for_people_who_may_not_use_the_server(
    world, monkeypatch
) -> None:
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", PAT)
    get_settings.cache_clear()
    alex = world["alex"]
    cred, _ = covered_by(GitHubAccess(user_id=alex, use_server=False), "octo/other")
    assert cred is None  # the PAT is the server's
    cred, _ = covered_by(GitHubAccess(user_id=alex, use_server=True), "octo/other")
    assert cred and cred.source == "token"
    # and a member whose own installation doesn't cover the repo doesn't get the PAT either
    cred, _ = covered_by(GitHubAccess(user_id=world["sam"], use_server=False), "octo/other")
    assert cred is None


def test_a_member_with_their_own_installation_may_use_github_even_without_the_setting(
    world,
) -> None:
    with session_scope() as s:
        sam, alex = s.get(User, world["sam"]), s.get(User, world["alex"])
        assert github_allowed(sam) is True  # their own installation
        assert github_allowed(alex) is False  # nothing of their own, setting off
        assert github_allowed(None) is True  # the owner


def test_executors_pass_a_bool_unless_the_person_has_their_own_installation(world) -> None:
    from thinair.jobs.executors import github_access_for

    with session_scope() as s:
        sam, alex = s.get(User, world["sam"]), s.get(User, world["alex"])
        s.expunge_all()
    assert github_access_for(alex) is False  # exactly as before phase 7 (test_users relies on it)
    assert github_access_for(None) is True
    own = github_access_for(sam)
    assert isinstance(own, GitHubAccess) and own.user_id == world["sam"] and own.use_server is False


def test_admin_sync_never_reassigns_a_members_installation(world, gh) -> None:
    creds = ga.load_credentials()
    with session_scope() as s:
        ga.sync_installations(s, ga.GitHubApp(creds))
        assert s.get(GitHubInstallation, 77).user_id == world["sam"]  # still sam's
        assert s.get(GitHubInstallation, 42).user_id is None
        assert [i.installation_id for i in ga.active_installations(s)] == [42]


def test_server_pem_exists_only_in_the_secret_file() -> None:
    assert PEM  # (imported to keep the shared fixture key in one place)


# ---- the picker -----------------------------------------------------------------------------------
def test_a_member_lists_only_their_own_repositories_and_others_are_refused(client, gh) -> None:
    connected_app()
    seed_server_installation()
    _, sam = person("sam")
    _, alex = person("alex")
    client.get(f"/api/github/callback/user?code=sam-code&state={start(client, sam)}")
    gh.minted.clear()
    r = client.get("/api/github/repos", headers=sam)
    assert r.status_code == 200 and r.json()["total"] == 2
    assert {m["installation"] for m in gh.minted} == {
        77
    }  # sam's installation only, never the server's
    assert all(m["permissions"] == {"metadata": "read"} for m in gh.minted)
    refused = client.get("/api/github/repos", headers=alex)  # nothing of their own, no setting
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "github_not_allowed"
    gh.minted.clear()
    owner = client.get("/api/github/repos", headers=H)
    assert owner.status_code == 200 and {m["installation"] for m in gh.minted} == {42}  # not sam's
    st = client.get("/api/github", headers=sam).json()
    assert (
        st["allowed"] is True and client.get("/api/github", headers=alex).json()["allowed"] is False
    )
