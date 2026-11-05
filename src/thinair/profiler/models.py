"""Data shapes produced by the profiler (also what is stored and exported as JSON)."""

from typing import Literal

from pydantic import BaseModel, Field

from thinair.db.models import RunOutcome

__all__ = ["Config", "CpuLevel", "ProfileResult", "RunOutcome", "RunRecord"]


class Config(BaseModel):
    cpus: float
    mem_bytes: int


class RunRecord(BaseModel):
    """One test run under one resource limit."""

    cpus: float
    mem_bytes: int
    outcome: RunOutcome
    attempt: int = 1  # >1 for stability re-runs at the boundary
    exit_code: int | None = None
    oom_killed: bool = False
    peak_mem_bytes: int | None = None
    cpu_time_s: float | None = None
    wall_s: float | None = None
    note: str | None = None  # why this label was chosen, when not obvious
    output_tail: list[str] = Field(default_factory=list)  # last lines of output; failures only

    @property
    def passed(self) -> bool:
        return self.outcome == RunOutcome.PASSED


class CpuLevel(BaseModel):
    """Search outcome for one CPU limit."""

    cpus: float
    status: Literal["found", "infeasible", "skipped"]
    min_mem_bytes: int | None = None
    flaky: bool = False  # the first minimum failed a stability re-run, so a larger tier was used
    reason: str | None = None  # for infeasible/skipped levels


class ProfileResult(BaseModel):
    status: Literal["ok", "baseline_failed", "incomplete"]
    headroom: float = 1.5
    baseline: RunRecord | None = None
    levels: list[CpuLevel] = Field(default_factory=list)
    runs: list[RunRecord] = Field(default_factory=list)
    min_config: Config | None = None  # lowest CPU that passes, with its minimum memory
    recommended: Config | None = None  # min_config plus headroom, rounded to a usual size
    min_config_peak_mem_bytes: int | None = None
    notes: list[str] = Field(default_factory=list)
