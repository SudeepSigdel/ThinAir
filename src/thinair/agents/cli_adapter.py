"""Run any agent CLI, described by an `AgentConfig`, inside a sandbox next to the project.

Lifecycle (the `AgentAdapter` interface):
    adapter = CLIAdapter(config, mgr, job_id, prepared)
    await adapter.prepare(log)          # agent image, login volume, git baseline, login check
    async for event in adapter.run(task):
        ...                             # streamed AgentEvents
    result = adapter.result()           # exit status, success, summary, diff, logs

Thinair never reads the agent's credentials: they live in the `thinair-auth-<id>` volume (or in
environment variables the owner chose to pass through), are mounted only into the agent's own
container, and are masked in all captured output.
"""

import asyncio
import hashlib
import os
import re
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from thinair.agents.config import AgentConfig
from thinair.agents.stream import AgentEvent, StreamParser
from thinair.config import get_settings
from thinair.project.prepare import GIT_IMAGE, PreparedProject, ensure_image
from thinair.sandbox.egress import EgressGateway
from thinair.sandbox.manager import SandboxManager
from thinair.sandbox.spec import SandboxSpec, VolumeMount

LogFn = Callable[[str], None]
NODE_IMAGE_FOR_RUNTIME = "node:22-slim"
CA_CERTS_IMAGE = "python:3.12-slim"  # Debian with ca-certificates
MAX_DIFF_BYTES = 1024**2
# The exact patch kept for opening a pull request. Bigger changes can still be reviewed in the
# (capped) diff, but not turned into a PR from the phone.
MAX_PATCH_BYTES = 5 * 1024**2
PATCH_PATH = ".git/thinair.patch"  # inside the workspace, outside the working tree
# Dependency folders created by the install step are never part of the agent's change.
DIFF_EXCLUDES = [":(exclude).venv", ":(exclude)node_modules"]
_GIT_ID = ["-c", "user.name=Thinair", "-c", "user.email=thinair@localhost"]
_GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *_GIT_ID]


class AgentAuthError(Exception):
    code = "agent_not_authenticated"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class AgentResult:
    exit_status: int | None = None
    success: bool = False
    summary: str = ""
    diff: str = ""
    diff_stat: dict[str, Any] = field(default_factory=dict)
    diff_truncated: bool = False
    # Byte-exact `git diff --binary` against the clone, or None (no changes / too large).
    patch: bytes | None = None
    patch_too_large: bool = False
    cost_usd: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    logs_tail: list[str] = field(default_factory=list)


class AgentAdapter(Protocol):
    async def prepare(self, log: LogFn) -> None: ...
    def run(self, task: str) -> AsyncIterator[AgentEvent]: ...
    def result(self) -> AgentResult: ...


# ---- agent image -----------------------------------------------------------------------------
def agent_dockerfile(cfg: AgentConfig, base_image: str, stack: str) -> str:
    lines = [f"FROM {base_image}", "USER root"]
    if cfg.needs_ca_certs:
        # From the same Debian release as our images; no apt needed.
        lines += [
            f"COPY --from={CA_CERTS_IMAGE} /etc/ssl/certs/ca-certificates.crt "
            "/etc/ssl/certs/ca-certificates.crt",
            "ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
        ]
    if cfg.needs_node and stack != "node":
        # Same Debian/glibc family as our Python image, so the official binaries run as-is.
        lines += [
            f"COPY --from={NODE_IMAGE_FOR_RUNTIME} /usr/local/bin/node /usr/local/bin/node",
            f"COPY --from={NODE_IMAGE_FOR_RUNTIME} /usr/local/lib/node_modules "
            "/usr/local/lib/node_modules",
            "RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm"
            " && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx",
        ]
    # The login volume takes this directory's ownership when first mounted (see CLAUDE.md:
    # Docker copies an image's mount-point ownership into an empty volume), so the sandbox user
    # must own it or the agent cannot save its login.
    auth_dir = cfg.auth.config_dir
    lines += [
        f"RUN {cfg.install}",
        f"RUN mkdir -p {auth_dir} && chown 10001:10001 {auth_dir}",
        "USER 10001:10001",
        "",
    ]
    return "\n".join(lines)


def agent_image_tag(cfg: AgentConfig, stack: str, dockerfile: str) -> str:
    digest = hashlib.sha256(dockerfile.encode()).hexdigest()[:12]
    return f"thinair-agent-{cfg.id}-{stack}:{digest}"


async def ensure_agent_image(mgr: SandboxManager, cfg: AgentConfig, stack: str, log: LogFn) -> str:
    base = await ensure_image(mgr, stack, log)
    dockerfile = agent_dockerfile(cfg, base, stack)
    tag = agent_image_tag(cfg, stack, dockerfile)  # config change -> new tag -> rebuild
    if not await asyncio.to_thread(mgr.image_exists, tag):
        log(f"[thinair] building the {cfg.name} image for {stack} projects (first use only)")
        await asyncio.to_thread(mgr.build_image_from_text, tag, dockerfile)
    return tag


# ---- adapter ---------------------------------------------------------------------------------
class CLIAdapter:
    def __init__(
        self,
        cfg: AgentConfig,
        mgr: SandboxManager,
        job_id: str,
        prepared: PreparedProject,
        host_env: Mapping[str, str] | None = None,
    ) -> None:
        self.cfg = cfg
        self._mgr = mgr
        self._job = job_id
        self._p = prepared
        env = os.environ if host_env is None else host_env
        self._passthrough = {k: env[k] for k in cfg.auth.env_passthrough if env.get(k)}
        self._image: str | None = None
        self._base: str | None = None
        self._result = AgentResult()
        self._tail: deque[str] = deque(maxlen=40)
        self._egress_log: list[str] = []

    # -- helpers --
    def _mounts(self, workspace_ro: bool = False) -> list[VolumeMount]:
        return [
            VolumeMount(volume=self._p.volume, path="/workspace", read_only=workspace_ro),
            VolumeMount(volume=self.cfg.auth_volume, path=self.cfg.auth.config_dir),
        ]

    def _env(self) -> dict[str, str]:
        return {**self._p.resolved.env, **self.cfg.env, **self._passthrough}

    def _spec(self, command: list[str], **kw: Any) -> SandboxSpec:
        lim = self.cfg.limits
        base: dict[str, Any] = {
            "image": self._image,
            "command": command,
            "job_id": self._job,
            "workdir": self._p.resolved.workdir,
            "env": self._env(),
            "mounts": self._mounts(),
            "cpus": lim.cpus,
            "memory_bytes": lim.memory,
            "pids_limit": lim.pids,
            "timeout_s": lim.timeout_s,
            "network": self.cfg.network,
            "tmp_noexec": False,  # agents run arbitrary developer tooling
            "tmpfs_tmp_bytes": 512 * 1024**2,
            "redact": list(self._passthrough.values()),
        }
        base.update(kw)
        return SandboxSpec(**base)

    def _gateway(self) -> EgressGateway | None:
        """An allowlisting proxy for this run, if the agent declares `egress` and the owner has
        not turned the allowlist off (THINAIR_AGENT_EGRESS=open)."""
        s = get_settings()
        if self.cfg.egress is None or s.agent_egress != "allowlist" or not self.cfg.network:
            return None
        extra = [h.strip() for h in s.agent_egress_extra_hosts.split(",") if h.strip()]
        return EgressGateway(self._mgr, self._job, [*self.cfg.egress, *extra])

    async def _git(self, args: list[str], *, max_bytes: int = 64 * 1024) -> tuple[int, list[str]]:
        res, lines = await self._mgr.run_capture(
            SandboxSpec(
                image=GIT_IMAGE,
                command=[*_GIT_SAFE, *args],
                job_id=self._job,
                mounts=[VolumeMount(volume=self._p.volume, path="/workspace")],
                env={"HOME": "/tmp"},  # noqa: S108
                cpus=1,
                memory_bytes=512 * 1024**2,
                timeout_s=120,
                max_output_bytes=max_bytes,
            )
        )
        return (res.exit_code if res.exit_code is not None else -1), lines

    # -- AgentAdapter --
    async def prepare(self, log: LogFn) -> None:
        stack = self._p.resolved.stack
        self._image = await ensure_agent_image(self._mgr, self.cfg, stack, log)
        await ensure_image(self._mgr, "git", log)
        await asyncio.to_thread(
            self._mgr.ensure_persistent_volume, self.cfg.auth_volume, {"thinair.auth": self.cfg.id}
        )
        # Baseline commit after clone + install, so the diff shows only the agent's work (even
        # work it commits itself). Uploaded folders without git get a repository here.
        await self._git(["init", "-q"])
        await self._git(["add", "-A", "--", ".", *DIFF_EXCLUDES])
        code, out = await self._git(["commit", "-q", "--allow-empty", "-m", "thinair: baseline"])
        if code != 0:
            raise RuntimeError("could not record the baseline commit: " + " | ".join(out[-3:]))
        code, out = await self._git(["rev-parse", "HEAD"])
        self._base = out[0] if code == 0 and out else None
        await self._check_login(log)

    async def _check_login(self, log: LogFn) -> None:
        check = self.cfg.auth.check_command
        if not check or self._passthrough:
            return  # a token was passed explicitly; the run itself will reveal a bad one
        log(f"[thinair] checking that {self.cfg.name} is logged in")
        res, lines = await self._mgr.run_capture(
            self._spec(check, timeout_s=90, cpus=1, memory_bytes=512 * 1024**2)
        )
        if res.exit_code != 0:
            raise AgentAuthError(self.cfg.auth_hint)

    async def run(self, task: str) -> AsyncIterator[AgentEvent]:
        if self._image is None:
            raise RuntimeError("call prepare() first")
        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        patterns = [p.lower() for p in self.cfg.auth_error_patterns]
        auth_hit = False
        result_event: AgentEvent | None = None
        parser = StreamParser(self.cfg.output)

        def on_line(line: str) -> None:
            nonlocal auth_hit
            self._tail.append(line)
            low = line.lower()
            if any(p in low for p in patterns):
                auth_hit = True
            for ev in parser.feed(line):
                queue.put_nowait(ev)

        def on_done(_t: asyncio.Task) -> None:
            for ev in parser.finish():  # e.g. buffered text when the output ends abruptly
                queue.put_nowait(ev)
            queue.put_nowait(None)

        gateway = self._gateway()
        spec_kw: dict[str, Any] = {}
        if gateway is not None:
            await gateway.start()
            spec_kw = {"network_name": gateway.network, "env": {**self._env(), **gateway.env}}
            yield AgentEvent(
                "log", "[thinair] outbound traffic limited to: " + ", ".join(gateway.allow)
            )
        try:
            spec = self._spec(self.cfg.argv(task), **spec_kw)
            runner = asyncio.create_task(self._mgr.run(spec, on_line))
            runner.add_done_callback(on_done)
            try:
                while (ev := await queue.get()) is not None:
                    if ev.kind == "result":
                        result_event = ev
                    elif ev.kind == "auth_error":
                        auth_hit = True
                    yield ev
            finally:
                if not runner.done():
                    runner.cancel()
                    await asyncio.wait([runner])
        finally:
            if gateway is not None:
                for line in await gateway.stop():
                    self._tail.append(line)
                    self._egress_log.append(line)
        for line in self._egress_log:
            if line.startswith("[egress] blocked"):
                yield AgentEvent("log", f"[thinair] {line[9:]} (not on the allowlist)")
        res = runner.result()  # SandboxError propagates to the caller

        r = self._result
        r.exit_status = res.exit_code
        r.logs_tail = list(self._tail)
        is_error = bool(result_event and result_event.data.get("is_error"))
        r.cost_usd = result_event.data.get("total_cost_usd") if result_event else None
        r.summary = (result_event.text if result_event else "") or _last_lines(self._tail)
        r.success = res.exit_code == 0 and not is_error and not res.timed_out
        if res.timed_out:
            r.error_code = "agent_timeout"
            r.error_message = (
                f"{self.cfg.name} did not finish within {self.cfg.limits.timeout_s} s."
            )
        elif auth_hit and not r.success:
            r.error_code, r.error_message = AgentAuthError.code, self.cfg.auth_hint
        elif not r.success:
            r.error_code = "agent_failed"
            r.error_message = (
                f"{self.cfg.name} reported a failure (exit code {res.exit_code}). "
                + (r.summary[:400] if r.summary else "")
            ).strip()
        await self._collect_diff()

    async def _collect_diff(self) -> None:
        r = self._result
        if not self._base:
            return
        await self._git(["add", "-A", "--", ".", *DIFF_EXCLUDES])
        _, stat = await self._git(["diff", "--cached", "--stat", "--no-color", self._base])
        code, lines = await self._git(
            ["diff", "--cached", "--no-color", "--no-ext-diff", self._base],
            max_bytes=MAX_DIFF_BYTES,
        )
        if code == 0:
            r.diff_truncated = any(ln.startswith("[thinair] output truncated") for ln in lines)
            r.diff = "\n".join(
                ln for ln in lines if not ln.startswith("[thinair] output truncated")
            )
        r.diff_stat = parse_diff_stat(stat[-1] if stat else "")
        if r.diff_stat.get("files"):
            await self._collect_patch()

    async def _collect_patch(self) -> None:
        """Write the binary patch into the volume and read it back as bytes. Captured output is
        line-based (CR stripped, long lines cut), so it can't carry an exact patch.

        For a cloned repo the patch is made against the *cloned commit*, not the post-install
        baseline, but only for the paths the agent changed. So it applies to a fresh clone even
        when the agent edited a file the install step created (e.g. an untracked lockfile), and
        untouched install output stays out of the pull request.
        """
        r = self._result
        target = self._base or "HEAD"
        pathspec: list[str] = []
        if self._p.commit_sha:
            code, names = await self._git(
                ["diff", "--cached", "--name-only", "--no-renames", self._base or "HEAD"]
            )
            # Unusual names come back C-quoted ("..."); fall back to the baseline diff then.
            if (
                code == 0
                and names
                and len(names) <= 2000
                and not any(n.startswith('"') for n in names)
            ):
                target = self._p.commit_sha
                pathspec = ["--", *(f":(literal){n}" for n in names)]
        code, _ = await self._git(
            [
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-color",
                "--no-ext-diff",
                f"--output={PATCH_PATH}",
                target,
                *pathspec,
            ]
        )
        if code != 0:
            return
        data = await asyncio.to_thread(
            self._mgr.get_file,
            self._p.volume,
            f"/workspace/{PATCH_PATH}",
            GIT_IMAGE,
            self._job,
            MAX_PATCH_BYTES,
        )
        if data is None:
            r.patch_too_large = True
        elif data:
            r.patch = data

    def result(self) -> AgentResult:
        return self._result


def _last_lines(lines: deque[str], n: int = 5) -> str:
    return "\n".join(ln for ln in list(lines)[-n:] if ln.strip())


_STAT = re.compile(
    r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?"
)


def parse_diff_stat(line: str) -> dict[str, int]:
    m = _STAT.search(line)
    if not m:
        return {"files": 0, "insertions": 0, "deletions": 0}
    return {
        "files": int(m.group(1)),
        "insertions": int(m.group(2) or 0),
        "deletions": int(m.group(3) or 0),
    }
