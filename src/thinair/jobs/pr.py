"""Open a pull request from a finished agent job (the `pr` job kind).

The agent's workspace is deleted when its job ends, so the agent job keeps an exact binary patch
plus the commit it was made against (`JobResult.patch`, `Job.commit_sha`). A `pr` job:

1. fetches exactly that commit into a fresh workspace volume (clone sandbox, token in env only),
2. applies the patch and commits it as the configured author (no extra trailers),
3. force-pushes branch `thinair/<agent-job-id>` (a branch only Thinair writes, so retries are safe),
4. opens the pull request through GitHub's REST API from Thinair's own process.

Repo code never runs here: hooks are disabled and no install or test step exists.
"""

import asyncio
import contextlib
import io
import tarfile

from thinair.config import get_settings
from thinair.db.models import Job, JobKind, JobResult, JobStatus, Repo
from thinair.db.session import session_scope
from thinair.github import (
    GitHubAPIError,
    GitHubClient,
    GitHubTokenError,
    git_auth_env,
    github_token,
    repo_slug,
)
from thinair.jobs.service import ExecResult, JobContext
from thinair.project.prepare import GIT_IMAGE, SANDBOX_UID, ensure_image, workspace_volume
from thinair.sandbox.manager import SandboxManager
from thinair.sandbox.spec import SandboxSpec, VolumeMount

PATCH_IN_VOLUME = ".git/thinair-pr.patch"
_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
_SAFE += ["-c", "protocol.file.allow=never"]


class PRError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def pr_branch(agent_job_id: str) -> str:
    return f"thinair/{agent_job_id}"


def check_pr_possible(job: Job | None, result: JobResult | None) -> None:
    """Raise PRError (with a phone-readable message) unless this agent job can become a PR."""
    if job is None:
        raise PRError("job_not_found", "No job with that id.")
    if job.kind != JobKind.AGENT:
        raise PRError("not_agent_job", "Only agent tasks can be turned into a pull request.")
    if job.status != JobStatus.SUCCEEDED:
        raise PRError("agent_not_succeeded", "Only a finished, successful agent task can.")
    if result is None or not result.patch:
        if result is not None and (result.diff_stat or {}).get("patch_too_large"):
            raise PRError(
                "patch_too_large", "The change is too large to open as a PR from Thinair."
            )
        raise PRError("no_changes", "The agent did not change any files.")
    if not job.commit_sha:
        raise PRError("no_base_commit", "This job has no recorded base commit.")


def require_token() -> str:
    try:
        token = github_token()
    except GitHubTokenError as e:
        raise PRError("bad_github_token", str(e)) from None
    if not token:
        raise PRError(
            "pr_needs_token",
            "Set THINAIR_GITHUB_TOKEN to open pull requests (Contents and Pull requests: "
            "Read and write).",
        )
    return token


def pr_title(task: str | None) -> str:
    line = next((ln.strip() for ln in (task or "").splitlines() if ln.strip()), "")
    line = " ".join(line.split()) or "Changes from Thinair"
    return line if len(line) <= 72 else line[:71].rstrip() + "…"


def pr_body(task: str | None, summary: str | None, agent_job_id: str) -> str:
    parts = ["## Task", (task or "").strip()[:4000]]
    if summary and summary.strip():
        parts += ["## Summary", summary.strip()[:8000]]
    parts += [f"Opened from Thinair job `{agent_job_id}`."]
    return "\n\n".join(parts)


def _patch_tar(patch: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        ti = tarfile.TarInfo(PATCH_IN_VOLUME)
        ti.size, ti.mode = len(patch), 0o644
        ti.uid = ti.gid = SANDBOX_UID
        tar.addfile(ti, io.BytesIO(patch))
    return buf.getvalue()


async def _git(
    mgr: SandboxManager,
    job_id: str,
    volume: str,
    args: list[str],
    log,
    *,
    token: str | None = None,
    timeout_s: float = 120,
) -> tuple[int, list[str]]:
    env = {"HOME": "/tmp", "GIT_TERMINAL_PROMPT": "0"}  # noqa: S108
    redact: list[str] = []
    if token:
        auth, redact = git_auth_env(token)
        env.update(auth)
    lines: list[str] = []
    res = await mgr.run(
        SandboxSpec(
            image=GIT_IMAGE,
            command=[*_SAFE, *args],
            job_id=job_id,
            mounts=[VolumeMount(volume=volume, path="/workspace")],
            env=env,
            redact=redact,
            network=token is not None,  # only fetch and push talk to GitHub
            cpus=1,
            memory_bytes=512 * 1024**2,
            timeout_s=timeout_s,
        ),
        lambda ln: (lines.append(ln), log(ln)),
    )
    return (res.exit_code if res.exit_code is not None else -1), lines


async def build_commit(
    mgr: SandboxManager,
    job_id: str,
    volume: str,
    *,
    url: str | None,
    sha: str,
    patch: bytes,
    message: tuple[str, str],
    log,
    token: str | None,
) -> None:
    """Fetch `sha` (unless `url` is None: the volume already holds it), apply, commit."""
    s = get_settings()
    if url is not None:
        for args in (["init", "-q"], ["remote", "add", "origin", url]):
            await _git(mgr, job_id, volume, args, log)
        log(f"[thinair] fetching {url} at {sha[:12]}")
        code, out = await _git(
            mgr,
            job_id,
            volume,
            ["fetch", "-q", "--depth", "1", "--no-tags", "origin", sha],
            log,
            token=token,
            timeout_s=300,
        )
        if code != 0:
            raise PRError(
                "fetch_failed",
                "Could not fetch the commit the agent worked on. Check that the token can "
                "read this repository. Last output: " + " | ".join(out[-3:]),
            )
        await _git(mgr, job_id, volume, ["checkout", "-q", "--detach", "FETCH_HEAD"], log)
    await asyncio.to_thread(mgr.put_tar, volume, GIT_IMAGE, _patch_tar(patch), job_id)
    code, out = await _git(
        mgr, job_id, volume, ["apply", "--index", "--whitespace=nowarn", PATCH_IN_VOLUME], log
    )
    if code != 0:
        raise PRError(
            "patch_failed",
            "The agent's change no longer applies to the commit it was made on. "
            "Last output: " + " | ".join(out[-3:]),
        )
    ident = ["-c", f"user.name={s.git_author_name}", "-c", f"user.email={s.git_author_email}"]
    code, out = await _git(
        mgr, job_id, volume, [*ident, "commit", "-q", "-m", message[0], "-m", message[1]], log
    )
    if code != 0:
        raise PRError("commit_failed", "Could not commit the change: " + " | ".join(out[-3:]))


async def push_branch(
    mgr: SandboxManager, job_id: str, volume: str, branch: str, token: str, log
) -> None:
    log(f"[thinair] pushing branch {branch}")
    code, out = await _git(
        mgr,
        job_id,
        volume,
        ["push", "-q", "--force", "origin", f"HEAD:refs/heads/{branch}"],
        log,
        token=token,
        timeout_s=300,
    )
    if code != 0:
        raise PRError(
            "push_failed",
            'GitHub refused the push. The token needs "Contents: Read and write" on this '
            "repository (you can only open PRs on repos you can push to). Last output: "
            + " | ".join(out[-3:]),
        )


async def pr_executor(ctx: JobContext, mgr: SandboxManager | None) -> ExecResult:
    assert mgr is not None
    job = ctx.job
    try:
        token = require_token()
        with session_scope() as s:
            parent = s.get(Job, job.parent_job_id) if job.parent_job_id else None
            result = s.get(JobResult, parent.id) if parent else None
            repo = s.get(Repo, parent.repo_id) if parent else None
        check_pr_possible(parent, result)
        assert parent is not None and result is not None and repo is not None
        assert result.patch is not None and parent.commit_sha is not None
        owner, name = repo_slug(repo.url)
        branch = pr_branch(parent.id)
        title = pr_title(parent.task)
        body = pr_body(parent.task, result.summary, parent.id)

        volume = workspace_volume(job.id)
        await ensure_image(mgr, "git", ctx.log)
        await asyncio.to_thread(mgr.create_volume, volume, job.id)
        try:
            await build_commit(
                mgr,
                job.id,
                volume,
                url=repo.url,
                sha=parent.commit_sha,
                patch=result.patch,
                message=(title, f"Opened from Thinair job {parent.id}."),
                log=ctx.log,
                token=token,
            )
            await push_branch(mgr, job.id, volume, branch, token, ctx.log)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.shield(asyncio.to_thread(mgr.remove_volume, volume))

        gh = GitHubClient(token)
        base = parent.ref or await asyncio.to_thread(gh.default_branch, owner, name)
        ctx.log(f"[thinair] opening a pull request: {branch} -> {base}")
        url = await asyncio.to_thread(
            gh.create_pull, owner, name, title=title, body=body, head=branch, base=base
        )
    except PRError as e:
        return ExecResult(JobStatus.FAILED, e.code, e.message)
    except GitHubAPIError as e:
        return ExecResult(JobStatus.FAILED, "github_api_error", e.message)

    ctx.log(f"[thinair] pull request: {url}")
    with session_scope() as s:
        row = s.get(JobResult, parent.id)
        if row is not None:
            row.pr_url = url
            s.add(row)
        s.add(JobResult(job_id=job.id, summary=url, pr_url=url))
        s.commit()
    return ExecResult(JobStatus.SUCCEEDED)
