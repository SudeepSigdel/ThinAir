"""Real job executors. Kept thin: they only translate between the job system and the
profiler/agent code, which have their own tests."""

import asyncio
import contextlib

from sqlmodel import Session

from thinair.agents.cli_adapter import AgentAuthError, CLIAdapter
from thinair.agents.config import load_agents
from thinair.config import get_settings
from thinair.db.models import (
    EventType,
    Job,
    JobKind,
    JobResult,
    JobStatus,
    ProfileReport,
    ProfileRun,
)
from thinair.db.session import session_scope
from thinair.jobs.service import ExecResult, Executor, JobContext
from thinair.profiler.models import ProfileResult
from thinair.profiler.report import to_markdown
from thinair.profiler.runner import profile_source
from thinair.project.prepare import GitSource, prepare_workspace
from thinair.sandbox.manager import SandboxManager
from thinair.users import auth_volume_name, github_token_allowed, job_user


def _owner(job: Job):  # noqa: ANN202
    with session_scope() as s:
        user = job_user(s, job)
        if user is not None:
            s.expunge(user)
        return user


def owner_github_auth(job: Job) -> bool:
    """May this job use a GitHub token (the server's, or a remote runner's own)?"""
    return github_token_allowed(_owner(job))


async def profile_executor(ctx: JobContext, mgr: SandboxManager | None) -> ExecResult:
    assert mgr is not None
    result, prepared = await profile_source(
        mgr,
        GitSource(ctx.repo.url, ctx.job.ref),
        ctx.job.id,
        log=ctx.log,
        on_run=lambda rec: ctx.emit(EventType.PROFILE_RUN, rec.model_dump(mode="json")),
        github_auth=owner_github_auth(ctx.job),
    )
    return save_profile_result(
        ctx.job.id, ctx.repo.name, result, prepared.commit_sha, prepared.resolved.stack
    )


def save_profile_result(
    job_id: str, repo_name: str, result: ProfileResult, commit_sha: str | None, stack: str | None
) -> ExecResult:
    """Store a finished profile (also used for results reported by remote runners)."""
    markdown = to_markdown(result, project=repo_name, commit=commit_sha, stack=stack)
    with session_scope() as s:
        _save_profile(s, job_id, result, markdown)
        job = s.get(Job, job_id)
        if job is not None:
            job.commit_sha = commit_sha
            s.add(job)
            s.commit()
    if result.status == "baseline_failed":
        return ExecResult(
            JobStatus.FAILED, "baseline_failed", result.notes[0] if result.notes else None
        )
    return ExecResult(JobStatus.SUCCEEDED)


def _save_profile(s: Session, job_id: str, result: ProfileResult, markdown: str) -> None:
    for rec in result.runs:
        s.add(
            ProfileRun(
                job_id=job_id,
                cpus=rec.cpus,
                mem_bytes=rec.mem_bytes,
                outcome=rec.outcome,
                exit_code=rec.exit_code,
                oom_killed=rec.oom_killed,
                peak_mem_bytes=rec.peak_mem_bytes,
                cpu_time_s=rec.cpu_time_s,
                wall_s=rec.wall_s,
                attempt=rec.attempt,
            )
        )
    s.add(
        ProfileReport(
            job_id=job_id,
            min_config=result.min_config.model_dump() if result.min_config else None,
            recommended=result.recommended.model_dump() if result.recommended else None,
            report=result.model_dump(mode="json"),
            report_md=markdown,
        )
    )
    s.commit()


def agents_dir():
    return get_settings().data_dir / "agents"


async def agent_executor(ctx: JobContext, mgr: SandboxManager | None) -> ExecResult:
    """Clone + install the project, run the chosen agent CLI on the task, save summary + diff."""
    assert mgr is not None
    job = ctx.job
    cfg = load_agents(agents_dir())[0].get(job.agent_id or "")
    if cfg is None:
        return ExecResult(
            JobStatus.FAILED, "unknown_agent", f"No agent called {job.agent_id!r} is configured."
        )
    if not (job.task or "").strip():
        return ExecResult(JobStatus.FAILED, "missing_task", "Describe the task for the agent.")

    owner = _owner(job)
    prepared = await prepare_workspace(
        mgr,
        GitSource(ctx.repo.url, job.ref),
        job.id,
        ctx.log,
        github_auth=github_token_allowed(owner),
    )
    try:
        # Each user logs in to the agent in their own volume; API keys from Thinair's own
        # environment are the owner's, so only the owner's jobs get them.
        adapter = CLIAdapter(
            cfg,
            mgr,
            job.id,
            prepared,
            host_env=None if owner is None else {},
            auth_volume=auth_volume_name(cfg.login_id, owner),
        )
        try:
            await adapter.prepare(ctx.log)
        except AgentAuthError as e:
            return ExecResult(JobStatus.FAILED, e.code, e.message)
        ctx.log(f"[thinair] {cfg.name} is working on the task")
        async for ev in adapter.run(job.task or ""):
            if ev.kind == "log":
                ctx.log(ev.text)
            else:
                payload = {"kind": ev.kind, "text": ev.text}
                if ev.data:
                    payload["data"] = ev.data
                ctx.emit(EventType.AGENT_MSG, payload)
        r = adapter.result()
        with session_scope() as s:
            s.add(
                JobResult(
                    job_id=job.id,
                    exit_status=r.exit_status,
                    summary=r.summary,
                    diff_text=r.diff,
                    diff_stat={
                        **r.diff_stat,
                        "truncated": r.diff_truncated,
                        "patch_too_large": r.patch_too_large,
                        "cost_usd": r.cost_usd,
                    },
                    patch=r.patch,
                )
            )
            row = s.get(Job, job.id)
            if row is not None:
                row.commit_sha = prepared.commit_sha
                s.add(row)
            s.commit()
        if r.success:
            return ExecResult(JobStatus.SUCCEEDED)
        return ExecResult(JobStatus.FAILED, r.error_code, r.error_message)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.shield(asyncio.to_thread(mgr.remove_volume, prepared.volume))


def default_executors() -> dict[JobKind, Executor]:
    from thinair.jobs.pr import pr_executor

    return {
        JobKind.PROFILE: profile_executor,
        JobKind.AGENT: agent_executor,
        JobKind.PR: pr_executor,
    }
