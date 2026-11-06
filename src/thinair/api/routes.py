"""REST + SSE routes. Every route requires auth (router-level dependency).

Visibility: admins see every job; members see only their own (others' are a plain 404, so ids
reveal nothing), and only repositories they have jobs for. A job keeps its owner on retry and
when a pull request is opened from it.

Errors use one shape: `{"detail": {"code": "...", "message": "..."}}`, where `message` is safe
and readable to show on a phone.
"""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlmodel import Session, case, func, select

from thinair.agents.config import load_agents
from thinair.api.auth import require_admin, require_auth
from thinair.auth import create_token
from thinair.db.models import Job, JobKind, JobResult, JobStatus, ProfileReport, Repo, User
from thinair.db.session import session_scope
from thinair.jobs.events import TERMINAL_STATUSES
from thinair.jobs.executors import agents_dir
from thinair.jobs.pr import PRError
from thinair.jobs.service import JobService
from thinair.project.prepare import GitSource, PrepareError
from thinair.users import (
    OWNER_NAME,
    Principal,
    UserError,
    create_user,
    disable_user,
    get_user,
    list_users,
    set_job_limit,
)

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


def _err(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(code, detail={"code": error, "message": message})


def jobs_service(request: Request) -> JobService:
    return request.app.state.jobs


# ---- schemas -------------------------------------------------------------------------------
class LastJob(BaseModel):
    id: str
    kind: JobKind
    status: JobStatus
    task: str | None = None
    created_at: datetime


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    url: str
    name: str
    created_at: datetime
    default_agent_id: str | None = None
    default_ref: str | None = None
    # Counted over the jobs the caller can see (members: their own).
    jobs: int = 0
    active_jobs: int = 0
    last_job: LastJob | None = None


class RepoSettingsIn(BaseModel):
    default_agent_id: str | None = Field(None, max_length=100)
    default_ref: str | None = Field(None, max_length=200)


class JobOut(BaseModel):
    id: str
    repo_id: str
    repo_url: str
    repo_name: str
    kind: JobKind
    status: JobStatus
    agent_id: str | None = None
    task: str | None = None
    ref: str | None = None
    commit_sha: str | None = None
    parent_job_id: str | None = None
    interactive: bool = False
    ask_permissions: bool = False
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    logs_removed_at: datetime | None = None  # log lines deleted by the retention cleanup
    finished: bool

    @classmethod
    def of(cls, job: Job, repo: Repo) -> "JobOut":
        data = job.model_dump(exclude={"limits"})
        return cls(
            **data,
            repo_url=repo.url,
            repo_name=repo.name,
            finished=job.status in TERMINAL_STATUSES,
        )


class RepoIn(BaseModel):
    url: str = Field(min_length=1, max_length=300)


class JobIn(BaseModel):
    repo_url: str | None = Field(None, max_length=300)
    repo_id: str | None = None
    kind: JobKind = JobKind.PROFILE
    ref: str | None = Field(None, max_length=200)
    task: str | None = Field(None, max_length=10_000)
    agent_id: str | None = Field(None, max_length=100)
    # Agent conversations (ACP agents only): reply to the agent and approve its actions while
    # it runs (POST /jobs/{id}/input).
    interactive: bool = False
    ask_permissions: bool = False

    @model_validator(mode="after")
    def _one_repo(self) -> "JobIn":
        if bool(self.repo_url) == bool(self.repo_id):
            raise ValueError("provide exactly one of repo_url or repo_id")
        return self


# ---- helpers -------------------------------------------------------------------------------
def _job_and_repo(s: Session, job_id: str, who: Principal) -> tuple[Job, Repo]:
    job = s.get(Job, job_id)
    repo = s.get(Repo, job.repo_id) if job else None
    if job is None or repo is None or not who.can_see(job):
        raise _err(status.HTTP_404_NOT_FOUND, "job_not_found", "No job with that id.")
    return job, repo


def _submit(svc: JobService, who: Principal, url: str, **kw: Any) -> JobOut:
    try:
        job = svc.submit(url, **kw)
    except PrepareError as e:
        raise _err(429 if e.code == "too_many_jobs" else 422, e.code, e.message) from e
    with session_scope() as s:
        return JobOut.of(*_job_and_repo(s, job.id, who))


def _visible_repo(s: Session, repo_id: str, who: Principal) -> Repo:
    repo = s.get(Repo, repo_id)
    if repo is not None and not who.is_admin:
        mine = s.exec(select(Job.id).where(Job.repo_id == repo_id, Job.user_id == who.user_id))
        if mine.first() is None:
            repo = None
    if repo is None:
        raise _err(status.HTTP_404_NOT_FOUND, "repo_not_found", "No repository with that id.")
    return repo


# ---- repos ---------------------------------------------------------------------------------
def _repo_out(s: Session, repos: list[Repo], who: Principal) -> list[RepoOut]:
    """Repos with job counts and their latest job, most recently used first."""
    ids = [r.id for r in repos]
    unfinished = Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])  # type: ignore[attr-defined]
    q = select(
        Job.repo_id, func.count(), func.sum(case((unfinished, 1), else_=0)), func.max(Job.id)
    ).where(Job.repo_id.in_(ids))  # type: ignore[attr-defined]
    if not who.is_admin:
        q = q.where(Job.user_id == who.user_id)
    stats = {rid: (n, act or 0, last) for rid, n, act, last in s.exec(q.group_by(Job.repo_id))}
    last_ids = [v[2] for v in stats.values()]
    last = {j.id: j for j in s.exec(select(Job).where(Job.id.in_(last_ids)))}  # type: ignore[attr-defined]
    out = []
    for r in repos:
        n, act, last_id = stats.get(r.id, (0, 0, None))
        j = last.get(last_id) if last_id else None
        extra = {
            "jobs": n,
            "active_jobs": act,
            "last_job": LastJob(**j.model_dump()) if j else None,
        }
        out.append(RepoOut.model_validate(r).model_copy(update=extra))
    # Most recent job first; projects without (visible) jobs after, newest added first.
    return sorted(
        out,
        key=lambda o: (o.last_job.id if o.last_job else "", o.created_at.isoformat()),
        reverse=True,
    )


@router.get("/repos", response_model=list[RepoOut], tags=["repos"])
def list_repos(who: Principal = Depends(require_auth)) -> list[RepoOut]:
    """The caller's projects, most recently used first, with job counts and the latest job."""
    with session_scope() as s:
        q = select(Repo)
        if not who.is_admin:
            mine = select(Job.repo_id).where(Job.user_id == who.user_id)
            q = q.where(Repo.id.in_(mine))  # type: ignore[attr-defined]
        return _repo_out(s, list(s.exec(q)), who)


@router.post("/repos", response_model=RepoOut, status_code=201, tags=["repos"])
def add_repo(body: RepoIn) -> Repo:
    """Register a GitHub repository (idempotent). Private ones need THINAIR_GITHUB_TOKEN."""
    source = GitSource(body.url.strip().rstrip("/"))
    try:
        source.validate()
    except PrepareError as e:
        raise _err(422, e.code, e.message) from e
    with session_scope() as s:
        repo = s.exec(select(Repo).where(Repo.url == source.url)).first()
        if repo is None:
            repo = Repo(url=source.url, name=source.url.split("/")[-1].removesuffix(".git"))
            s.add(repo)
            s.commit()
        return repo


@router.get("/repos/{repo_id}", response_model=RepoOut, tags=["repos"])
def get_repo(repo_id: str, who: Principal = Depends(require_auth)) -> RepoOut:
    with session_scope() as s:
        return _repo_out(s, [_visible_repo(s, repo_id, who)], who)[0]


@router.patch("/repos/{repo_id}", response_model=RepoOut, tags=["repos"])
def update_repo(
    repo_id: str, body: RepoSettingsIn, who: Principal = Depends(require_admin)
) -> RepoOut:
    """Set the project's default agent and branch (empty or null clears them). Admins only:
    the defaults apply to everyone's new jobs on this project."""
    agent = (body.default_agent_id or "").strip() or None
    ref = (body.default_ref or "").strip() or None
    if agent and agent not in load_agents(agents_dir())[0]:
        raise _err(422, "unknown_agent", "Pick one of the configured agents.")
    try:
        GitSource("https://github.com/x/y", ref).validate()
    except PrepareError as e:
        raise _err(422, e.code, e.message) from e
    with session_scope() as s:
        repo = _visible_repo(s, repo_id, who)
        repo.default_agent_id, repo.default_ref = agent, ref
        s.add(repo)
        s.commit()
        return _repo_out(s, [repo], who)[0]


# ---- jobs ----------------------------------------------------------------------------------
@router.post("/jobs", response_model=JobOut, status_code=201, tags=["jobs"])
def create_job(
    body: JobIn,
    svc: JobService = Depends(jobs_service),
    who: Principal = Depends(require_auth),
) -> JobOut:
    """Queue a `profile` job, or an `agent` job (needs `agent_id` and `task`).
    Left-out `agent_id` / `ref` come from the project's defaults, if it has them.
    Poll it, or stream `/jobs/{id}/events`."""
    url = body.repo_url
    agent_id, ref = body.agent_id, body.ref
    with session_scope() as s:
        if body.repo_id:
            repo: Repo | None = _visible_repo(s, body.repo_id, who)
        else:
            assert url is not None
            repo = s.exec(select(Repo).where(Repo.url == url.strip().rstrip("/"))).first()
        if repo is not None:
            url = repo.url
            agent_id = agent_id or repo.default_agent_id
            ref = ref or repo.default_ref
    if body.kind == JobKind.AGENT:
        agents = load_agents(agents_dir())[0]
        if agent_id not in agents:
            raise _err(422, "unknown_agent", "Pick one of the configured agents.")
        if not (body.task or "").strip():
            raise _err(422, "missing_task", "Describe the task for the agent.")
        if body.interactive and agents[agent_id].protocol != "acp":
            raise _err(
                422,
                "not_conversational",
                f"{agents[agent_id].name} runs one task at a time. Pick an ACP agent (for "
                "example Claude Code (ACP)) to reply and approve while it works.",
            )
    elif body.interactive:
        raise _err(422, "not_conversational", "Only agent tasks can be conversations.")
    assert url is not None
    return _submit(
        svc,
        who,
        url,
        kind=body.kind,
        ref=ref,
        task=body.task,
        agent_id=agent_id,
        user_id=who.user_id,
        interactive=body.interactive,
        ask_permissions=body.ask_permissions,
    )


@router.get("/jobs", response_model=list[JobOut], tags=["jobs"])
def list_jobs(
    limit: int = Query(30, ge=1, le=100),
    before: str | None = Query(None, description="Return jobs older than this job id"),
    status_: JobStatus | None = Query(None, alias="status"),
    repo_id: str | None = None,
    who: Principal = Depends(require_auth),
) -> list[JobOut]:
    """Newest first. Page with `before=<last id of the previous page>`."""
    with session_scope() as s:
        q = select(Job, Repo).join(Repo, Repo.id == Job.repo_id)  # type: ignore[arg-type]
        if not who.is_admin:
            q = q.where(Job.user_id == who.user_id)
        if before:
            q = q.where(Job.id < before)
        if status_:
            q = q.where(Job.status == status_)
        if repo_id:
            q = q.where(Job.repo_id == repo_id)
        rows = s.exec(q.order_by(Job.id.desc()).limit(limit))  # type: ignore[attr-defined]
        return [JobOut.of(j, r) for j, r in rows]


@router.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"])
def get_job(job_id: str, who: Principal = Depends(require_auth)) -> JobOut:
    with session_scope() as s:
        return JobOut.of(*_job_and_repo(s, job_id, who))


@router.post("/jobs/{job_id}/cancel", response_model=JobOut, tags=["jobs"])
def cancel_job(
    job_id: str, svc: JobService = Depends(jobs_service), who: Principal = Depends(require_auth)
) -> JobOut:
    with session_scope() as s:
        _job_and_repo(s, job_id, who)  # 404 if unknown or not theirs
    if not svc.cancel(job_id):
        raise _err(status.HTTP_409_CONFLICT, "not_cancellable", "That job has already finished.")
    with session_scope() as s:
        return JobOut.of(*_job_and_repo(s, job_id, who))


@router.post("/jobs/{job_id}/retry", response_model=JobOut, status_code=201, tags=["jobs"])
def retry_job(
    job_id: str, svc: JobService = Depends(jobs_service), who: Principal = Depends(require_auth)
) -> JobOut:
    """Queue a fresh job with the same repository, kind, settings and owner."""
    with session_scope() as s:
        job, repo = _job_and_repo(s, job_id, who)
    if job.status not in TERMINAL_STATUSES:
        raise _err(status.HTTP_409_CONFLICT, "still_running", "Wait for the job to finish first.")
    if job.kind == JobKind.PR:
        return _submit_pr(svc, who, job.parent_job_id or "")
    return _submit(
        svc,
        who,
        repo.url,
        kind=job.kind,
        ref=job.ref,
        task=job.task,
        agent_id=job.agent_id,
        user_id=job.user_id,
        interactive=job.interactive,
        ask_permissions=job.ask_permissions,
    )


class InputIn(BaseModel):
    type: Literal["message", "permission", "stop", "end"]
    text: str | None = Field(None, max_length=10_000)
    request: int | None = Field(None, ge=1)  # permission: the request number shown with it
    option: str | None = Field(None, max_length=200)  # permission: chosen option id; null = deny


@router.post("/jobs/{job_id}/input", status_code=202, tags=["agents"])
async def job_input(
    job_id: str,
    body: InputIn,
    svc: JobService = Depends(jobs_service),
    who: Principal = Depends(require_auth),
) -> dict[str, bool]:
    """Talk to a running agent conversation: `message` (another instruction; queued if the
    agent is busy), `permission` (answer a request), `stop` (stop the current step), `end`.
    Only the person whose job it is may answer: it runs with their login."""
    with session_scope() as s:
        job, _ = _job_and_repo(s, job_id, who)
    if job.user_id != who.user_id:
        raise _err(403, "not_your_job", "Only the person who started this job can answer it.")
    if not job.interactive:
        raise _err(409, "not_conversational", "This job runs one task; it doesn't take replies.")
    if job.status in TERMINAL_STATUSES:
        raise _err(409, "finished", "This conversation has ended. Start a new task to continue.")
    command: dict[str, Any] = {"type": body.type}
    if body.type == "message":
        text = (body.text or "").strip()
        if not text:
            raise _err(422, "missing_text", "Type a message first.")
        command["text"] = text
    elif body.type == "permission":
        if body.request is None:
            raise _err(422, "missing_request", "Which request are you answering?")
        command.update(request=body.request, option=body.option)
    if not await svc.send_input(job_id, command):
        raise _err(
            409,
            "not_ready",
            "The agent isn't ready for messages yet (it may still be starting). Try again soon.",
        )
    return {"delivered": True}


@router.get("/jobs/{job_id}/events", tags=["jobs"], response_class=StreamingResponse)
async def job_events(
    job_id: str,
    request: Request,
    after: int | None = Query(None, ge=0, description="Resume after this event seq"),
    last_event_id: str | None = Header(None),
    who: Principal = Depends(require_auth),
) -> StreamingResponse:
    """Server-Sent Events: replays stored events, then follows the job live until it finishes.

    Browsers reconnect automatically and send `Last-Event-ID`, so no event is missed or repeated.
    """
    with session_scope() as s:
        _job_and_repo(s, job_id, who)
    start = after
    if start is None:
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    return StreamingResponse(
        jobs_service(request).events.stream(job_id, start),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- profiles ------------------------------------------------------------------------------
def _report(job_id: str, who: Principal) -> ProfileReport:
    with session_scope() as s:
        _job_and_repo(s, job_id, who)
        report = s.get(ProfileReport, job_id)
    if report is None:
        raise _err(status.HTTP_404_NOT_FOUND, "no_profile", "This job has no profile report (yet).")
    return report


@router.get("/jobs/{job_id}/profile", tags=["profiles"])
def get_profile(job_id: str, who: Principal = Depends(require_auth)) -> dict[str, Any]:
    """The structured report: minimum and recommended config plus every run."""
    r = _report(job_id, who)
    return {
        "job_id": r.job_id,
        "min_config": r.min_config,
        "recommended": r.recommended,
        "report": r.report,
    }


@router.get("/jobs/{job_id}/profile.md", tags=["profiles"], response_class=PlainTextResponse)
def get_profile_markdown(job_id: str, who: Principal = Depends(require_auth)) -> PlainTextResponse:
    return PlainTextResponse(
        _report(job_id, who).report_md, media_type="text/markdown; charset=utf-8"
    )


# ---- agent results -------------------------------------------------------------------------
@router.get("/jobs/{job_id}/result", tags=["agents"])
def get_result(job_id: str, who: Principal = Depends(require_auth)) -> dict[str, Any]:
    """What an agent job produced: summary, diff and diff statistics."""
    with session_scope() as s:
        _job_and_repo(s, job_id, who)
        # Named columns only: the patch (up to 5 MB) is not loaded just to say whether it exists.
        r = s.exec(
            select(
                JobResult.job_id,
                JobResult.exit_status,
                JobResult.summary,
                JobResult.diff_text,
                JobResult.diff_stat,
                JobResult.patch.is_not(None),  # type: ignore[union-attr]
                JobResult.pr_url,
            ).where(JobResult.job_id == job_id)
        ).first()
    if r is None:
        raise _err(status.HTTP_404_NOT_FOUND, "no_result", "This job has no agent result (yet).")
    return {
        "job_id": r[0],
        "exit_status": r[1],
        "summary": r[2],
        "diff": r[3],
        "diff_stat": r[4],
        "has_patch": bool(r[5]),
        "pr_url": r[6],
        "pr_job": _latest_pr_job(job_id),
    }


def _latest_pr_job(agent_job_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        job = s.exec(
            select(Job)
            .where(Job.parent_job_id == agent_job_id, Job.kind == JobKind.PR)
            .order_by(Job.id.desc())  # type: ignore[attr-defined]
        ).first()
    if job is None:
        return None
    return {
        "id": job.id,
        "status": job.status,
        "finished": job.status in TERMINAL_STATUSES,
        "error_message": job.error_message,
    }


def _submit_pr(svc: JobService, who: Principal, agent_job_id: str) -> JobOut:
    with session_scope() as s:
        _job_and_repo(s, agent_job_id, who)
    try:
        job = svc.submit_pr(agent_job_id)
    except PRError as e:
        code = {
            "job_not_found": status.HTTP_404_NOT_FOUND,
            "pr_exists": status.HTTP_409_CONFLICT,
            "pr_in_progress": status.HTTP_409_CONFLICT,
            "github_not_allowed": status.HTTP_403_FORBIDDEN,
            "too_many_jobs": status.HTTP_429_TOO_MANY_REQUESTS,
        }.get(e.code, 422)
        raise _err(code, e.code, e.message) from e
    with session_scope() as s:
        return JobOut.of(*_job_and_repo(s, job.id, who))


@router.post("/jobs/{job_id}/pr", response_model=JobOut, status_code=201, tags=["agents"])
def create_pr(
    job_id: str, svc: JobService = Depends(jobs_service), who: Principal = Depends(require_auth)
) -> JobOut:
    """Open a pull request from a successful agent job's change. Queues a `pr` job; its result
    (and this job's `/result`) then carries `pr_url`. Needs THINAIR_GITHUB_TOKEN with
    Contents and Pull requests: Read and write."""
    return _submit_pr(svc, who, job_id)


@router.get("/jobs/{job_id}/patch", tags=["agents"])
def get_patch(job_id: str, who: Principal = Depends(require_auth)) -> Response:
    """The agent's exact change as a binary git patch (`git apply` it yourself)."""
    with session_scope() as s:
        _job_and_repo(s, job_id, who)
        r = s.get(JobResult, job_id)
    if r is None or not r.patch:
        raise _err(status.HTTP_404_NOT_FOUND, "no_patch", "This job has no patch.")
    return Response(
        r.patch,
        media_type="text/x-patch",
        headers={"Content-Disposition": f'attachment; filename="thinair-{job_id}.patch"'},
    )


# ---- agents --------------------------------------------------------------------------------
@router.get("/agents", tags=["agents"])
def list_agents() -> list[dict[str, Any]]:
    """Configured coding agents (built-in plus `$THINAIR_DATA_DIR/agents/*.yaml`)."""
    agents, _errors = load_agents(agents_dir())
    return [
        {
            "id": a.id,
            "name": a.name,
            "version": a.version,
            "login_hint": a.auth_hint,
            # ACP agents can hold a conversation (replies and approvals while they work).
            "conversational": a.protocol == "acp",
        }
        for a in agents.values()
    ]


# ---- users (admins only) -------------------------------------------------------------------
class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str | None
    name: str
    is_admin: bool
    created_at: datetime | None = None
    disabled_at: datetime | None = None
    max_active_jobs: int | None = None  # None = server default (0 = no limit)


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    is_admin: bool = False


class TokenIn(BaseModel):
    name: str = Field("api", min_length=1, max_length=64)


def _user_err(e: UserError) -> HTTPException:
    code = {"user_not_found": 404, "user_exists": 409}.get(e.code, 422)
    return _err(code, e.code, e.message)


@router.get("/users", response_model=list[UserOut], tags=["users"])
def users_list(_admin: Principal = Depends(require_admin)) -> list[Any]:
    """Everyone who can sign in. `admin` (id null) is the owner from THINAIR_ADMIN_TOKEN."""
    with session_scope() as s:
        return [UserOut(id=None, name=OWNER_NAME, is_admin=True), *list_users(s)]


@router.post("/users", status_code=201, tags=["users"])
def users_create(body: UserIn, _admin: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Add a user and return their first API token. It is shown only once: pass it on safely."""
    with session_scope() as s:
        try:
            user = create_user(s, body.name, admin=body.is_admin)
        except UserError as e:
            raise _user_err(e) from e
        token = create_token(s, "first", user_id=user.id)
        return {"user": UserOut.model_validate(user).model_dump(mode="json"), "token": token}


@router.post("/users/{name}/tokens", status_code=201, tags=["users"])
def users_new_token(
    name: str, body: TokenIn, _admin: Principal = Depends(require_admin)
) -> dict[str, str]:
    """Another token for a user (e.g. a second device). Shown only once."""
    with session_scope() as s:
        user = get_user(s, name)
        if user is None or user.disabled_at is not None:
            raise _err(404, "user_not_found", f"No active user called {name!r}.")
        return {"token": create_token(s, body.name, user_id=user.id)}


class LimitIn(BaseModel):
    max_active_jobs: int | None = Field(None, ge=0, le=10_000)


@router.post("/users/{name}/limit", response_model=UserOut, tags=["users"])
def users_limit(name: str, body: LimitIn, _admin: Principal = Depends(require_admin)) -> User:
    """Unfinished jobs this member may have at once: a number (0 = no limit), or null for the
    server default (THINAIR_MEMBER_JOB_LIMIT). Admins are never limited."""
    with session_scope() as s:
        try:
            return set_job_limit(s, name, body.max_active_jobs)
        except UserError as e:
            raise _err(404 if e.code == "user_not_found" else 422, e.code, e.message) from e


@router.post("/users/{name}/disable", response_model=UserOut, tags=["users"])
def users_disable(name: str, _admin: Principal = Depends(require_admin)) -> User:
    """Disable a user and revoke their tokens. Their jobs and results stay."""
    with session_scope() as s:
        try:
            return disable_user(s, name)
        except UserError as e:
            raise _user_err(e) from e
