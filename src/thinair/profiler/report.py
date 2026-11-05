"""Render a `ProfileResult` as JSON (machines) and Markdown (people, phones)."""

from thinair.profiler.models import Config, ProfileResult, RunRecord

_LABELS = {
    "passed": "✅ passed",
    "test_failed": "❌ test failure",
    "oom_killed": "💥 out of memory",
    "timeout": "⏱ timeout",
    "startup_failed": "🚫 startup failed",
}


def fmt_mem(n: int | None) -> str:
    if n is None:
        return "–"
    if n >= 1024**3:
        return f"{n / 1024**3:.2f}".rstrip("0").rstrip(".") + " GiB"
    return f"{n / 1024**2:.0f} MiB"


def fmt_cfg(c: Config) -> str:
    return f"{c.cpus:g} CPU / {fmt_mem(c.mem_bytes)}"


def to_json(result: ProfileResult, **meta: str | None) -> str:
    """Pretty JSON of the result; `meta` (project, commit, stack...) is added under `meta`."""
    import json

    data = result.model_dump(mode="json")
    data["meta"] = {k: v for k, v in meta.items() if v}
    return json.dumps(data, indent=2)


def to_markdown(
    result: ProfileResult,
    *,
    project: str | None = None,
    commit: str | None = None,
    stack: str | None = None,
) -> str:
    out: list[str] = [f"# Resource profile{f': {project}' if project else ''}", ""]
    facts = [
        x
        for x in (f"Stack: {stack}" if stack else "", f"Commit: `{commit[:10]}`" if commit else "")
        if x
    ]
    if facts:
        out += [" · ".join(facts), ""]

    out += ["## Result", ""]
    if result.status == "baseline_failed":
        out += [f"**No profile:** {n}" for n in result.notes] or ["**No profile.**"]
        if result.baseline and result.baseline.output_tail:
            out += [
                "",
                "Last output of the failing run:",
                "",
                "```",
                *result.baseline.output_tail,
                "```",
            ]
    elif result.min_config is None:
        out += ["**No passing configuration found.**"]
    else:
        out += [f"- **Minimum passing:** {fmt_cfg(result.min_config)}"]
        if result.recommended:
            out += [
                f"- **Recommended:** {fmt_cfg(result.recommended)} "
                f"(minimum with {result.headroom:g}× headroom)"
            ]
        if result.min_config_peak_mem_bytes:
            peak = fmt_mem(result.min_config_peak_mem_bytes)
            out += [f"- Peak memory observed at the minimum: {peak}"]
    if result.status == "incomplete":
        out += ["", "> ⚠️ The run limit was reached; this result is partial."]
    out += [""]

    if result.levels:
        out += [
            "## Minimum memory by CPU limit",
            "",
            "| CPUs | Minimum memory | Status |",
            "|---|---|---|",
        ]
        for lv in result.levels:
            status = {"found": "passes", "infeasible": "fails", "skipped": "not run"}[lv.status]
            if lv.reason:
                status += f" ({lv.reason})"
            if lv.flaky:
                status += " ⚠ flaky"
            out.append(f"| {lv.cpus:g} | {fmt_mem(lv.min_mem_bytes)} | {status} |")
        out += [""]

    if result.runs:
        out += [
            "## All runs",
            "",
            "| # | CPUs | Memory | Result | Peak mem | CPU time | Wall |",
            "|---|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(result.runs, 1):
            out.append(_run_row(i, r))
        out += [""]

    if result.status != "baseline_failed" and result.notes:
        out += ["## Notes", ""] + [f"- {n}" for n in result.notes] + [""]

    out += [
        "## How to read this",
        "",
        "Each run executes the project's tests inside a container with hard CPU and memory limits "
        "and no swap. Memory is searched per CPU level assuming that more memory never hurts and "
        "that less CPU never reduces the memory needed. The minimum is re-run to check it is "
        "stable. Peak memory is sampled about once a second and can miss short spikes.",
        "",
    ]
    return "\n".join(out)


def _run_row(i: int, r: RunRecord) -> str:
    label = _LABELS.get(r.outcome.value, r.outcome.value)
    if r.attempt > 1:
        label += f" (re-run {r.attempt})"
    cpu_t = f"{r.cpu_time_s:.1f}s" if r.cpu_time_s is not None else "–"
    wall = f"{r.wall_s:.1f}s" if r.wall_s is not None else "–"
    cells = [str(i), f"{r.cpus:g}", fmt_mem(r.mem_bytes), label, fmt_mem(r.peak_mem_bytes)]
    return "| " + " | ".join([*cells, cpu_t, wall]) + " |"
