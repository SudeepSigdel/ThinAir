"""Run one hardened container per call and always clean it up.

Hardening applied to every container (see `build_container_args`): non-root user, all Linux
capabilities dropped, no-new-privileges, read-only root filesystem, CPU/memory/pids limits,
swap disabled (memswap == memory, so OOM is not hidden), network off by default, and no
host mounts. The Docker socket is never exposed.

Cleanup: the container is removed in `finally`; cancellation kills it; the janitor
(`thinair.sandbox.janitor`) catches anything left behind by a crash.
"""

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import docker
import requests
from docker.errors import APIError, DockerException, ImageNotFound
from docker.models.containers import Container

from thinair.config import get_settings
from thinair.sandbox.spec import (
    LABEL_JOB,
    LABEL_MANAGED,
    SANDBOX_USER,
    SandboxError,
    SandboxResult,
    SandboxSpec,
)

LineCallback = Callable[[str], None]
MAX_LINE_CHARS = 8192


def build_container_args(spec: SandboxSpec) -> dict[str, Any]:
    """Translate a spec into docker-py `containers.create` kwargs (pure; unit-tested)."""
    return {
        "image": spec.image,
        "command": spec.command,
        "working_dir": spec.workdir,
        "environment": spec.env,
        "user": SANDBOX_USER,
        "labels": {LABEL_MANAGED: "true", LABEL_JOB: spec.job_id},
        "detach": True,
        "tty": False,
        "stdin_open": False,
        "privileged": False,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "read_only": spec.read_only_rootfs,
        "tmpfs": {
            "/tmp": f"rw,{'noexec,' if spec.tmp_noexec else ''}nosuid,size={spec.tmpfs_tmp_bytes}"  # noqa: S108
        },
        "nano_cpus": int(spec.cpus * 1_000_000_000),
        "mem_limit": spec.memory_bytes,
        "memswap_limit": spec.memory_bytes,
        "pids_limit": spec.pids_limit,
        "network_mode": "bridge" if spec.network else "none",
        "volumes": {
            m.volume: {"bind": m.path, "mode": "ro" if m.read_only else "rw"} for m in spec.mounts
        },
    }


class _Handle:
    """Lets another thread/task abort a run, even before the container exists."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._container: Container | None = None
        self.aborted = False

    def attach(self, container: Container) -> None:
        with self._lock:
            self._container = container
            if self.aborted:
                self._kill()

    def abort(self) -> None:
        with self._lock:
            self.aborted = True
            self._kill()

    def _kill(self) -> None:
        if self._container is not None:
            with contextlib.suppress(DockerException):
                self._container.kill()


class SandboxManager:
    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client
        self._host_cpus: int | None = None

    def host_cpus(self) -> int:
        """How many CPUs the Docker host offers (0 if unknown). Read once and cached.

        Docker refuses to create a container whose `--cpus` exceeds this ("range of CPUs is from
        0.01 to N"), so every limit is capped to it (see `fit`).
        """
        if self._host_cpus is None:
            try:
                self._host_cpus = max(0, int(self.client.info().get("NCPU") or 0))
            except (DockerException, requests.exceptions.RequestException, TypeError, ValueError):
                self._host_cpus = 0
        return self._host_cpus

    def fit(self, spec: SandboxSpec) -> SandboxSpec:
        """Cap the CPU limit at what the host has, so default limits (2 CPUs for installs and
        agents) also work on a 1-CPU VM. A limit is a ceiling, so nothing else changes."""
        host = self.host_cpus()
        if host and spec.cpus > host:
            return spec.model_copy(update={"cpus": float(host)})
        return spec

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            host = get_settings().docker_host
            try:
                self._client = docker.DockerClient(base_url=host) if host else docker.from_env()
            except DockerException as e:
                raise SandboxError(_docker_down_message(e)) from e
        return self._client

    # ---- volumes -------------------------------------------------------------------------
    def create_volume(self, name: str, job_id: str) -> None:
        self.client.volumes.create(name=name, labels={LABEL_MANAGED: "true", LABEL_JOB: job_id})

    def ensure_persistent_volume(self, name: str, labels: dict[str, str]) -> None:
        """Get or create a volume that must survive jobs (agent logins). It deliberately has no
        `thinair.managed` label, so the janitor never removes it."""
        try:
            self.client.volumes.get(name)
        except docker.errors.NotFound:
            self.client.volumes.create(name=name, labels=labels)

    def build_image_from_text(self, tag: str, dockerfile: str) -> None:
        """Build from Dockerfile text with no build context (only FROM/RUN/COPY --from)."""
        import io

        try:
            self.client.images.build(
                fileobj=io.BytesIO(dockerfile.encode()), tag=tag, rm=True, forcerm=True
            )
        except (docker.errors.BuildError, APIError, requests.exceptions.RequestException) as e:
            raise SandboxError(f"Could not build image {tag}: {e}") from e

    def remove_volume(self, name: str) -> None:
        with contextlib.suppress(docker.errors.NotFound):
            self.client.volumes.get(name).remove(force=True)

    # ---- images and volume contents --------------------------------------------------------
    def image_exists(self, tag: str) -> bool:
        try:
            self.client.images.get(tag)
        except ImageNotFound:
            return False
        return True

    def build_image(self, tag: str, dockerfile: Path) -> None:
        """Build a sandbox base image from one of our own Dockerfiles (never a user's)."""
        try:
            self.client.images.build(
                path=str(dockerfile.parent), dockerfile=dockerfile.name, tag=tag, rm=True
            )
        except (docker.errors.BuildError, APIError, requests.exceptions.RequestException) as e:
            raise SandboxError(f"Could not build sandbox image {tag}: {e}") from e

    def put_tar(self, volume: str, image: str, tar_bytes: bytes, job_id: str) -> None:
        """Extract a tar archive into the root of a named volume (mounted at /workspace).

        A never-started helper container is used, so no code runs while the files land.
        """
        c = self.client.containers.create(
            image,
            ["true"],
            volumes={volume: {"bind": "/workspace", "mode": "rw"}},
            labels={LABEL_MANAGED: "true", LABEL_JOB: job_id},
            network_mode="none",
        )
        try:
            if not c.put_archive("/workspace", tar_bytes):
                raise SandboxError("Docker refused the workspace upload")
        finally:
            with contextlib.suppress(DockerException):
                c.remove(force=True, v=True)

    async def run_capture(self, spec: SandboxSpec) -> tuple[SandboxResult, list[str]]:
        """Run to completion and return the output lines alongside the result."""
        lines: list[str] = []
        res = await self.run(spec, lines.append)
        await asyncio.sleep(0)  # let queued line callbacks flush
        return res, lines

    # ---- running -------------------------------------------------------------------------
    async def run(self, spec: SandboxSpec, on_line: LineCallback | None = None) -> SandboxResult:
        """Run to completion. Cancelling the awaiting task kills and removes the container."""
        loop = asyncio.get_running_loop()
        handle = _Handle()
        cb = (lambda line: loop.call_soon_threadsafe(on_line, line)) if on_line else None
        fut = asyncio.ensure_future(asyncio.to_thread(self.run_blocking, spec, cb, handle))
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            handle.abort()
            with contextlib.suppress(Exception):
                await asyncio.shield(fut)  # let the thread finish its cleanup
            raise

    def run_blocking(
        self,
        spec: SandboxSpec,
        on_line: LineCallback | None = None,
        handle: _Handle | None = None,
    ) -> SandboxResult:
        spec = self.fit(spec)
        handle = handle or _Handle()
        container: Container | None = None
        timed_out = threading.Event()
        timer: threading.Timer | None = None
        threads: list[threading.Thread] = []
        stats: dict[str, float] = {}
        truncated = threading.Event()
        try:
            try:
                container = self.client.containers.create(**build_container_args(spec))
            except ImageNotFound as e:
                raise SandboxError(
                    f"Image {spec.image!r} not found. Build or pull it first."
                ) from e
            except (APIError, requests.exceptions.RequestException) as e:
                raise SandboxError(f"Could not create sandbox: {e}") from e
            handle.attach(container)

            t0 = time.monotonic()
            try:
                container.start()
            except (APIError, requests.exceptions.RequestException) as e:
                if handle.aborted:
                    return SandboxResult(cancelled=True, wall_s=time.monotonic() - t0)
                raise SandboxError(f"Could not start sandbox: {e}") from e

            threads = [
                threading.Thread(
                    target=_stream_logs, args=(container, spec, on_line, truncated), daemon=True
                ),
                threading.Thread(target=_sample_stats, args=(container, stats), daemon=True),
            ]
            for t in threads:
                t.start()

            def _timeout() -> None:
                timed_out.set()
                handle.abort()

            timer = threading.Timer(spec.timeout_s, _timeout)
            timer.daemon = True
            timer.start()

            _wait_until_exited(container)
            wall = time.monotonic() - t0
            container.reload()
            state = container.attrs.get("State", {})
            for t in threads:
                t.join(timeout=5)
            return SandboxResult(
                exit_code=state.get("ExitCode"),
                oom_killed=bool(state.get("OOMKilled")),
                timed_out=timed_out.is_set(),
                cancelled=handle.aborted and not timed_out.is_set(),
                wall_s=wall,
                peak_mem_bytes=int(stats["peak_mem"]) if "peak_mem" in stats else None,
                cpu_time_s=stats.get("cpu_time_s"),
                output_truncated=truncated.is_set(),
            )
        finally:
            if timer is not None:
                timer.cancel()
            if container is not None:
                with contextlib.suppress(DockerException, requests.exceptions.RequestException):
                    container.remove(force=True, v=True)


def _docker_down_message(err: Exception) -> str:
    return (
        "Cannot reach the Docker daemon. Is Docker running, and is THINAIR_DOCKER_HOST "
        f"correct? ({err})"
    )


def _wait_until_exited(container: Container) -> None:
    """Block until the container stops. Polls in slices because docker-py's HTTP timeout
    (60 s by default) is shorter than a job."""
    while True:
        try:
            container.wait(timeout=30)
            return
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
            try:
                container.reload()
            except DockerException:
                return
            if container.status != "running":
                return


def _stream_logs(
    container: Container,
    spec: SandboxSpec,
    on_line: LineCallback | None,
    truncated: threading.Event,
) -> None:
    """Split the merged stdout/stderr stream into lines, redact secrets, cap total size."""
    secrets_ = [s for s in spec.redact if s]
    buf = b""
    total = 0

    def emit(raw: bytes) -> None:
        nonlocal total
        if on_line is None:
            return
        total += len(raw) + 1
        if total > spec.max_output_bytes:
            if not truncated.is_set():
                truncated.set()
                on_line(f"[thinair] output truncated after {spec.max_output_bytes} bytes")
            return
        line = raw.decode("utf-8", errors="replace")[:MAX_LINE_CHARS]
        for s in secrets_:
            line = line.replace(s, "***")
        on_line(line)

    with contextlib.suppress(DockerException, requests.exceptions.RequestException):
        for chunk in container.logs(stream=True, follow=True, stdout=True, stderr=True):
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for ln in lines:
                emit(ln.rstrip(b"\r"))
    if buf:
        emit(buf)


def _sample_stats(container: Container, out: dict[str, float]) -> None:
    """Track peak memory and CPU time from Docker's ~1 Hz stats stream (job-lifetime only)."""
    with contextlib.suppress(DockerException, requests.exceptions.RequestException, KeyError):
        for s in container.stats(stream=True, decode=True):
            mem = s.get("memory_stats", {})
            usage = mem.get("usage")
            if usage is not None:
                st = mem.get("stats", {})
                cache = st.get("inactive_file", st.get("total_inactive_file", 0))
                out["peak_mem"] = max(out.get("peak_mem", 0), usage - cache)
            cpu = s.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage")
            if cpu is not None:
                out["cpu_time_s"] = cpu / 1e9
