"""Sign an agent in from the phone (see `thinair.agents.login`). Every route needs a login.

A session belongs to the user who started it: anyone else, admins included, gets 404, because
it is that person's login volume (`thinair-auth-<agent>[.<user>]`) being written.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from thinair.agents.config import AgentConfig, load_agents
from thinair.agents.login import (
    LoginBusy,
    LoginHub,
    LoginInputError,
    LoginSession,
    TooManyLogins,
)
from thinair.api.auth import require_auth
from thinair.db.models import User
from thinair.db.session import session_scope
from thinair.jobs.executors import agents_dir
from thinair.users import Principal, auth_volume_name

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)], tags=["agent login"])


def _err(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


def hub(request: Request) -> LoginHub:
    return request.app.state.logins


def owner_key(who: Principal) -> str:
    return who.user_id or "owner"


def login_volume(cfg: AgentConfig, who: Principal) -> str:
    """This caller's login volume for the agent (shared-login agents use the other's)."""
    if who.user_id is None:
        return auth_volume_name(cfg.login_id, None)
    with session_scope() as s:
        user = s.get(User, who.user_id)
    return auth_volume_name(cfg.login_id, user)


def flow_agent(agents: dict[str, AgentConfig], cfg: AgentConfig) -> AgentConfig | None:
    """The agent whose image and login flow sign `cfg` in: itself, or the agent whose login it
    shares (claude-acp is signed in through Claude Code). None = no in-app sign-in."""
    if cfg.auth.login_flow is not None:
        return cfg
    other = agents.get(cfg.auth.shares_login_with or "")
    if other is not None and other.auth.login_flow is not None:
        return other
    return None


def _agent(agent_id: str) -> tuple[dict[str, AgentConfig], AgentConfig]:
    agents = load_agents(agents_dir())[0]
    cfg = agents.get(agent_id)
    if cfg is None:
        raise _err(404, "unknown_agent", "No agent with that id.")
    return agents, cfg


def _session(request: Request, sid: str, who: Principal) -> LoginSession:
    s = hub(request).get(sid, owner_key(who))
    if s is None:
        raise _err(404, "login_not_found", "This sign-in doesn't exist or has ended.")
    return s


def _out(s: LoginSession) -> dict[str, Any]:
    return s.public()


@router.post("/agents/{agent_id}/login", status_code=201)
async def start_login(
    agent_id: str, request: Request, who: Principal = Depends(require_auth)
) -> Any:
    """Start signing this agent in for the caller. 409 returns their sign-in already running."""
    agents, cfg = _agent(agent_id)
    flow_cfg = flow_agent(agents, cfg)
    if flow_cfg is None:
        raise _err(
            422,
            "no_in_app_login",
            f"{cfg.name} can't be signed in from the app. On the server, run: "
            f"thinair auth {cfg.login_id} (see its --print-command option for Docker).",
            command=f"thinair auth {cfg.login_id}"
            + ("" if who.user_id is None else f" --user {who.name}"),
        )
    try:
        s = hub(request).start(flow_cfg, owner_key(who), login_volume(flow_cfg, who))
    except LoginBusy as busy:
        raise _err(
            409,
            "login_running",
            "A sign-in is already running. Finish or cancel it first.",
            session=busy.session.id,
            agent=busy.session.cfg.id,
        ) from None
    except TooManyLogins:
        raise _err(
            429, "too_many_logins", "Too many sign-ins are running. Try again in a few minutes."
        ) from None
    return _out(s)


@router.get("/agent-logins/{sid}")
def get_login(sid: str, request: Request, who: Principal = Depends(require_auth)) -> Any:
    return _out(_session(request, sid, who))


@router.get("/agent-logins/{sid}/events", response_class=StreamingResponse)
def login_events(sid: str, request: Request, who: Principal = Depends(require_auth)) -> Any:
    """Server-Sent Events: `state` events, starting with the current one; ends when finished."""
    s = _session(request, sid, who)
    return StreamingResponse(
        s.events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=2000)


@router.post("/agent-logins/{sid}/input", status_code=202)
async def login_input(
    sid: str, body: CodeIn, request: Request, who: Principal = Depends(require_auth)
) -> Any:
    """Type the provider's code at the agent's prompt (only while it is asking for one)."""
    s = _session(request, sid, who)
    try:
        await hub(request).submit(s, body.code)
    except LoginInputError as e:
        raise _err(409 if e.code == "not_waiting_for_code" else 422, e.code, e.message) from None
    return {"state": s.snap.state}


@router.delete("/agent-logins/{sid}", status_code=204)
async def cancel_login(
    sid: str, request: Request, who: Principal = Depends(require_auth)
) -> Response:
    s = _session(request, sid, who)
    await hub(request).cancel(s)
    return Response(status_code=204)


@router.post("/agents/{agent_id}/login/check")
async def check_login(
    agent_id: str, request: Request, who: Principal = Depends(require_auth)
) -> Any:
    """Run the agent's login check once for the caller: logged_in true, false or null (unknown)."""
    agents, cfg = _agent(agent_id)
    target = flow_agent(agents, cfg) or cfg
    try:
        ok = await hub(request).check(target, login_volume(target, who))
    except TooManyLogins:
        raise _err(
            429, "check_running", "A check is already running. Try again in a moment."
        ) from None
    return {"logged_in": ok}


def login_info(
    request: Request, agents: dict[str, AgentConfig], cfg: AgentConfig, who: Principal
) -> dict[str, Any]:
    """For GET /api/agents: can it be signed in from the app, and is it (as far as we know)?
    Never runs a container: the state comes from sign-ins and explicit checks."""
    target = flow_agent(agents, cfg) or cfg
    known = hub(request).known.get(login_volume(target, who))
    state = "unknown" if known is None else ("ok" if known else "missing")
    return {"in_app": flow_agent(agents, cfg) is not None, "state": state}
