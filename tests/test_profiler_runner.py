"""run_profile on a small host, with a fake sandbox manager (no Docker)."""

import pytest

from thinair.profiler.runner import run_profile
from thinair.project.config import parse_project_config
from thinair.project.detect import resolve_project
from thinair.project.prepare import PreparedProject
from thinair.sandbox.spec import SandboxResult

MiB = 1024**2


class FakeManager:
    """Pretends to be a host with `cpus` CPUs; every test run passes with ~200 MiB."""

    def __init__(self, cpus: int) -> None:
        self.cpus = cpus
        self.requested: list[float] = []

    def host_cpus(self) -> int:
        return self.cpus

    async def run(self, spec, on_line=None) -> SandboxResult:
        self.requested.append(spec.cpus)
        passed = spec.memory_bytes >= 256 * MiB
        if on_line:
            on_line(f"[thinair-metrics] peak={200 * MiB} cpu_usec=1000000")
        return SandboxResult(exit_code=0 if passed else 137, oom_killed=not passed, wall_s=1.0)


def prepared(yaml: str | None = None) -> PreparedProject:
    resolved = resolve_project({"requirements.txt"}, parse_project_config(yaml))
    return PreparedProject(volume="thinair-ws-x", resolved=resolved, commit_sha=None)


async def test_one_cpu_host_skips_higher_tiers_and_says_so() -> None:
    mgr = FakeManager(cpus=1)
    result = await run_profile(mgr, prepared(), "job1")
    assert max(mgr.requested) == 1  # never asked Docker for more than the host has
    assert [lv.cpus for lv in result.levels] == [1, 0.5, 0.25]
    assert result.status == "ok" and result.min_config.cpus == 0.25
    assert "1 CPU(s)" in result.notes[0] and "2 CPU tier" in result.notes[0]
    assert result.recommended.cpus <= 1  # never recommends more than the machine has


async def test_all_configured_tiers_above_the_host_fall_back_to_the_host() -> None:
    mgr = FakeManager(cpus=1)
    result = await run_profile(mgr, prepared("profile: {cpus: [4, 2]}"), "job2")
    assert set(mgr.requested) == {1.0}
    assert [lv.cpus for lv in result.levels] == [1.0]
    assert "4, 2 CPU" in result.notes[0]


@pytest.mark.parametrize("host", [0, 16])  # 0 = unknown: use the configured tiers as-is
async def test_big_or_unknown_hosts_use_every_tier(host: int) -> None:
    mgr = FakeManager(cpus=host)
    result = await run_profile(mgr, prepared(), "job3")
    assert [lv.cpus for lv in result.levels] == [2, 1, 0.5, 0.25]
    assert not any("skipped" in n for n in result.notes)
