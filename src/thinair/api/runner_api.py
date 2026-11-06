"""HTTP side of remote runners (see thinair/runners.py).

- `/runner/v1/*`: called by runners, authenticated with a runner secret (`thinair_runner_...`).
  User API tokens are not accepted here, and runner secrets are not accepted anywhere else.
- `/api/runners*`: admins register, list and disable runners.

Request bodies from runners are read with a size cap before parsing, and every field is
validated: a runner can only report events and a result for a job it currently holds.
"""

import json
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from thinair.api import auth as api_auth
from thinair.api.auth import require_admin, require_auth
from thinair.db.models import EventType, Job, JobStatus, Repo, Runner, utcnow
from thinair.db.session import session_scope
from thinair.jobs.service import ExecResult
from thinair.profiler.models import ProfileResult, RunRecord
from thinair.runners import (
    CLAIM_WAIT_MAX_S,
    PROTOCOL,
    RunnerError,
    RunnerHub,
    create_runner,
    disable_runner,
    list_runners,
    verify_runner,
)
from thinair.users import Principal

MAX_BODY = 4 * 1024 * 1024
_SEEN_GRANULARITY = timedelta(minutes=1)


def _hub(request: Request) -> RunnerHub:
    return request.app.state.runners


# ---- runner authentication ------------------------------------------------------------------
class RunnerIdentity(BaseModel):
    id: str
    name: str


def require_runner(request: Request) -> RunnerIdentity:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail={"code": "bad_runner_token", "message": "Unknown or disabled runner secret."},
        headers={"WWW-Authenticate": "Bearer"},
    )
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthorized
    with session_scope() as s:
        runner = verify_runner(s, token.strip())
        if runner is not None:
            return RunnerIdentity(id=runner.id, name=runner.name)
    key = api_auth._client_key(request)
    if api_auth._throttled(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts; wait.")
    api_auth.record_failure(key)
    raise unauthorized


async def _body(request: Request, model: type[BaseModel]) -> Any:
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise HTTPException(413, "Request too large.")
        chunks.append(chunk)
    try:
        return model.model_validate(json.loads(b"".join(chunks) or b"{}"))
    except (ValueError, ValidationError) as e:
        raise HTTPException(
            422,
            detail={"code": "bad_request", "message": str(e)[:500]},
        ) from e


# ---- runner protocol --------------------------------------------------------------------------
class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    protocol: int = PROTOCOL
    version: str = Field(default="", max_length=40)
    cpus: float | None = Field(default=None, ge=0, le=4096)
    memory_bytes: int | None = Field(default=None, ge=0)


class EventIn(BaseModel):
    type: Literal["log", "profile_run"]
    line: str | None = Field(default=None, max_length=8000)
    run: RunRecord | None = None


class EventsIn(BaseModel):
    events: list[EventIn] = Field(default_factory=list, max_length=1000)


class FinishIn(BaseModel):
    outcome: Literal["profiled", "failed"]
    result: ProfileResult | None = None
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    stack: str | None = Field(default=None, max_length=40)
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$")
    error_message: str | None = Field(default=None, max_length=2000)


runner_router = APIRouter(prefix="/runner/v1", tags=["runners"])


@runner_router.post("/claim", response_model=None)
async def claim(
    request: Request, wait: float = CLAIM_WAIT_MAX_S, me: RunnerIdentity = Depends(require_runner)
) -> dict[str, Any] | Response:
    """Long-poll for a profile job. 204 = nothing to do yet (ask again)."""
    body: ClaimIn = await _body(request, ClaimIn)
    if body.protocol != PROTOCOL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "protocol_mismatch",
                "message": f"This server speaks runner protocol {PROTOCOL}; update the runner.",
            },
        )
    _record_seen(me.id, body)
    lease = await _hub(request).claim(me.id, me.name, wait)
    if lease is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    with session_scope() as s:
        job = s.get(Job, lease.job_id)
        repo = s.get(Repo, job.repo_id) if job else None
        if job is None or repo is None:  # deleted meanwhile; the executor will time out
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return {
            "protocol": PROTOCOL,
            "job": {
                "id": job.id,
                "kind": job.kind.value,
                "repo_url": repo.url,
                "ref": job.ref,
                "github_auth": lease.github_auth,
            },
        }


@runner_router.post("/jobs/{job_id}/events")
async def post_events(
    job_id: str, request: Request, me: RunnerIdentity = Depends(require_runner)
) -> dict[str, bool]:
    """Append log lines / probe results. Doubles as the heartbeat. `cancel: true` = stop."""
    body: EventsIn = await _body(request, EventsIn)
    lease = _hub(request).lease_for(job_id, me.id)
    if lease is None:
        return {"cancel": True}
    events = request.app.state.jobs.events
    for ev in body.events:
        if ev.type == "log" and ev.line is not None:
            events.emit(job_id, EventType.LOG, {"line": ev.line}, "output")
        elif ev.type == "profile_run" and ev.run is not None:
            events.emit(job_id, EventType.PROFILE_RUN, ev.run.model_dump(mode="json"))
    return {"cancel": False}


@runner_router.post("/jobs/{job_id}/finish")
async def finish(
    job_id: str, request: Request, me: RunnerIdentity = Depends(require_runner)
) -> dict[str, bool]:
    body: FinishIn = await _body(request, FinishIn)
    lease = _hub(request).lease_for(job_id, me.id)
    if lease is None or lease.done is None or lease.done.done():
        return {"accepted": False}
    if body.outcome == "profiled":
        if body.result is None:
            raise HTTPException(
                422,
                detail={"code": "bad_request", "message": "A profiled job needs its result."},
            )
        from thinair.jobs.executors import save_profile_result

        with session_scope() as s:
            job = s.get(Job, job_id)
            repo = s.get(Repo, job.repo_id) if job else None
            name = repo.name if repo else ""
        res = save_profile_result(job_id, name, body.result, body.commit_sha, body.stack)
    else:
        res = ExecResult(
            JobStatus.FAILED,
            body.error_code or "runner_failed",
            body.error_message or "The runner could not finish this job.",
        )
    lease.done.set_result(res)
    return {"accepted": True}


def _record_seen(runner_id: str, body: ClaimIn) -> None:
    info = {"version": body.version, "cpus": body.cpus, "memory_bytes": body.memory_bytes}
    with session_scope() as s:
        row = s.get(Runner, runner_id)
        if row is None:
            return
        now = utcnow()
        last = row.last_seen_at.replace(tzinfo=now.tzinfo) if row.last_seen_at else None
        if row.info != info or last is None or now - last > _SEEN_GRANULARITY:
            row.info, row.last_seen_at = info, now
            s.add(row)
            s.commit()


# ---- admin management -------------------------------------------------------------------------
class RunnerOut(BaseModel):
    name: str
    created_at: datetime
    last_seen_at: datetime | None
    disabled: bool
    online: bool
    info: dict[str, Any]


class RunnerNew(BaseModel):
    name: str = Field(max_length=64)


admin_router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)], tags=["runners"])


def _out(r: Runner, hub: RunnerHub) -> RunnerOut:
    return RunnerOut(
        name=r.name,
        created_at=r.created_at,
        last_seen_at=r.last_seen_at,
        disabled=r.disabled_at is not None,
        online=r.disabled_at is None and r.id in hub.online(),
        info=r.info or {},
    )


@admin_router.get("/runners", response_model=list[RunnerOut])
def runners_list(request: Request, _admin: Principal = Depends(require_admin)) -> list[RunnerOut]:
    with session_scope() as s:
        return [_out(r, _hub(request)) for r in list_runners(s)]


@admin_router.post("/runners", status_code=201)
def runners_create(
    body: RunnerNew, request: Request, _admin: Principal = Depends(require_admin)
) -> dict[str, str]:
    """Register a runner. The secret is in this response only; put it on the runner machine."""
    with session_scope() as s:
        try:
            runner, token = create_runner(s, body.name)
        except RunnerError as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT if e.code == "runner_exists" else 422,
                detail={"code": e.code, "message": e.message},
            ) from e
        return {"name": runner.name, "token": token}


@admin_router.post("/runners/{name}/disable", response_model=RunnerOut)
def runners_disable(
    name: str, request: Request, _admin: Principal = Depends(require_admin)
) -> RunnerOut:
    with session_scope() as s:
        try:
            return _out(disable_runner(s, name), _hub(request))
        except RunnerError as e:
            raise HTTPException(404, detail={"code": e.code, "message": e.message}) from e
