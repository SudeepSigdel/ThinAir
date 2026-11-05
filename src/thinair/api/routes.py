"""REST + SSE routes. Every route requires auth (router-level dependency).

Errors use one shape: `{"detail": {"code": "...", "message": "..."}}`, where `message` is safe
and readable to show on a phone.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlmodel import Session, select

from thinair.agents.config import load_agents
from thinair.api.auth import require_auth
from thinair.db.models import Job, JobKind, JobResult, JobStatus, ProfileReport, Repo
from thinair.db.session import session_scope
from thinair.jobs.events import TERMINAL_STATUSES
from thinair.jobs.executors import agents_dir
from thinair.jobs.pr import PRError
from thinair.jobs.service import JobService
from thinair.project.prepare import GitSource, PrepareError

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


def _err(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(code, detail={"code": error, "message": message})


def jobs_service(request: Request) -> JobService:
    return request.app.state.jobs


# ---- schemas -------------------------------------------------------------------------------
class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    url: str
    name: str
    created_at: datetime


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
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
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

    @model_validator(mode="after")
    def _one_repo(self) -> "JobIn":
        if bool(self.repo_url) == bool(self.repo_id):
            raise ValueError("provide exactly one of repo_url or repo_id")
        return self


# ---- helpers -------------------------------------------------------------------------------
def _job_and_repo(s: Session, job_id: str) -> tuple[Job, Repo]:
    job = s.get(Job, job_id)
    repo = s.get(Repo, job.repo_id) if job else None
    if job is None or repo is None:
        raise _err(status.HTTP_404_NOT_FOUND, "job_not_found", "No job with that id.")
    return job, repo


def _submit(svc: JobService, url: str, body: JobIn | None = None, **kw: Any) -> JobOut:
    try:
        job = svc.submit(url, **kw)
    except PrepareError as e:
        raise _err(422, e.code, e.message) from e
    with session_scope() as s:
        return JobOut.of(*_job_and_repo(s, job.id))


# ---- repos ---------------------------------------------------------------------------------
@router.get("/repos", response_model=list[RepoOut], tags=["repos"])
def list_repos() -> list[Repo]:
    with session_scope() as s:
        return list(s.exec(select(Repo).order_by(Repo.id.desc())))  # type: ignore[attr-defined]


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
def get_repo(repo_id: str) -> Repo:
    with session_scope() as s:
        repo = s.get(Repo, repo_id)
    if repo is None:
        raise _err(status.HTTP_404_NOT_FOUND, "repo_not_found", "No repository with that id.")
    return repo


# ---- jobs ----------------------------------------------------------------------------------
@router.post("/jobs", response_model=JobOut, status_code=201, tags=["jobs"])
def create_job(body: JobIn, svc: JobService = Depends(jobs_service)) -> JobOut:
    """Queue a `profile` job, or an `agent` job (needs `agent_id` and `task`).
    Poll it, or stream `/jobs/{id}/events`."""
    if body.kind == JobKind.AGENT:
        if body.agent_id not in load_agents(agents_dir())[0]:
            raise _err(422, "unknown_agent", "Pick one of the configured agents.")
        if not (body.task or "").strip():
            raise _err(422, "missing_task", "Describe the task for the agent.")
    url = body.repo_url
    if body.repo_id:
        with session_scope() as s:
            repo = s.get(Repo, body.repo_id)
        if repo is None:
            raise _err(status.HTTP_404_NOT_FOUND, "repo_not_found", "No repository with that id.")
        url = repo.url
    assert url is not None
    return _submit(svc, url, kind=body.kind, ref=body.ref, task=body.task, agent_id=body.agent_id)


@router.get("/jobs", response_model=list[JobOut], tags=["jobs"])
def list_jobs(
    limit: int = Query(30, ge=1, le=100),
    before: str | None = Query(None, description="Return jobs older than this job id"),
    status_: JobStatus | None = Query(None, alias="status"),
    repo_id: str | None = None,
) -> list[JobOut]:
    """Newest first. Page with `before=<last id of the previous page>`."""
    with session_scope() as s:
        q = select(Job, Repo).join(Repo, Repo.id == Job.repo_id)  # type: ignore[arg-type]
        if before:
            q = q.where(Job.id < before)
        if status_:
            q = q.where(Job.status == status_)
        if repo_id:
            q = q.where(Job.repo_id == repo_id)
        rows = s.exec(q.order_by(Job.id.desc()).limit(limit))  # type: ignore[attr-defined]
        return [JobOut.of(j, r) for j, r in rows]


@router.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"])
def get_job(job_id: str) -> JobOut:
    with session_scope() as s:
        return JobOut.of(*_job_and_repo(s, job_id))


@router.post("/jobs/{job_id}/cancel", response_model=JobOut, tags=["jobs"])
def cancel_job(job_id: str, svc: JobService = Depends(jobs_service)) -> JobOut:
    with session_scope() as s:
        _job_and_repo(s, job_id)  # 404 if unknown
    if not svc.cancel(job_id):
        raise _err(status.HTTP_409_CONFLICT, "not_cancellable", "That job has already finished.")
    with session_scope() as s:
        return JobOut.of(*_job_and_repo(s, job_id))


@router.post("/jobs/{job_id}/retry", response_model=JobOut, status_code=201, tags=["jobs"])
def retry_job(job_id: str, svc: JobService = Depends(jobs_service)) -> JobOut:
    """Queue a fresh job with the same repository, kind and settings."""
    with session_scope() as s:
        job, repo = _job_and_repo(s, job_id)
    if job.status not in TERMINAL_STATUSES:
        raise _err(status.HTTP_409_CONFLICT, "still_running", "Wait for the job to finish first.")
    if job.kind == JobKind.PR:
        return _submit_pr(svc, job.parent_job_id or "")
    return _submit(svc, repo.url, kind=job.kind, ref=job.ref, task=job.task, agent_id=job.agent_id)


@router.get("/jobs/{job_id}/events", tags=["jobs"], response_class=StreamingResponse)
async def job_events(
    job_id: str,
    request: Request,
    after: int | None = Query(None, ge=0, description="Resume after this event seq"),
    last_event_id: str | None = Header(None),
) -> StreamingResponse:
    """Server-Sent Events: replays stored events, then follows the job live until it finishes.

    Browsers reconnect automatically and send `Last-Event-ID`, so no event is missed or repeated.
    """
    with session_scope() as s:
        _job_and_repo(s, job_id)
    start = after
    if start is None:
        start = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    return StreamingResponse(
        jobs_service(request).events.stream(job_id, start),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- profiles ------------------------------------------------------------------------------
def _report(job_id: str) -> ProfileReport:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise _err(status.HTTP_404_NOT_FOUND, "job_not_found", "No job with that id.")
        report = s.get(ProfileReport, job_id)
    if report is None:
        raise _err(status.HTTP_404_NOT_FOUND, "no_profile", "This job has no profile report (yet).")
    return report


@router.get("/jobs/{job_id}/profile", tags=["profiles"])
def get_profile(job_id: str) -> dict[str, Any]:
    """The structured report: minimum and recommended config plus every run."""
    r = _report(job_id)
    return {
        "job_id": r.job_id,
        "min_config": r.min_config,
        "recommended": r.recommended,
        "report": r.report,
    }


@router.get("/jobs/{job_id}/profile.md", tags=["profiles"], response_class=PlainTextResponse)
def get_profile_markdown(job_id: str) -> PlainTextResponse:
    return PlainTextResponse(_report(job_id).report_md, media_type="text/markdown; charset=utf-8")


# ---- agent results -------------------------------------------------------------------------
@router.get("/jobs/{job_id}/result", tags=["agents"])
def get_result(job_id: str) -> dict[str, Any]:
    """What an agent job produced: summary, diff and diff statistics."""
    with session_scope() as s:
        _job_and_repo(s, job_id)
        r = s.get(JobResult, job_id)
    if r is None:
        raise _err(status.HTTP_404_NOT_FOUND, "no_result", "This job has no agent result (yet).")
    return {
        "job_id": r.job_id,
        "exit_status": r.exit_status,
        "summary": r.summary,
        "diff": r.diff_text,
        "diff_stat": r.diff_stat,
        "has_patch": r.patch is not None,
        "pr_url": r.pr_url,
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


def _submit_pr(svc: JobService, agent_job_id: str) -> JobOut:
    try:
        job = svc.submit_pr(agent_job_id)
    except PRError as e:
        code = {
            "job_not_found": status.HTTP_404_NOT_FOUND,
            "pr_exists": status.HTTP_409_CONFLICT,
            "pr_in_progress": status.HTTP_409_CONFLICT,
        }.get(e.code, 422)
        raise _err(code, e.code, e.message) from e
    with session_scope() as s:
        return JobOut.of(*_job_and_repo(s, job.id))


@router.post("/jobs/{job_id}/pr", response_model=JobOut, status_code=201, tags=["agents"])
def create_pr(job_id: str, svc: JobService = Depends(jobs_service)) -> JobOut:
    """Open a pull request from a successful agent job's change. Queues a `pr` job; its result
    (and this job's `/result`) then carries `pr_url`. Needs THINAIR_GITHUB_TOKEN with
    Contents and Pull requests: Read and write."""
    return _submit_pr(svc, job_id)


@router.get("/jobs/{job_id}/patch", tags=["agents"])
def get_patch(job_id: str) -> Response:
    """The agent's exact change as a binary git patch (`git apply` it yourself)."""
    with session_scope() as s:
        _job_and_repo(s, job_id)
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
        }
        for a in agents.values()
    ]
