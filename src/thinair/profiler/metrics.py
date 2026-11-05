"""Accurate peak-memory / CPU-time measurement from inside the sandbox.

Docker's stats stream is sampled about once a second, which misses short test runs entirely.
Instead the test command is wrapped so that, right after it finishes, the container's own cgroup
counters are printed on a marker line (cgroup v2: `memory.peak`, `cpu.stat`). The wrapper keeps
the test's exit status, and the marker line is stripped from the output shown to users.

`memory.peak` includes file cache, so it can read slightly above the process's resident memory.
That is the figure the kernel enforces limits against, which is what we want to size for.
"""

import re

MARKER = "[thinair-metrics]"

# $1 is the project's test command. Nothing from the repo is interpolated into this script.
WRAPPER_SCRIPT = (
    'sh -c "$1"; rc=$?; '
    "p=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null); "
    "u=$(sed -n 's/^usage_usec //p' /sys/fs/cgroup/cpu.stat 2>/dev/null); "
    f'echo "{MARKER} peak=$p cpu_usec=$u"; exit $rc'
)

_LINE = re.compile(re.escape(MARKER) + r"\s+peak=(\d*)\s+cpu_usec=(\d*)")


def wrap_command(test_command: str) -> list[str]:
    return ["sh", "-c", WRAPPER_SCRIPT, "thinair-run", test_command]


def parse_metrics(line: str) -> tuple[int | None, float | None] | None:
    """Return (peak_mem_bytes, cpu_time_s) if `line` is the marker line, else None."""
    m = _LINE.search(line)
    if not m:
        return None
    peak = int(m.group(1)) if m.group(1) else None
    cpu = int(m.group(2)) / 1e6 if m.group(2) else None
    return peak, cpu
