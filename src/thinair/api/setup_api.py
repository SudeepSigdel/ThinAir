"""First-run API: `/api/meta` (open), `/api/setup` (the checklist) and `/api/health` (doctor).

`/api/setup` computes every step from real state each time (see `thinair.setup_state`), so it
cannot disagree with reality; the only things stored are the person's own choices (skip, dismiss,
preferred agent). Nothing here calls out to anything but Docker (health) and, through the existing
routes, GitHub.
"""

import asyncio
import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import select

from thinair import __version__, doctor, setup_state
from thinair.api.auth import Principal, _client_key, require_admin, require_auth
from thinair.api.github_api import status_payload
from thinair.api.login_api import login_volume
from thinair.config import get_settings
from thinair.db.models import Job, JobKind, JobStatus
from thinair.db.session import session_scope
from thinair.jobs.executors import agents_dir

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)], tags=["setup"])
open_router = APIRouter(prefix="/api", tags=["meta"])

API_VERSION = 1
FEATURES = ["github_app", "agent_login", "setup", "devices"]
_META_PER_MIN = 60
_meta_hits: dict[str, deque[float]] = defaultdict(deque)
_background: set[asyncio.Task] = set()
_building: set[str] = set()


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


# ---- meta (open) ------------------------------------------------------------------------------
@open_router.get("/meta")
def meta(request: Request) -> dict:
    """What this server is, so a cached app shell (or a future wrapper) can tell which screens
    it may show. Within `api: 1` changes are additive only. Open and throttled; nothing private."""
    key = _client_key(request)
    now = time.monotonic()
    q = _meta_hits[key]
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= _META_PER_MIN:
        raise HTTPException(429, "Too many requests; wait a minute.")
    q.append(now)
    if len(_meta_hits) > 10_000:
        for k in [k for k, v in _meta_hits.items() if not v or v[-1] < now - 60]:
            del _meta_hits[k]
    features = [*FEATURES, "push"] if get_settings().web_push else list(FEATURES)
    return {
        "version": __version__,
        "api": API_VERSION,
        "features": features,
        "agent_idle_s": get_settings().agent_idle_s,
    }


# ---- the checklist ------------------------------------------------------------------------------
def _facts(request: Request, who: Principal) -> setup_state.Facts:
    from thinair.agents.config import load_agents

    gh = status_payload(who)
    connected = (gh["mode"] == "app" and bool(gh["installations"])) or gh["mode"] == "token"
    if gh["mode"] == "app" and gh["installations"]:
        detail = "Connected as " + ", ".join(i["account"] for i in gh["installations"])
    elif gh["mode"] == "token":
        detail = "A token from .env"
    else:
        detail = ""
    agents = load_agents(agents_dir())[0]
    hub = getattr(request.app.state, "logins", None)

    def agent_ok(agent_id: str) -> bool:
        cfg = agents.get(agent_id)
        if cfg is None:
            return False
        if hub is not None and hub.known.get(login_volume(cfg, who)) is True:
            return True
        # An agent job that already succeeded proves the login works, even after a restart.
        with session_scope() as s:
            q = select(Job.id).where(Job.agent_id == agent_id, Job.status == JobStatus.SUCCEEDED)
            q = q.where(Job.kind == JobKind.AGENT)
            q = q.where(
                Job.user_id.is_(None) if who.user_id is None else Job.user_id == who.user_id
            )  # type: ignore[union-attr]
            return s.exec(q.limit(1)).first() is not None

    return setup_state.Facts(
        github_connected=connected,
        github_detail=detail,
        agent_ok=agent_ok,
        agent_names={k: v.name for k, v in agents.items()},
    )


@router.get("/setup")
def get_setup(request: Request, who: Principal = Depends(require_auth)) -> dict:
    facts = _facts(request, who)
    with session_scope() as s:
        return setup_state.steps_for(s, user_id=who.user_id, is_admin=who.is_admin, facts=facts)


class SetupIn(BaseModel):
    skip: str | None = Field(None, max_length=30)
    unskip: str | None = Field(None, max_length=30)
    dismiss: bool | None = None
    preferred_agent: str | None = Field(None, max_length=60)


@router.post("/setup")
async def post_setup(
    body: SetupIn, request: Request, who: Principal = Depends(require_auth)
) -> dict:
    """Record a choice: skip a step, dismiss the card, or pick the agent (which also starts
    preparing that agent's image in the background, once)."""
    from thinair.agents.config import load_agents

    with session_scope() as s:
        for name, flag in (("skip", body.skip), ("unskip", body.unskip)):
            if flag is None:
                continue
            if flag not in setup_state.STEPS:
                raise _err(422, "unknown_step", "That setup step doesn't exist.")
            if name == "skip":
                setup_state.set_choice(s, who.user_id, f"skip:{flag}", None)
            else:
                setup_state.clear(s, who.user_id, f"skip:{flag}")
        if body.dismiss is not None:
            if body.dismiss:
                setup_state.set_choice(s, who.user_id, "dismissed", None)
            else:
                setup_state.clear(s, who.user_id, "dismissed")
    if body.preferred_agent is not None:
        agents = load_agents(agents_dir())[0]
        choice = body.preferred_agent
        if choice != "other" and choice not in agents:
            raise _err(422, "unknown_agent", "Pick one of the configured agents.")
        with session_scope() as s:
            setup_state.set_choice(s, who.user_id, "preferred_agent", choice)
        if choice != "other" and who.is_admin:  # builds are heavy; a member's first job builds it
            _start_prebuild(request, agents[choice])
    return get_setup(request, who)


def _start_prebuild(request: Request, cfg) -> None:  # noqa: ANN001
    """Build the chosen agent's image now instead of at the first sign-in or job. Once per agent;
    a failure is only logged (the first job builds it again and reports properly)."""
    if cfg.id in _building:
        return
    _building.add(cfg.id)

    async def go() -> None:
        try:
            await _prebuild(cfg)
        except Exception as e:  # noqa: BLE001
            log.info("could not prepare the %s image ahead of time (%s)", cfg.id, type(e).__name__)
        finally:
            _building.discard(cfg.id)

    task = asyncio.create_task(go(), name=f"thinair-prebuild-{cfg.id}")
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _prebuild(cfg) -> None:  # noqa: ANN001
    from thinair.agents.cli_adapter import ensure_agent_image
    from thinair.sandbox.manager import SandboxManager

    await ensure_agent_image(SandboxManager(), cfg, "node", lambda _line: None)


# ---- health -------------------------------------------------------------------------------------
def _check_json(c: doctor.Check) -> dict:
    act = c.action
    return {
        "id": c.id,
        "status": c.status,
        "name": c.name,
        "detail": c.detail,
        "fix": c.fix,
        "action": {"kind": act[0], "target": act[1], "label": act[2]} if act else None,
    }


@router.get("/health")
async def get_health(who: Principal = Depends(require_admin)) -> dict:
    """`thinair doctor` for the phone: the same checks, offline (no outbound request), problems
    first. Admins only."""
    checks = await asyncio.to_thread(doctor.run_checks, None, online=False)
    order = {"fail": 0, "warn": 1, "ok": 2}
    rows = sorted((_check_json(c) for c in checks), key=lambda r: order[r["status"]])
    problems = sum(1 for r in rows if r["status"] != "ok")
    with session_scope() as s:
        first_run = setup_state.funnel(s, None)
    return {"checks": rows, "problems": problems, "first_run": first_run}


@router.post("/health/fix/images", status_code=202)
async def fix_images(who: Principal = Depends(require_admin)) -> dict:
    """Prepare the sandbox images now (what `thinair images` does), in the background, once."""
    if "images" in _building:
        return {"started": False}
    _building.add("images")

    async def go() -> None:
        from thinair.project.prepare import BUILDABLE_IMAGES, ensure_image
        from thinair.sandbox.manager import SandboxManager

        try:
            mgr = SandboxManager()
            for stack in BUILDABLE_IMAGES:
                await ensure_image(mgr, stack, lambda _line: None)
        except Exception as e:  # noqa: BLE001
            log.info("could not prepare the sandbox images (%s)", type(e).__name__)
        finally:
            _building.discard("images")

    task = asyncio.create_task(go(), name="thinair-prepare-images")
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"started": True}
