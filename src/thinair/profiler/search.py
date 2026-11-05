"""Find the smallest CPU/memory limits under which a project's tests still pass.

Brute force over 4 CPU x 5 memory tiers is 20 runs. This does it in roughly 4 + 1 per extra CPU
level (plus stability re-runs), using two assumptions that are stated in the report:

1. More memory never makes a passing run fail (monotonic in memory).
2. Less CPU never reduces the memory a project needs, so the minimum memory at a lower CPU is
   at least the minimum at a higher CPU. That lets each level start from the previous answer.

The search is independent of Docker: it is given a `run(cpus, mem_bytes, timeout_s)` coroutine
(Step 7 wires in the real sandbox; tests use a fake).
"""

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from thinair.profiler.models import Config, CpuLevel, ProfileResult, RunOutcome, RunRecord

RunFn = Callable[[float, int, float], Awaitable[RunRecord]]
ProgressFn = Callable[[RunRecord], None]
LogFn = Callable[[str], None]

# Memory sizes people actually provision, in MiB; recommendations are rounded up to these.
_STANDARD_MIB = (64, 96, 128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192)
_MIB = 1024**2
# A run at low CPU may legitimately take longer; allow up to this many times the base timeout.
_MAX_TIMEOUT_FACTOR = 4


class _BudgetExhausted(Exception):
    pass


def round_up_memory(n_bytes: float) -> int:
    mib = math.ceil(n_bytes / _MIB)
    for size in _STANDARD_MIB:
        if size >= mib:
            return size * _MIB
    return math.ceil(mib / 1024) * 1024 * _MIB


def timeout_for(cpus: float, top_cpus: float, base_timeout_s: float) -> float:
    """Tests get longer at lower CPU limits, so scale the timeout (capped)."""
    return base_timeout_s * min(max(1.0, top_cpus / cpus), _MAX_TIMEOUT_FACTOR)


def recommend(
    min_cfg: Config, peak_mem_bytes: int | None, headroom: float, cpu_tiers: list[float]
) -> Config:
    """Minimum plus headroom: memory is rounded up to a usual size, and CPU goes up one tier.

    Memory need does not shrink with more CPU (assumption 2), so the larger CPU tier is safe.
    """
    base = max(min_cfg.mem_bytes, peak_mem_bytes or 0)
    higher = [c for c in cpu_tiers if c > min_cfg.cpus]
    cpus = min(higher) if higher else min_cfg.cpus
    return Config(cpus=cpus, mem_bytes=round_up_memory(base * headroom))


@dataclass
class _State:
    result: ProfileResult
    cache: dict[tuple[float, int, int], RunRecord]


class ProfileSearch:
    def __init__(
        self,
        run: RunFn,
        cpus: list[float],
        memory: list[int],
        *,
        base_timeout_s: float = 300,
        headroom: float = 1.5,
        confirm_runs: int = 2,
        max_runs: int = 40,
        on_run: ProgressFn | None = None,
    ) -> None:
        if not cpus or not memory:
            raise ValueError("need at least one CPU tier and one memory tier")
        self.cpus = sorted(cpus, reverse=True)
        self.mem = sorted(memory, reverse=True)  # index 0 = most memory
        self._run = run
        self._timeout = base_timeout_s
        self._headroom = headroom
        self._confirm = confirm_runs
        self._max_runs = max_runs
        self._on_run = on_run
        self._st = _State(ProfileResult(status="ok", headroom=headroom), {})

    # ---- public ------------------------------------------------------------------------
    async def execute(self) -> ProfileResult:
        try:
            await self._search()
        except _BudgetExhausted:
            self._st.result.status = "incomplete"
            self._st.result.notes.append(
                f"Stopped after {self._max_runs} runs (limit); results below are partial."
            )
        self._finalise()
        return self._st.result

    # ---- internals ---------------------------------------------------------------------
    async def _try(self, ci: int, mi: int, attempt: int = 1) -> RunRecord:
        """Run (or reuse) the test at cpu tier `ci` and memory tier `mi`."""
        cpus, mem = self.cpus[ci], self.mem[mi]
        key = (cpus, mem, attempt)
        if key in self._st.cache:
            return self._st.cache[key]
        if len(self._st.result.runs) >= self._max_runs:
            raise _BudgetExhausted
        rec = await self._run(cpus, mem, timeout_for(cpus, self.cpus[0], self._timeout))
        rec.attempt = attempt
        self._st.cache[key] = rec
        self._st.result.runs.append(rec)
        if self._on_run:
            self._on_run(rec)
        return rec

    async def _search(self) -> None:
        res = self._st.result
        baseline = await self._try(0, 0)
        res.baseline = baseline
        if not baseline.passed:
            res.status = "baseline_failed"
            res.notes.append(_baseline_message(baseline))
            return

        prev_min: int | None = None  # memory index of the previous level's minimum
        dead = False  # a higher CPU level already failed everywhere
        for ci, cpus in enumerate(self.cpus):
            if dead:
                res.levels.append(
                    CpuLevel(
                        cpus=cpus,
                        status="skipped",
                        reason="a higher CPU level already failed at every memory size",
                    )
                )
                continue
            found = await self._find_min(ci, prev_min)
            if found is None:
                top = self._st.cache[(cpus, self.mem[0], 1)]
                res.levels.append(
                    CpuLevel(
                        cpus=cpus,
                        status="infeasible",
                        reason=(
                            f"{top.outcome.value} even with the most memory ({_fmt(self.mem[0])})"
                        ),
                    )
                )
                dead = True
                continue
            idx, flaky = await self._confirm_min(ci, found)
            if idx is None:
                res.levels.append(
                    CpuLevel(
                        cpus=cpus,
                        status="infeasible",
                        flaky=True,
                        reason="results were inconsistent even at the largest memory tier",
                    )
                )
                dead = True
                continue
            res.levels.append(
                CpuLevel(cpus=cpus, status="found", min_mem_bytes=self.mem[idx], flaky=flaky)
            )
            prev_min = idx

    async def _find_min(self, ci: int, prev_min: int | None) -> int | None:
        """Index of the smallest passing memory tier at this CPU level, or None."""
        if prev_min is None:  # highest CPU level: baseline already proved index 0 passes
            lo, hi = 0, len(self.mem) - 1
        else:
            if (await self._try(ci, prev_min)).passed:  # assumption 2: nothing smaller passes
                return prev_min
            if prev_min == 0 or not (await self._try(ci, 0)).passed:
                return None
            lo, hi = 0, prev_min - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if (await self._try(ci, mid)).passed:
                lo = mid
            else:
                hi = mid - 1
        return lo

    async def _confirm_min(self, ci: int, idx: int) -> tuple[int | None, bool]:
        """Re-run the boundary; if it ever fails, step up one memory tier and re-check."""
        flaky = False
        while idx >= 0:
            stable = True
            for attempt in range(1, self._confirm + 1):
                if not (await self._try(ci, idx, attempt)).passed:
                    stable = False
                    break
            if stable:
                return idx, flaky
            flaky = True
            idx -= 1
        return None, True

    def _finalise(self) -> None:
        res = self._st.result
        feasible = [lv for lv in res.levels if lv.status == "found" and lv.min_mem_bytes]
        if not feasible:
            return
        best = min(feasible, key=lambda lv: lv.cpus)  # lowest CPU that still passes
        assert best.min_mem_bytes is not None
        res.min_config = Config(cpus=best.cpus, mem_bytes=best.min_mem_bytes)
        peaks = [
            r.peak_mem_bytes
            for r in res.runs
            if r.passed and r.cpus == best.cpus and r.mem_bytes == best.min_mem_bytes
            if r.peak_mem_bytes
        ]
        res.min_config_peak_mem_bytes = max(peaks) if peaks else None
        res.recommended = recommend(
            res.min_config, res.min_config_peak_mem_bytes, self._headroom, self.cpus
        )
        if any(lv.flaky for lv in res.levels):
            res.notes.append(
                "Some results were inconsistent at the boundary, so a larger memory size was used. "
                "The tests may be flaky or sensitive to memory pressure."
            )
        floor = self.mem[-1]
        if res.min_config.mem_bytes == floor:
            res.notes.append(
                f"Tests passed at the smallest memory tier tested ({_fmt(floor)}); "
                "the true minimum may be lower. Add smaller tiers under `profile.memory`."
            )
        if res.min_config.cpus == self.cpus[-1]:
            res.notes.append(
                f"Tests passed at the smallest CPU tier tested ({res.min_config.cpus:g}); "
                "the true minimum may be lower. Add smaller tiers under `profile.cpus`."
            )


def _fmt(n: int) -> str:
    return f"{n / 1024**3:g} GiB" if n >= 1024**3 else f"{n / _MIB:g} MiB"


def _baseline_message(rec: RunRecord) -> str:
    by_outcome = {
        RunOutcome.TEST_FAILED: "The tests fail even with the most resources, so there is nothing "
        "to profile. Fix the failing tests first (or set `test:` in thinair.yaml).",
        RunOutcome.OOM_KILLED: "The tests ran out of memory even at the largest tier "
        f"({_fmt(rec.mem_bytes)}). Add a larger value under `profile.memory`.",
        RunOutcome.TIMEOUT: "The tests timed out even with the most resources. "
        "Raise `timeouts.test_s` in thinair.yaml.",
        RunOutcome.STARTUP_FAILED: "The test command could not be started. Check `test:` in "
        "thinair.yaml and that the install step succeeded.",
    }
    return by_outcome.get(rec.outcome, f"The baseline run failed ({rec.outcome.value}).")
