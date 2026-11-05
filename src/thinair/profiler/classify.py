"""Turn a finished sandbox run into one of the profiler's failure-mode labels."""

import re
from collections.abc import Sequence

from thinair.db.models import RunOutcome
from thinair.profiler.models import RunRecord
from thinair.sandbox.spec import SandboxResult

# Memory exhaustion that does not always trigger a kernel OOM kill: language runtimes that
# watch the cgroup limit abort themselves and exit with an ordinary error code.
_MEMORY_MARKERS = (
    "memoryerror",
    "cannot allocate memory",
    "heap out of memory",
    "std::bad_alloc",
    "java.lang.outofmemoryerror",
    "out of memory",
)


# The shell reports a SIGKILLed child as e.g. "run.sh: line 3:    42 Killed    python x.py".
_SHELL_KILLED = re.compile(r"(^|\s)\d+\s+Killed|^\s*Killed\s*$")


def classify(res: SandboxResult, output_tail: Sequence[str] = ()) -> tuple[RunOutcome, str | None]:
    """Return (outcome, note). The note explains non-obvious labels."""
    if res.timed_out:
        return RunOutcome.TIMEOUT, None
    if res.oom_killed:
        return RunOutcome.OOM_KILLED, None
    if res.exit_code == 0:
        return RunOutcome.PASSED, None
    if res.exit_code is None or res.exit_code in (126, 127):
        return (
            RunOutcome.STARTUP_FAILED,
            "command could not be started (not found or not executable)",
        )
    if res.exit_code == 137:
        return (
            RunOutcome.OOM_KILLED,
            "exit 137 (SIGKILL) without a timeout; treated as out-of-memory",
        )
    for line in output_tail:
        low = line.lower()
        if any(m in low for m in _MEMORY_MARKERS) or _SHELL_KILLED.search(line):
            return RunOutcome.OOM_KILLED, "memory exhaustion reported in the output"
    return RunOutcome.TEST_FAILED, None


def make_record(
    res: SandboxResult,
    output_tail: Sequence[str],
    *,
    cpus: float,
    mem_bytes: int,
    attempt: int = 1,
) -> RunRecord:
    outcome, note = classify(res, output_tail)
    return RunRecord(
        cpus=cpus,
        mem_bytes=mem_bytes,
        outcome=outcome,
        attempt=attempt,
        exit_code=res.exit_code,
        oom_killed=res.oom_killed,
        peak_mem_bytes=res.peak_mem_bytes,
        cpu_time_s=res.cpu_time_s,
        wall_s=round(res.wall_s, 2),
        note=note,
        output_tail=[] if outcome == RunOutcome.PASSED else [ln[:300] for ln in output_tail[-10:]],
    )
