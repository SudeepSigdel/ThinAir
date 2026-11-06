"""Turn a repo (GitHub URL or local directory) into a ready-to-test workspace volume.

Pipeline: create volume -> get code in (clone inside a sandbox, or tar upload) -> read
`thinair.yaml` + detect stack -> run the install phase. After this, the profiler and agent
runner only need `PreparedProject.volume` and `.resolved`.

The repo is never handled by Thinair's own process: `git clone` and every repo-supplied
command run inside sandboxes.
"""

import asyncio
import hashlib
import io
import re
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from thinair.config import get_settings
from thinair.github import GitHubTokenError, git_auth_env, github_token
from thinair.project.config import ProjectConfig, ProjectConfigError, parse_project_config
from thinair.project.detect import IMAGES, PYTHON_IMAGE, ResolvedProject, resolve_project
from thinair.sandbox.manager import SandboxManager
from thinair.sandbox.spec import SandboxError, SandboxSpec, VolumeMount

LogFn = Callable[[str], None]
IMAGES_DIR = Path(__file__).resolve().parent.parent / "sandbox" / "images"
GITHUB_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(?:\.git)?/?$")
GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.-]{0,199}$")
MAX_UPLOAD_BYTES = 200 * 1024**2
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
SANDBOX_UID = 10001
# Clone runs in a small git-only image instead of a git-enabled Python/Node image, so building
# our own images never needs apt (and the test sandboxes carry no git client).
GIT_IMAGE = "thinair-sandbox-git:1"
BUILDABLE_IMAGES = {**IMAGES, "git": GIT_IMAGE}


class PrepareError(Exception):
    """A user-facing failure while preparing a repo. `code` is stable for the API/UI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GitSource:
    url: str
    ref: str | None = None

    def validate(self) -> None:
        if not GITHUB_URL.match(self.url):
            raise PrepareError(
                "bad_repo_url",
                "Only GitHub repositories are supported for now, "
                "e.g. https://github.com/owner/repo",
            )
        if self.ref is not None and not GIT_REF.match(self.ref):
            raise PrepareError("bad_ref", f"Invalid branch or tag name: {self.ref!r}")


@dataclass(frozen=True)
class DirSource:
    path: Path


@dataclass
class PreparedProject:
    volume: str
    resolved: ResolvedProject
    commit_sha: str | None


def workspace_volume(job_id: str) -> str:
    return f"thinair-ws-{job_id}"


# ---- dependency cache ----------------------------------------------------------------------
# Package managers' download caches, kept per (repo, user) in a volume that outlives jobs, so a
# repeat job on the same project reinstalls from local files instead of the network. Mounted
# ONLY during the install step (never for tests or agents). Per user, because install commands
# come from the repo and can write anything into the cache: nobody's jobs read a cache another
# user's jobs wrote. `thinair cache clear` removes them.
CACHE_LABEL = "thinair.cache"
CACHE_ENV = {
    "PIP_CACHE_DIR": "/cache/pip",
    "UV_CACHE_DIR": "/cache/uv",
    "npm_config_cache": "/cache/npm",
    "YARN_CACHE_FOLDER": "/cache/yarn",
}


def cache_volume(repo_url: str, scope: str) -> str:
    """Name of the dependency cache for `repo_url` as used by `scope` (a user name)."""
    digest = hashlib.sha256(f"{scope} {repo_url}".encode()).hexdigest()[:20]
    return f"thinair-cache-{digest}"


def dir_to_tar(path: Path, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Pack a local directory for upload. Files are owned by the sandbox user; symlinks and
    junk directories (node_modules, .venv, ...) are skipped."""
    path = path.resolve()
    if not path.is_dir():
        raise PrepareError("bad_path", f"{path} is not a directory")
    buf = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for f in sorted(path.rglob("*")):
            rel = f.relative_to(path)
            if SKIP_DIRS & set(rel.parts) or f.is_symlink() or not (f.is_file() or f.is_dir()):
                continue
            total += f.stat().st_size if f.is_file() else 0
            if total > max_bytes:
                raise PrepareError("too_large", f"Directory exceeds {max_bytes // 1024**2} MB")

            def _own(ti: tarfile.TarInfo) -> tarfile.TarInfo:
                ti.uid = ti.gid = SANDBOX_UID
                ti.uname = ti.gname = ""
                return ti

            tar.add(f, arcname=rel.as_posix(), recursive=False, filter=_own)
    return buf.getvalue()


async def ensure_image(mgr: SandboxManager, stack: str, log: LogFn) -> str:
    tag = BUILDABLE_IMAGES[stack]
    if not await asyncio.to_thread(mgr.image_exists, tag):
        log(f"[thinair] building sandbox image {tag} (first use only; can take minutes)")
        await asyncio.to_thread(mgr.build_image, tag, IMAGES_DIR / f"{stack}.Dockerfile")
    return tag


def _spec(
    job_id: str, volume: str, image: str, command: list[str], ro: bool = False, **kw
) -> SandboxSpec:
    kw.setdefault("mounts", [VolumeMount(volume=volume, path="/workspace", read_only=ro)])
    return SandboxSpec(image=image, command=command, job_id=job_id, **kw)


async def _read_only(
    mgr, job_id, volume, command: list[str], image: str = PYTHON_IMAGE
) -> tuple[int | None, list[str]]:
    res, lines = await mgr.run_capture(
        _spec(
            job_id,
            volume,
            image,
            command,
            ro=True,
            cpus=0.5,
            memory_bytes=128 * 1024**2,
            timeout_s=30,
            env={"HOME": "/tmp"},  # noqa: S108
        )
    )
    return res.exit_code, lines


async def _list_names(mgr, job_id, volume, subdir: str = ".") -> set[str]:
    code, lines = await _read_only(mgr, job_id, volume, ["ls", "-1A", "--", f"/workspace/{subdir}"])
    return set(lines) if code == 0 else set()


async def _read_text(mgr, job_id, volume, rel: str) -> str | None:
    code, lines = await _read_only(mgr, job_id, volume, ["cat", "--", f"/workspace/{rel}"])
    return "\n".join(lines) if code == 0 else None


async def prepare_workspace(
    mgr: SandboxManager,
    source: GitSource | DirSource,
    job_id: str,
    log: LogFn,
    *,
    github_auth: bool = True,
    cache_scope: str | None = None,
) -> PreparedProject:
    """Create the workspace volume and run clone/upload, detection and install.

    `github_auth=False` clones anonymously even if a GitHub token is configured (members' jobs).
    `cache_scope` (a user name) turns on the dependency cache for GitHub sources.
    On any failure the volume is removed and a `PrepareError` is raised.
    """
    if isinstance(source, GitSource):
        source.validate()
    volume = workspace_volume(job_id)
    try:
        await ensure_image(mgr, "python", log)  # also used for clone / inspection
        await asyncio.to_thread(mgr.create_volume, volume, job_id)
        commit = await _load_source(mgr, source, job_id, volume, log, github_auth)

        names = await _list_names(mgr, job_id, volume)
        cfg_text = (
            await _read_text(mgr, job_id, volume, "thinair.yaml")
            if "thinair.yaml" in names
            else None
        )
        config = parse_project_config(cfg_text)
        if config.workdir != ".":
            names = await _list_names(mgr, job_id, volume, config.workdir)
        resolved = resolve_project(names, config)
        log(f"[thinair] detected {resolved.stack} project; install: {resolved.install}")

        await ensure_image(mgr, resolved.stack, log)
        cache = None
        if cache_scope and isinstance(source, GitSource) and get_settings().dependency_cache:
            cache = cache_volume(source.url, cache_scope)
            labels = {CACHE_LABEL: "true", "thinair.cache.scope": cache_scope[:63]}
            await asyncio.to_thread(mgr.ensure_persistent_volume, cache, labels)
        await _install(mgr, job_id, volume, resolved, log, cache)
        return PreparedProject(volume=volume, resolved=resolved, commit_sha=commit)
    except ProjectConfigError as e:
        await asyncio.to_thread(mgr.remove_volume, volume)
        raise PrepareError("config_invalid", str(e)) from e
    except SandboxError as e:
        await asyncio.to_thread(mgr.remove_volume, volume)
        raise PrepareError("sandbox_error", str(e)) from e
    except BaseException:
        await asyncio.to_thread(mgr.remove_volume, volume)
        raise


async def _load_source(mgr, source, job_id, volume, log, github_auth=True) -> str | None:
    if isinstance(source, DirSource):
        log(f"[thinair] importing {source.path}")
        data = await asyncio.to_thread(dir_to_tar, source.path)
        await asyncio.to_thread(mgr.put_tar, volume, PYTHON_IMAGE, data, job_id)
        return None

    try:
        token = github_token() if github_auth else None
    except GitHubTokenError as e:
        raise PrepareError("bad_github_token", str(e)) from None
    log(f"[thinair] cloning {source.url}" + (" (with the GitHub token)" if token else ""))
    await ensure_image(mgr, "git", log)
    tail: list[str] = []
    spec = clone_spec(job_id, volume, source, token)
    res = await mgr.run(spec, lambda ln: (tail.append(ln), log(ln)))
    if res.exit_code != 0:
        if token:
            hint = (
                "Check the URL, and that THINAIR_GITHUB_TOKEN has read access to this "
                "repository and has not expired."
            )
        elif not github_auth:
            hint = (
                "It may not exist, or it may be private: only admins' jobs may use the "
                "server's GitHub token."
            )
        else:
            hint = (
                "It may not exist, or it may be private: set THINAIR_GITHUB_TOKEN to clone "
                "private repositories."
            )
        raise PrepareError(
            "clone_failed",
            f"Could not clone the repository. {hint} Last output: " + " | ".join(tail[-3:]),
        )
    code, lines = await _read_only(mgr, job_id, volume, ["rev-parse", "HEAD"], image=GIT_IMAGE)
    return lines[0] if code == 0 and lines else None


def clone_spec(job_id: str, volume: str, source: GitSource, token: str | None) -> SandboxSpec:
    """The clone sandbox. With a token, git reads it from env (never argv or .git/config) and
    it is masked in the output."""
    # The image's entrypoint is `git`, so the command holds only git's arguments.
    clone = ["-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never"]
    clone += ["clone", "--depth", "1", "--single-branch"]
    if source.ref:
        clone += ["--branch", source.ref]
    clone += ["--", source.url, "."]
    env = {"HOME": "/tmp", "GIT_TERMINAL_PROMPT": "0"}  # noqa: S108
    redact: list[str] = []
    if token:
        auth_env, redact = git_auth_env(token)
        env.update(auth_env)
    return _spec(
        job_id,
        volume,
        GIT_IMAGE,
        clone,
        network=True,
        cpus=1,
        memory_bytes=512 * 1024**2,
        timeout_s=300,
        env=env,
        redact=redact,
    )


def mirror_env() -> dict[str, str]:
    """Package-mirror settings for the install phase only (never for test runs)."""
    s = get_settings()
    env: dict[str, str] = {}
    for value, var in ((s.pip_index_url, "PIP_INDEX_URL"), (s.npm_registry, "npm_config_registry")):
        if value:
            if not value.startswith(("https://", "http://")):
                raise PrepareError("bad_mirror", f"{var} mirror must be an http(s) URL")
            env[var] = value
    return env


async def _install(
    mgr, job_id, volume, resolved: ResolvedProject, log: LogFn, cache: str | None = None
) -> None:
    cfg: ProjectConfig = resolved.config
    tail: list[str] = []
    env = {**resolved.env, **mirror_env()}
    extra: dict = {}
    if cache:
        env.update(CACHE_ENV)
        extra["mounts"] = [
            VolumeMount(volume=volume, path="/workspace"),
            VolumeMount(volume=cache, path="/cache"),
        ]
        log("[thinair] using this project's dependency cache")
    res = await mgr.run(
        _spec(
            job_id,
            volume,
            resolved.image,
            ["sh", "-c", resolved.install],
            workdir=resolved.workdir,
            env=env,
            network=cfg.network.install,
            tmp_noexec=False,
            tmpfs_tmp_bytes=512 * 1024**2,
            cpus=2,
            memory_bytes=2 * 1024**3,
            pids_limit=512,
            timeout_s=cfg.timeouts.install_s,
            **extra,
        ),
        lambda ln: (tail.append(ln), log(ln)),
    )
    if res.exit_code != 0:
        why = (
            "ran out of memory (2 GB install limit)"
            if res.oom_killed
            else f"timed out after {cfg.timeouts.install_s}s"
            if res.timed_out
            else f"exited with code {res.exit_code}"
        )
        raise PrepareError(
            "install_failed",
            f"The install step failed: it {why}. Command: {resolved.install!r}. "
            "Set `install:` in thinair.yaml to fix it. Last output: " + " | ".join(tail[-3:]),
        )
