"""Connect the Docker-free search to real sandboxes.

Each probe is one fresh container: the project's test command, with hard CPU/memory limits, no
swap, and no network (unless thinair.yaml allows it). All probes share the prepared workspace
volume, so dependencies are installed once and every probe sees identical files.
"""

import asyncio
import contextlib
from collections import deque

from thinair.profiler.classify import make_record
from thinair.profiler.metrics import parse_metrics, wrap_command
from thinair.profiler.models import ProfileResult, RunRecord
from thinair.profiler.report import fmt_mem
from thinair.profiler.search import LogFn, ProfileSearch, ProgressFn
from thinair.project.prepare import (
    DirSource,
    GitSource,
    PreparedProject,
    prepare_workspace,
)
from thinair.sandbox.manager import SandboxManager
from thinair.sandbox.spec import SandboxSpec, VolumeMount

TAIL_LINES = 60


async def run_profile(
    mgr: SandboxManager,
    prepared: PreparedProject,
    job_id: str,
    *,
    on_run: ProgressFn | None = None,
    log: LogFn | None = None,
) -> ProfileResult:
    """Profile an already-prepared workspace."""
    r = prepared.resolved
    cfg = r.config

    async def probe(cpus: float, mem_bytes: int, timeout_s: float) -> RunRecord:
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        measured: list[tuple[int | None, float | None]] = []

        def collect(line: str) -> None:
            m = parse_metrics(line)  # the wrapper's marker line is data, not user-visible output
            if m is None:
                tail.append(line)
            else:
                measured.append(m)

        res = await mgr.run(
            SandboxSpec(
                image=r.image,
                command=wrap_command(r.test),
                job_id=job_id,
                workdir=r.workdir,
                env=r.env,
                mounts=[VolumeMount(volume=prepared.volume, path="/workspace")],
                cpus=cpus,
                memory_bytes=mem_bytes,
                pids_limit=512,
                timeout_s=timeout_s,
                network=cfg.network.test,
            ),
            collect,
        )
        await asyncio.sleep(0)  # let queued line callbacks flush
        if res.cancelled:
            raise asyncio.CancelledError
        rec = make_record(res, list(tail), cpus=cpus, mem_bytes=mem_bytes)
        if measured:  # exact in-container counters beat the ~1 Hz stats sampler
            peak, cpu_s = measured[-1]
            rec.peak_mem_bytes = peak or rec.peak_mem_bytes
            rec.cpu_time_s = cpu_s if cpu_s is not None else rec.cpu_time_s
        if log:
            log(
                f"[thinair] {cpus:g} CPU / {fmt_mem(mem_bytes)}: {rec.outcome.value} "
                f"in {rec.wall_s:.1f}s"
            )
        return rec

    # Tiers above the host's CPU count can't run (Docker refuses them), and capping them would
    # mislabel results ("2 CPU" really running on 1), so they are skipped and the report says so.
    host = await asyncio.to_thread(mgr.host_cpus)
    tiers = list(cfg.profile.cpus)
    skipped: list[float] = []
    if host:
        skipped = [c for c in tiers if c > host]
        tiers = [c for c in tiers if c <= host] or [float(host)]
        if skipped and log:
            log(f"[thinair] this machine has {host} CPU(s); skipping higher CPU tiers")

    result = await ProfileSearch(
        probe,
        tiers,
        cfg.profile.memory,
        base_timeout_s=cfg.timeouts.test_s,
        headroom=cfg.profile.headroom,
        confirm_runs=cfg.profile.confirm_runs,
        on_run=on_run,
    ).execute()
    if skipped:
        listed = ", ".join(f"{c:g}" for c in skipped)
        result.notes.insert(
            0,
            f"This machine has {host} CPU(s), so the {listed} CPU tier(s) were skipped. "
            "Results are only shown for CPU limits this machine can provide.",
        )
    return result


async def profile_source(
    mgr: SandboxManager,
    source: GitSource | DirSource,
    job_id: str,
    *,
    log: LogFn,
    on_run: ProgressFn | None = None,
    github_auth: bool = True,
) -> tuple[ProfileResult, PreparedProject]:
    """Prepare a repo, profile it, and always remove the workspace volume afterwards."""
    prepared = await prepare_workspace(mgr, source, job_id, log, github_auth=github_auth)
    try:
        return await run_profile(mgr, prepared, job_id, on_run=on_run, log=log), prepared
    finally:
        with contextlib.suppress(Exception):
            await asyncio.shield(asyncio.to_thread(mgr.remove_volume, prepared.volume))
