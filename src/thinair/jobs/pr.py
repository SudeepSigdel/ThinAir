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
import re
import tarfile
from dataclasses import dataclass

from thinair.db.models import Job, JobKind, JobResult, JobStatus, Repo
from thinair.db.session import session_scope
from thinair.github import (
    Credential,
    GitHubAccess,
    GitHubAPIError,
    GitHubClient,
    GitHubTokenError,
    commit_identity,
    git_auth_env,
    repo_slug,
)
from thinair.jobs.service import ExecResult, JobContext
from thinair.project.prepare import GIT_IMAGE, SANDBOX_UID, ensure_image, workspace_volume
from thinair.sandbox.manager import SandboxManager
from thinair.sandbox.spec import SandboxSpec, VolumeMount
from thinair.users import github_allowed, github_token_allowed, job_user

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


NEEDS_GITHUB = (
    "Connect GitHub first (Connect GitHub in the app), or set THINAIR_GITHUB_TOKEN "
    "(Contents and Pull requests: Read and write), to open pull requests."
)


def require_credentials() -> None:
    """Fail fast on the phone (no network): is any GitHub access configured at all? Whether it
    covers this repository is only known when a token is minted, in the job."""
    try:
        ok = GitHubAccess().can_open_pr()
    except GitHubTokenError as e:
        raise PRError("bad_github_token", str(e)) from None
    if not ok:
        raise PRError("pr_needs_token", NEEDS_GITHUB)


async def _credential(access: GitHubAccess, url: str, need: str) -> Credential:
    try:
        cred = await access.token_for(url, need)
    except GitHubTokenError as e:
        raise PRError("bad_github_token", str(e)) from None
    if cred is None:
        if access.note == "not_installed":
            raise PRError(
                "pr_needs_token",
                "Thinair's GitHub App is not installed on this repository. Add it under "
                "Connect GitHub > Change repositories.",
            )
        extra = f" ({access.note})" if access.note else ""
        raise PRError("pr_needs_token", NEEDS_GITHUB + extra)
    return cred


@dataclass(frozen=True)
class FileChange:
    path: str
    kind: str  # added | modified | deleted | renamed
    added: int = 0
    removed: int = 0
    binary: bool = False
    old_path: str | None = None


_GIT_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")
_VERBS = {"added": "Add", "modified": "Update", "deleted": "Delete", "renamed": "Rename"}
_PAST = {"added": "Added", "modified": "Updated", "deleted": "Deleted", "renamed": "Renamed"}
MAX_LISTED = 25


def parse_changes(patch: bytes | str | None) -> list[FileChange]:
    """What the patch does, per file. Counting is hunk-aware: header lines (`---`/`+++`) come
    before the first `@@`, so a removed line that starts with `-- ` is never mistaken for one."""
    if not patch:
        return []
    text = patch.decode("utf-8", "replace") if isinstance(patch, bytes) else patch
    out: list[FileChange] = []
    cur: dict | None = None

    def close() -> None:
        if cur is not None:
            out.append(FileChange(**cur))

    in_hunk = False
    for line in text.split("\n"):
        m = _GIT_HEADER.match(line) if not in_hunk or line.startswith("diff --git ") else None
        if m:
            close()
            cur = {"path": m.group(2), "kind": "modified", "old_path": None}
            cur.update(added=0, removed=0, binary=False)
            in_hunk = False
        elif cur is None:
            continue
        elif in_hunk:
            if line.startswith("+"):
                cur["added"] += 1
            elif line.startswith("-"):
                cur["removed"] += 1
        elif line.startswith("@@"):
            in_hunk = True
        elif line.startswith("new file mode"):
            cur["kind"] = "added"
        elif line.startswith("deleted file mode"):
            cur["kind"] = "deleted"
        elif line.startswith("rename from "):
            cur["kind"], cur["old_path"] = "renamed", line[len("rename from ") :]
        elif line.startswith("rename to "):
            cur["path"] = line[len("rename to ") :]
        elif line.startswith(("Binary files ", "GIT binary patch")):
            cur["binary"] = True
    close()
    return out


def _common_dir(paths: list[str]) -> str:
    parts = [p.split("/")[:-1] for p in paths]
    shared: list[str] = []
    for level in zip(*parts, strict=False):
        if len(set(level)) != 1:
            break
        shared.append(level[0])
    return "/".join(shared)


def pr_title(patch: bytes | str | None) -> str:
    """Describes the change itself (what files, what kind), not the prompt that asked for it."""
    files = parse_changes(patch)
    if not files:
        return "Changes from Thinair"
    kinds = {f.kind for f in files}
    verb = _VERBS[kinds.pop()] if len(kinds) == 1 else "Change"
    names = [f.path.rsplit("/", 1)[-1] for f in files]
    if len(files) == 1 and files[0].kind == "renamed" and files[0].old_path:
        title = f"Rename {files[0].old_path.rsplit('/', 1)[-1]} to {names[0]}"
    elif len(files) == 1:
        title = f"{verb} {names[0]}"
    elif len(files) == 2:
        title = f"{verb} {names[0]} and {names[1]}"
    else:
        where = _common_dir([f.path for f in files])
        title = f"{verb} {len(files)} files" + (f" in {where}" if where else "")
    return title if len(title) <= 72 else title[:71].rstrip() + "…"


MAX_TITLE = 256  # GitHub's limit
MAX_BODY = 20_000


def clean_title(text: str | None) -> str | None:
    """A typed title on one line; blank = use the generated one."""
    line = " ".join((text or "").split())
    return line[:MAX_TITLE] or None


def clean_body(text: str | None) -> str | None:
    """A typed description as-is (trimmed); None = use the generated one, "" = really empty."""
    if text is None:
        return None
    return text.replace("\r\n", "\n").strip()[:MAX_BODY]


def _tick(path: str) -> str:
    return "`" + path.replace("`", "'") + "`"


def pr_body(patch: bytes | str | None, summary: str | None, agent_job_id: str) -> str:
    """A reviewer's view: the files and their size first; the agent's own words, if any, are
    folded away at the end. The original prompt is deliberately left out."""
    files = parse_changes(patch)
    parts: list[str] = []
    if files:
        added, removed = sum(f.added for f in files), sum(f.removed for f in files)
        noun = "file" if len(files) == 1 else "files"
        parts += [f"## Changes\n\n{len(files)} {noun} changed, +{added} / -{removed}."]
        lines = []
        for f in files[:MAX_LISTED]:
            size = "binary" if f.binary else f"+{f.added} / -{f.removed}"
            name = (
                f"{_tick(f.old_path)} → {_tick(f.path)}"
                if f.kind == "renamed" and f.old_path
                else _tick(f.path)
            )
            lines.append(f"- {_PAST[f.kind]} {name} ({size})")
        if len(files) > MAX_LISTED:
            lines.append(f"- … and {len(files) - MAX_LISTED} more")
        parts.append("\n".join(lines))
    if summary and summary.strip():
        parts.append("## Notes from the agent\n\n" + summary.strip()[:8000])
    parts.append(f"Opened from Thinair job `{agent_job_id}`.")
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
    name, email = commit_identity()
    ident = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    code, out = await _git(
        mgr, job_id, volume, [*ident, "commit", "-q", "-m", message[0], "-m", message[1]], log
    )
    if code != 0:
        raise PRError("commit_failed", "Could not commit the change: " + " | ".join(out[-3:]))


async def push_branch(
    mgr: SandboxManager, job_id: str, volume: str, branch: str, cred: "Credential | str", log
) -> None:
    if isinstance(cred, str):  # a bare token (the PAT): kept for existing callers
        cred = Credential(cred, "token")
    log(f"[thinair] pushing branch {branch}")
    code, out = await _git(
        mgr,
        job_id,
        volume,
        ["push", "-q", "--force", "origin", f"HEAD:refs/heads/{branch}"],
        log,
        token=cred.token,
        timeout_s=300,
    )
    if code != 0:
        if cred.source == "app" and any("workflow" in ln.lower() for ln in out):
            raise PRError(
                "push_failed",
                "This change edits files under .github/workflows/, which Thinair's GitHub App "
                "is not allowed to push (it has no Workflows permission, on purpose). Download "
                "the patch instead, or leave those files out of the change.",
            )
        who = (
            "Thinair's GitHub App needs access to this repository with Contents: Read and write"
            if cred.source == "app"
            else 'The token needs "Contents: Read and write" on this repository'
        )
        raise PRError(
            "push_failed",
            f"GitHub refused the push. {who} (you can only open PRs on repos you can push to). "
            "Last output: " + " | ".join(out[-3:]),
        )


async def pr_executor(ctx: JobContext, mgr: SandboxManager | None) -> ExecResult:
    assert mgr is not None
    job = ctx.job
    access = GitHubAccess()  # replaced below by one for the person the job belongs to
    try:
        require_credentials()  # nothing configured: stop before touching anything else
        with session_scope() as s:
            parent = s.get(Job, job.parent_job_id) if job.parent_job_id else None
            result = s.get(JobResult, parent.id) if parent else None
            repo = s.get(Repo, parent.repo_id) if parent else None
            who = job_user(s, parent) if parent is not None else None
            allowed = parent is not None and github_allowed(who)
            # Their own installations are theirs; the server's connection only if they may use it.
            access = GitHubAccess(
                user_id=who.id if who is not None else None, use_server=github_token_allowed(who)
            )
        check_pr_possible(parent, result)
        assert parent is not None and result is not None and repo is not None
        if not allowed:  # also checked at submit; the setting may have changed since
            from thinair.jobs.service import GITHUB_NOT_ALLOWED

            raise PRError("github_not_allowed", GITHUB_NOT_ALLOWED)
        assert result.patch is not None and parent.commit_sha is not None
        owner, name = repo_slug(repo.url)
        branch = pr_branch(parent.id)
        title = job.pr_title or pr_title(result.patch)
        body = (
            job.pr_body
            if job.pr_body is not None
            else pr_body(result.patch, result.summary, parent.id)
        )

        # The push container gets a Contents:write key and nothing else. The Pull requests:write
        # key is separate and never leaves this process (it is not in any SandboxSpec).
        push_cred = await _credential(access, repo.url, "push")
        volume = workspace_volume(job.id)
        try:
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
                    token=push_cred.token,
                )
                await push_branch(mgr, job.id, volume, branch, push_cred, ctx.log)
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.shield(asyncio.to_thread(mgr.remove_volume, volume))
        finally:
            await asyncio.shield(push_cred.arevoke())

        pr_cred = await _credential(access, repo.url, "pr")
        try:
            gh = GitHubClient(pr_cred.token, source=pr_cred.source)
            base = parent.ref or await asyncio.to_thread(gh.default_branch, owner, name)
            ctx.log(f"[thinair] opening a pull request: {branch} -> {base}")
            url = await asyncio.to_thread(
                gh.create_pull, owner, name, title=title, body=body, head=branch, base=base
            )
        finally:
            await asyncio.shield(pr_cred.arevoke())
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
