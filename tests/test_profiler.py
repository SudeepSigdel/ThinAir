import json

import pytest

from thinair.profiler.classify import classify, make_record
from thinair.profiler.models import Config, ProfileResult, RunOutcome, RunRecord
from thinair.profiler.report import fmt_mem, to_json, to_markdown
from thinair.profiler.search import ProfileSearch, recommend, round_up_memory, timeout_for
from thinair.sandbox.spec import SandboxResult

MiB = 1024**2
CPUS = [2, 1, 0.5, 0.25]
MEM = [2048 * MiB, 1024 * MiB, 512 * MiB, 256 * MiB, 128 * MiB]


class FakeProject:
    """A pretend project: needs `need(cpus)` bytes of memory and at least `min_cpus` CPUs."""

    def __init__(self, need, min_cpus: float = 0.0, flaky_once=(), fail_kind=None):
        self.need = need if callable(need) else (lambda _c, n=need: n)
        self.min_cpus = min_cpus
        self.flaky_once = set(flaky_once)
        self.fail_kind = fail_kind
        self.calls: list[tuple[float, int]] = []
        self.timeouts: list[float] = []

    async def __call__(self, cpus: float, mem: int, timeout: float) -> RunRecord:
        self.calls.append((cpus, mem))
        self.timeouts.append(timeout)
        n_seen = self.calls.count((cpus, mem))

        def rec(outcome: RunOutcome) -> RunRecord:
            peak = int(self.need(cpus) * 0.8) if outcome == RunOutcome.PASSED else None
            return RunRecord(cpus=cpus, mem_bytes=mem, outcome=outcome, peak_mem_bytes=peak)

        if self.fail_kind:
            return rec(self.fail_kind)
        if cpus < self.min_cpus:
            return rec(RunOutcome.TIMEOUT)
        if mem < self.need(cpus):
            return rec(RunOutcome.OOM_KILLED)
        if (cpus, mem) in self.flaky_once and n_seen >= 2:
            return rec(RunOutcome.TEST_FAILED)
        return rec(RunOutcome.PASSED)


def search(project, **kw) -> ProfileSearch:
    kw.setdefault("cpus", CPUS)
    kw.setdefault("memory", MEM)
    return ProfileSearch(project, **kw)


async def test_finds_minimum_with_far_fewer_runs_than_brute_force() -> None:
    p = FakeProject(200 * MiB)
    res = await search(p).execute()
    assert res.status == "ok"
    assert [lv.min_mem_bytes for lv in res.levels] == [256 * MiB] * 4
    assert res.min_config == Config(cpus=0.25, mem_bytes=256 * MiB)
    assert len(p.calls) == 11 < 20  # baseline + binary search + 1 per level + stability re-runs


async def test_lower_cpu_needing_more_memory_is_found() -> None:
    p = FakeProject(lambda c: 200 * MiB if c >= 1 else 300 * MiB)
    res = await search(p).execute()
    assert [lv.min_mem_bytes for lv in res.levels] == [256 * MiB, 256 * MiB, 512 * MiB, 512 * MiB]
    assert res.min_config == Config(cpus=0.25, mem_bytes=512 * MiB)


async def test_too_slow_cpu_levels_are_infeasible_then_skipped() -> None:
    p = FakeProject(200 * MiB, min_cpus=0.5)
    res = await search(p, cpus=[2, 1, 0.5, 0.25, 0.1]).execute()
    statuses = {lv.cpus: lv.status for lv in res.levels}
    assert statuses == {2: "found", 1: "found", 0.5: "found", 0.25: "infeasible", 0.1: "skipped"}
    assert "timeout" in next(lv for lv in res.levels if lv.cpus == 0.25).reason
    assert res.min_config.cpus == 0.5
    assert all(c >= 0.25 for c, _ in p.calls)  # 0.1 was never run


async def test_baseline_out_of_memory_explains_how_to_fix() -> None:
    p = FakeProject(10 * 1024 * MiB)
    res = await search(p).execute()
    assert res.status == "baseline_failed" and len(p.calls) == 1
    assert res.min_config is None and "profile.memory" in res.notes[0]


@pytest.mark.parametrize(
    ("kind", "hint"),
    [
        (RunOutcome.TEST_FAILED, "Fix the failing tests"),
        (RunOutcome.TIMEOUT, "timeouts.test_s"),
        (RunOutcome.STARTUP_FAILED, "test:"),
    ],
)
async def test_baseline_failures_get_specific_advice(kind, hint) -> None:
    res = await search(FakeProject(0, fail_kind=kind)).execute()
    assert res.status == "baseline_failed" and hint in res.notes[0]


async def test_flaky_boundary_steps_up_one_tier() -> None:
    # Passes once at 2 CPU / 256 MiB, fails on the stability re-run.
    p = FakeProject(200 * MiB, flaky_once=[(2, 256 * MiB)])
    res = await search(p).execute()
    first = res.levels[0]
    assert first.min_mem_bytes == 512 * MiB and first.flaky
    assert any("inconsistent" in n for n in res.notes)


async def test_run_budget_gives_partial_result() -> None:
    p = FakeProject(200 * MiB)
    res = await search(p, max_runs=3).execute()
    assert res.status == "incomplete" and len(res.runs) == 3 == len(p.calls)
    assert any("partial" in n for n in res.notes)


async def test_no_duplicate_first_attempts_and_progress_callback() -> None:
    seen: list[RunRecord] = []
    res = await search(FakeProject(200 * MiB), on_run=seen.append).execute()
    firsts = [(r.cpus, r.mem_bytes) for r in res.runs if r.attempt == 1]
    assert len(firsts) == len(set(firsts))
    assert seen == res.runs


async def test_stability_reruns_can_be_disabled() -> None:
    p = FakeProject(200 * MiB)
    res = await search(p, confirm_runs=1).execute()
    assert len(p.calls) == 7 and res.status == "ok"


async def test_timeouts_scale_with_lower_cpu_but_are_capped() -> None:
    p = FakeProject(200 * MiB)
    await search(p, base_timeout_s=100).execute()
    by_cpu = {c: t for (c, _), t in zip(p.calls, p.timeouts, strict=True)}
    assert by_cpu[2] == 100 and by_cpu[1] == 200 and by_cpu[0.5] == 400 and by_cpu[0.25] == 400
    assert timeout_for(0.1, 2, 10) == 40


async def test_single_tier_and_edge_notes() -> None:
    res = await search(FakeProject(50 * MiB), cpus=[1], memory=[512 * MiB]).execute()
    assert res.min_config == Config(cpus=1, mem_bytes=512 * MiB)
    assert any("smallest memory tier" in n for n in res.notes)
    assert any("smallest CPU tier" in n for n in res.notes)
    with pytest.raises(ValueError):
        ProfileSearch(FakeProject(0), cpus=[], memory=MEM)


async def test_unsorted_tiers_are_normalised() -> None:
    res = await search(FakeProject(200 * MiB), cpus=[0.25, 2, 1, 0.5], memory=MEM[::-1]).execute()
    assert res.min_config == Config(cpus=0.25, mem_bytes=256 * MiB)


def test_recommendation_adds_headroom_and_rounds_to_usual_sizes() -> None:
    rec = recommend(Config(cpus=0.5, mem_bytes=256 * MiB), 200 * MiB, 1.5, CPUS)
    assert rec == Config(cpus=1, mem_bytes=384 * MiB)  # next CPU tier up, 256 MiB x 1.5
    peaky = recommend(Config(cpus=2, mem_bytes=256 * MiB), 400 * MiB, 1.5, CPUS)
    assert peaky == Config(cpus=2, mem_bytes=768 * MiB)  # peak (400) dominates; top CPU stays
    assert round_up_memory(100 * MiB * 1.5) == 192 * MiB
    assert round_up_memory(5000 * MiB) == 6144 * MiB
    assert round_up_memory(9000 * MiB) == 9 * 1024 * MiB


async def test_result_recommendation_uses_peak_from_minimum_run() -> None:
    res = await search(FakeProject(200 * MiB)).execute()
    assert res.min_config_peak_mem_bytes == int(200 * MiB * 0.8)
    assert res.recommended.mem_bytes == 384 * MiB and res.recommended.cpus == 0.5


# ---- classifier -------------------------------------------------------------------------
def sres(**kw) -> SandboxResult:
    return SandboxResult(**{"exit_code": 1, **kw})


@pytest.mark.parametrize(
    ("res", "tail", "expected"),
    [
        (sres(exit_code=0), [], RunOutcome.PASSED),
        (sres(exit_code=1), ["3 failed"], RunOutcome.TEST_FAILED),
        (sres(exit_code=137, oom_killed=True), [], RunOutcome.OOM_KILLED),
        (sres(exit_code=137), [], RunOutcome.OOM_KILLED),
        (sres(exit_code=137, timed_out=True), [], RunOutcome.TIMEOUT),  # our kill, not memory
        (sres(exit_code=1, timed_out=True), [], RunOutcome.TIMEOUT),
        (sres(exit_code=127), ["sh: pytest: not found"], RunOutcome.STARTUP_FAILED),
        (sres(exit_code=126), [], RunOutcome.STARTUP_FAILED),
        (sres(exit_code=None), [], RunOutcome.STARTUP_FAILED),
        (sres(exit_code=1), ["MemoryError"], RunOutcome.OOM_KILLED),
        (
            sres(exit_code=134),
            ["FATAL ERROR: ... JavaScript heap out of memory"],
            RunOutcome.OOM_KILLED,
        ),
        (sres(exit_code=2), ["run.sh: line 3:  42 Killed   python x.py"], RunOutcome.OOM_KILLED),
    ],
)
def test_classification(res, tail, expected) -> None:
    assert classify(res, tail)[0] == expected


def test_make_record_carries_metrics_and_note() -> None:
    r = make_record(
        sres(exit_code=137, wall_s=1.234, peak_mem_bytes=5, cpu_time_s=0.5),
        [],
        cpus=0.5,
        mem_bytes=MEM[3],
        attempt=2,
    )
    assert (r.cpus, r.attempt, r.wall_s, r.peak_mem_bytes, r.cpu_time_s) == (0.5, 2, 1.23, 5, 0.5)
    assert r.outcome == RunOutcome.OOM_KILLED and "137" in r.note


# ---- reports ----------------------------------------------------------------------------
async def test_json_report_roundtrips_with_meta() -> None:
    res = await search(FakeProject(200 * MiB)).execute()
    data = json.loads(to_json(res, project="demo", commit="abc123", stack=None))
    assert data["meta"] == {"project": "demo", "commit": "abc123"}
    assert ProfileResult.model_validate({k: v for k, v in data.items() if k != "meta"}) == res


async def test_markdown_report_has_the_key_sections() -> None:
    res = await search(FakeProject(200 * MiB)).execute()
    md = to_markdown(res, project="demo", commit="abcdef1234567", stack="python")
    assert "# Resource profile: demo" in md and "`abcdef1234`" in md
    assert "**Minimum passing:** 0.25 CPU / 256 MiB" in md
    assert "**Recommended:** 0.5 CPU / 384 MiB" in md
    assert "## Minimum memory by CPU limit" in md and "## All runs" in md
    assert "💥 out of memory" in md and "(re-run 2)" in md


async def test_markdown_handles_failed_and_partial_profiles() -> None:
    failed = to_markdown(await search(FakeProject(10 * 1024 * MiB)).execute())
    assert "**No profile:**" in failed and "## Minimum memory" not in failed
    partial = to_markdown(await search(FakeProject(200 * MiB), max_runs=3).execute())
    assert "partial" in partial


def test_fmt_mem() -> None:
    assert fmt_mem(256 * MiB) == "256 MiB" and fmt_mem(1536 * MiB) == "1.5 GiB"
    assert fmt_mem(2048 * MiB) == "2 GiB" and fmt_mem(None) == "–"


# ---- in-container metrics ----------------------------------------------------------------
def test_metrics_marker_parsing() -> None:
    from thinair.profiler.metrics import parse_metrics

    assert parse_metrics("[thinair-metrics] peak=209715200 cpu_usec=1500000") == (209715200, 1.5)
    assert parse_metrics("[thinair-metrics] peak= cpu_usec=") == (
        None,
        None,
    )  # cgroup v1 / no files
    assert parse_metrics("3 passed in 0.5s") is None


def test_wrapper_keeps_the_exit_status_and_passes_the_command_as_data() -> None:
    from thinair.profiler.metrics import WRAPPER_SCRIPT, wrap_command

    argv = wrap_command("pytest -q; echo $(whoami)")
    assert argv[:3] == ["sh", "-c", WRAPPER_SCRIPT]
    assert (
        argv[-1] == "pytest -q; echo $(whoami)"
    )  # a separate argument: never spliced into the script
    assert "pytest" not in WRAPPER_SCRIPT and "exit $rc" in WRAPPER_SCRIPT
