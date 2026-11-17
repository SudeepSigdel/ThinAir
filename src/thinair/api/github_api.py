"""Connect GitHub: the manifest flow, installations and the repository picker.

Two routers. `router` is authenticated like every other `/api` route. `open_router` holds the two
redirect targets GitHub sends the phone's browser to; they carry no Thinair session, so each
acts only on a live one-time `state` (or, for the setup redirect, does nothing but re-read what
GitHub says), is throttled per client address, and never trusts the `installation_id` it is given.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from thinair import github_app as ga
from thinair import setup_state
from thinair.api.auth import (
    Principal,
    _client_key,
    _throttled,
    record_failure,
    require_admin,
    require_auth,
)
from thinair.config import get_settings
from thinair.db.models import GitHubInstallation, Repo
from thinair.db.session import session_scope
from thinair.github import GitHubAPIError, GitHubTokenError, github_token
from thinair.onetime import consume_state, consume_state_user, issue_state
from thinair.users import github_allowed, github_token_allowed

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/github", dependencies=[Depends(require_auth)], tags=["github"])
open_router = APIRouter(prefix="/api/github", tags=["github callbacks"], include_in_schema=False)

# The setup redirect has no state to check (it can also be reached from GitHub's own "Configure"
# page), so it is held to a per-address budget and a global cooldown between GitHub calls.
_SETUP_PER_MIN = 20
_SYNC_COOLDOWN_S = 5.0
_setup_hits: dict[str, deque[float]] = defaultdict(deque)
_last_sync = [0.0]


def _err(status: int, code: str, message: str, **extra) -> HTTPException:  # noqa: ANN003
    return HTTPException(status, detail={"code": code, "message": message, **extra})


def _setup_limited(key: str) -> bool:
    now = time.monotonic()
    q = _setup_hits[key]
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= _SETUP_PER_MIN:
        return True
    q.append(now)
    if len(_setup_hits) > 10_000:  # a flood of addresses must not grow the table forever
        for k in [k for k, v in _setup_hits.items() if not v or v[-1] < now - 60]:
            del _setup_hits[k]
    return False


def _installation_out(row: GitHubInstallation) -> dict:
    return {
        "id": row.installation_id,
        "account": row.account_login,
        "selection": row.repository_selection,
        "manage_url": ga.manage_url(row),
    }


def status_payload(who: Principal) -> dict:
    """What the app shows on the Connect GitHub screen. No secret is ever in it."""
    app, problem, installs = None, None, []
    try:
        creds = ga.load_credentials()
    except GitHubAPIError as e:
        creds, problem = None, e.message
    if creds is not None:
        app = {"slug": creds.slug, "html_url": creds.html_url, "owner": creds.owner_login}
        with session_scope() as s:
            installs = [_installation_out(r) for r in ga.active_installations(s)]
    elif problem is not None:
        app = {"slug": None, "html_url": None, "owner": None}
    token_set = False
    try:
        token_set = github_token() is not None
    except GitHubTokenError as e:
        problem = problem or str(e)
        token_set = True
    mode = "app" if app is not None else "token" if token_set else "none"
    mine = None
    if who.user_id is not None:  # a member's own connection (the owner uses the server's)
        with session_scope() as s:
            own = [_installation_out(r) for r in ga.installations_for(s, who.user_id)]
        mine = {"installations": own, "can_connect": bool(creds and creds.callback_ready)}
    return {
        "mine": mine,
        "callback_ready": bool(creds and creds.callback_ready),
        "mode": mode,
        "app": app,
        "installations": installs,
        "token_in_env": token_set,
        "problem": problem,
        "can_manage": who.is_admin,
        "allowed": github_allowed(_member(who)),
        "public_url_set": bool(get_settings().public_url),
    }


def _member(who: Principal):  # noqa: ANN202
    from thinair.db.models import User

    with session_scope() as s:
        return s.get(User, who.user_id) if who.user_id else None


@router.get("")
def get_status(who: Principal = Depends(require_auth)) -> dict:
    return status_payload(who)


class AppIn(BaseModel):
    org: str | None = Field(None, max_length=60)


@router.post("/app")
def start_app(body: AppIn | None = None, who: Principal = Depends(require_admin)) -> dict:
    """Begin the manifest flow. The page submits `manifest` to `action` as a real form POST (a
    top-level navigation to github.com; no CORS involved)."""
    public = get_settings().public_url
    if not public:
        raise _err(
            409,
            "no_public_url",
            "Set THINAIR_PUBLIC_URL first: GitHub sends your phone's browser back to that "
            "address (the installer sets it up).",
        )
    if ga.app_connected():
        raise _err(
            409, "app_exists", "Thinair already has a GitHub App. Disconnect it to start over."
        )
    try:
        org = ga.check_org(body.org if body else None)
    except ValueError as e:
        raise _err(422, "bad_org", str(e)) from None
    with session_scope() as s:
        state = issue_state(s, "github_app", who.user_id)
    manifest = ga.build_manifest(public, ga.new_app_name())
    return {"action": ga.manifest_action(org, state), "manifest": json.dumps(manifest)}


def _to_app_screen(result: str | None = None) -> RedirectResponse:
    suffix = f"?result={result}" if result else ""
    return RedirectResponse(f"/#/setup/github{suffix}", status_code=303)


@open_router.get("/callback/app")
async def callback_app(
    request: Request,
    code: str | None = Query(None, max_length=200),
    state: str | None = Query(None, max_length=200),
) -> RedirectResponse:
    """GitHub redirects here after "Create GitHub App". Nothing outbound happens unless `state`
    is one an admin just asked for; then the one-time `code` becomes the app's credentials."""
    key = _client_key(request)
    if _throttled(key):
        raise HTTPException(429, "Too many attempts; wait a minute.")
    with session_scope() as s:
        live = consume_state(s, state, "github_app")
    if not live:
        record_failure(key)
        return _to_app_screen("bad_state")
    if not code:
        return _to_app_screen("failed")
    try:
        creds = await asyncio.to_thread(ga.convert, code)
        await asyncio.to_thread(ga.save_credentials, creds)
    except GitHubAPIError as e:
        log.warning("GitHub App creation did not finish (status %s)", e.status)
        return _to_app_screen("failed")
    except OSError:
        log.warning("could not store the GitHub App credentials")
        return _to_app_screen("failed")
    with session_scope() as s:
        install_state = issue_state(s, "github_install")
    return RedirectResponse(ga.install_url(creds.slug, install_state), status_code=303)


@open_router.get("/callback/setup")
async def callback_setup(
    request: Request, state: str | None = Query(None, max_length=200)
) -> RedirectResponse:
    """GitHub redirects here after installing or reconfiguring the app. The `installation_id` it
    carries can be spoofed, so it is ignored: Thinair asks GitHub (as the app) what is installed."""
    key = _client_key(request)
    if _throttled(key) or _setup_limited(key):
        raise HTTPException(429, "Too many attempts; wait a minute.")
    if not ga.app_connected():
        return _to_app_screen("no_app")
    with session_scope() as s:
        trusted = consume_state(s, state, "github_install")
    now = time.monotonic()
    if not trusted and now - _last_sync[0] < _SYNC_COOLDOWN_S:
        return _to_app_screen()  # just refreshed; the screen reads the table
    _last_sync[0] = now
    try:
        rows = await asyncio.to_thread(_sync)
    except GitHubAPIError as e:
        log.warning("could not read the app's installations (status %s)", e.status)
        return _to_app_screen("sync_failed")
    if rows:
        setup_state.mark("github_connected")
    return _to_app_screen("connected")


def _sync() -> list[GitHubInstallation]:
    creds = ga.load_credentials()
    if creds is None:
        raise GitHubAPIError(None, "GitHub is not connected.")
    with session_scope() as s:
        return ga.sync_installations(s, ga.GitHubApp(creds))


@router.post("/sync")
async def sync(who: Principal = Depends(require_admin)) -> dict:
    """Re-read the installations (the app calls this when the page becomes visible again)."""
    try:
        rows = await asyncio.to_thread(_sync)
    except GitHubAPIError as e:
        raise _err(502, "github_unreachable", e.message) from None
    if rows:
        setup_state.mark("github_connected")
    return status_payload(who)


@router.get("/repos")
async def list_repos(
    installation_id: int | None = Query(None), who: Principal = Depends(require_auth)
) -> dict:
    """Repositories the app can see, newest push first (the picker). One GitHub call per page
    of 100, with a metadata-only key that is revoked straight after."""
    person = _member(who)
    if not github_allowed(person):
        raise _err(403, "github_not_allowed", "Only admins can use the server's GitHub connection.")
    try:
        creds = ga.load_credentials()
    except GitHubAPIError as e:
        raise _err(409, "app_broken", e.message) from None
    if creds is None:
        raise _err(
            409,
            "no_app",
            "Connect GitHub first. (A token in .env works for cloning by URL, but can't list "
            "repositories.)",
        )
    with session_scope() as s:
        # Their own installations, plus the server's if they may use the server's connection.
        rows = ga.installations_for(s, who.user_id) if who.user_id is not None else []
        if github_token_allowed(person):
            rows = [*rows, *ga.active_installations(s)]
    chosen = [r for r in rows if installation_id in (None, r.installation_id)]
    if not chosen:
        raise _err(404, "no_installation", "Thinair's app isn't installed on that account yet.")
    app = ga.GitHubApp(creds)
    repos: list[dict] = []
    try:
        for r in chosen:
            for item in await asyncio.to_thread(app.list_repos, r.installation_id):
                repos.append(item | {"_manage": ga.manage_url(r)})
    except GitHubAPIError as e:
        raise _err(502, "github_unreachable", e.message) from None
    repos.sort(key=lambda x: x.get("pushed_at") or "", reverse=True)
    with session_scope() as s:
        known = {r.url for r in s.exec(select(Repo))}
    out = [
        {
            "id": x.get("id"),
            "full_name": x.get("full_name"),
            "url": x.get("html_url"),
            "private": bool(x.get("private")),
            "default_branch": x.get("default_branch"),
            "pushed_at": x.get("pushed_at"),
            "description": (x.get("description") or "")[:200],
            "language": x.get("language"),
            "added": x.get("html_url") in known,
        }
        for x in repos
        if isinstance(x.get("html_url"), str) and x["html_url"].startswith("https://github.com/")
    ]
    return {"repos": out, "total": len(out), "manage_url": ga.manage_url(chosen[0])}


@router.delete("/app")
async def disconnect_app(who: Principal = Depends(require_admin)) -> dict:
    """Uninstall the app, delete its key and rows. GitHub has no API to delete the registration
    itself, so the app's settings page is returned for that last tap."""

    def _go() -> str | None:
        with session_scope() as s:
            return ga.disconnect(s)

    delete_url = await asyncio.to_thread(_go)
    return {"delete_url": delete_url}


# ---- members connect their own installation ------------------------------------------------
# The member authorizes Thinair on GitHub with our own one-time `state` (so the callback knows who
# it is for), we trade the code for a short-lived user token, and an installation is claimed only if
# it appears in `GET /user/installations` for THAT token: a spoofed installation id claims nothing.
# The user token is used for two read calls and dropped.
_MEMBER_PURPOSE = "github_member"


def _redirect_uri() -> str:
    return get_settings().public_url.rstrip("/") + "/api/github/callback/user"  # type: ignore[union-attr]


@router.post("/mine/start")
def mine_start(who: Principal = Depends(require_auth)) -> dict:
    """Begin connecting this person's own GitHub account. Returns where to send the browser."""
    if who.user_id is None:
        raise _err(409, "use_server_connection", "The owner connects GitHub for the whole server.")
    try:
        creds = ga.load_credentials()
    except GitHubAPIError as e:
        raise _err(409, "app_broken", e.message) from None
    if creds is None:
        raise _err(409, "no_app", "An admin needs to connect GitHub for the server first.")
    if not creds.callback_ready:
        raise _err(
            409,
            "members_not_ready",
            "An admin needs to reconnect GitHub once so people can connect their own account.",
        )
    if not get_settings().public_url:
        raise _err(409, "no_public_url", "THINAIR_PUBLIC_URL must be set.")
    with session_scope() as s:
        state = issue_state(s, _MEMBER_PURPOSE, who.user_id)
    return {"action": ga.GitHubApp(creds).authorize_url(state, _redirect_uri())}


@open_router.get("/callback/user")
async def callback_user(
    request: Request,
    code: str | None = Query(None, max_length=200),
    state: str | None = Query(None, max_length=200),
    error: str | None = Query(None, max_length=60),
) -> RedirectResponse:
    """GitHub sends the member back here after they authorize. Nothing outbound happens unless
    `state` is one this server issued for a signed-in person a few minutes ago."""
    key = _client_key(request)
    if _throttled(key):
        raise HTTPException(429, "Too many attempts; wait a minute.")
    with session_scope() as s:
        live, user_id = consume_state_user(s, state, _MEMBER_PURPOSE)
    if not live or user_id is None:
        record_failure(key)
        return _to_app_screen("bad_state")
    if error or not code:
        return _to_app_screen("denied" if error else "failed")

    def work() -> tuple[str, int, str | None]:
        creds = ga.load_credentials()
        if creds is None or not creds.callback_ready:
            raise GitHubAPIError(None, "GitHub is not connected for members.")
        app = ga.GitHubApp(creds)
        token = app.exchange_user_code(code, _redirect_uri())
        login, verified = app.user_installations(token)
        if not verified:
            return "install", 0, creds.slug  # they haven't installed the app on their account yet
        with session_scope() as s:
            claimed = ga.claim_installations(s, user_id, login, verified, app)
        return ("mine_connected" if claimed else "mine_taken"), len(claimed), None

    try:
        result, _n, slug = await asyncio.to_thread(work)
    except GitHubAPIError as e:
        log.warning("a member's GitHub sign-in did not finish (status %s)", e.status)
        return _to_app_screen("failed")
    if result == "install" and slug:
        return RedirectResponse(ga.install_url(slug), status_code=303)
    setup_state.mark("github_connected", user_id)
    return _to_app_screen(result)


@router.delete("/mine", status_code=204)
def mine_disconnect(who: Principal = Depends(require_auth)) -> None:
    """Forget this person's installations here (remove the app on GitHub to revoke it there)."""
    if who.user_id is None:
        raise _err(409, "use_server_connection", "Use Disconnect on the server's connection.")
    with session_scope() as s:
        for row in ga.installations_for(s, who.user_id):
            row.removed_at = ga.utcnow()
            s.add(row)
        s.commit()
