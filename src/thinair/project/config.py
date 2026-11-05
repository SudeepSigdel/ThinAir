"""`thinair.yaml`: optional per-repo settings, validated with readable error messages."""

import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from thinair.sandbox.spec import parse_size

MAX_CONFIG_BYTES = 64 * 1024


class ProjectConfigError(ValueError):
    """thinair.yaml is unreadable or invalid; the message is safe to show to the user."""


class _Strict(BaseModel):
    # Unknown keys are errors, so typos (and not-yet-supported options) fail loudly.
    model_config = ConfigDict(extra="forbid")


class Timeouts(_Strict):
    install_s: int = Field(600, gt=0)
    test_s: int = Field(300, gt=0)
    agent_s: int = Field(1800, gt=0)


class Network(_Strict):
    install: bool = True
    test: bool = False
    agent: bool = True


class ProfileTiers(_Strict):
    cpus: list[float] = [2, 1, 0.5, 0.25]
    memory: list[int] = [parse_size(m) for m in ("2g", "1g", "512m", "256m", "128m")]
    headroom: float = Field(1.5, ge=1.0, le=10.0)
    confirm_runs: int = Field(2, ge=1, le=5)

    @field_validator("cpus")
    @classmethod
    def _cpus(cls, v: list[float]) -> list[float]:
        if not v or any(c <= 0 for c in v):
            raise ValueError("must be a non-empty list of positive numbers")
        return sorted(set(v), reverse=True)

    @field_validator("memory", mode="before")
    @classmethod
    def _memory(cls, v: list[str | int]) -> list[int]:
        if not isinstance(v, list) or not v:
            raise ValueError("must be a non-empty list like [1g, 512m]")
        sizes = sorted({parse_size(x) for x in v}, reverse=True)
        if sizes[-1] < 16 * 1024**2:
            raise ValueError("smallest memory tier must be at least 16m")
        return sizes


class ProjectConfig(_Strict):
    version: Literal[1] = 1
    stack: Literal["auto", "python", "node"] = "auto"
    install: str | None = None  # shell command run in the sandbox (network on by default)
    test: str | None = None
    env: dict[str, str] = Field(default_factory=dict)  # non-secret values only
    workdir: str = "."  # relative to the repo root
    timeouts: Timeouts = Timeouts()
    network: Network = Network()
    profile: ProfileTiers = ProfileTiers()

    @field_validator("workdir")
    @classmethod
    def _workdir(cls, v: str) -> str:
        if v.startswith("/") or ".." in v.replace("\\", "/").split("/"):
            raise ValueError("must be a relative path inside the repository")
        return v

    @field_validator("env", mode="before")
    @classmethod
    def _env(cls, v: object) -> dict[str, str]:
        if not isinstance(v, dict):
            raise ValueError("must be a mapping of NAME: value")
        bad = [k for k in v if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k))]
        if bad:
            raise ValueError(f"invalid variable names: {bad}")
        # YAML turns `N: 3` / `DEBUG: true` into numbers/bools; environment values are strings.
        return {
            str(k): str(val).lower() if isinstance(val, bool) else str(val) for k, val in v.items()
        }


def parse_project_config(text: str | None) -> ProjectConfig:
    """Parse thinair.yaml content. `None`/empty means 'use defaults'."""
    if text is None or not text.strip():
        return ProjectConfig()
    if len(text.encode()) > MAX_CONFIG_BYTES:
        raise ProjectConfigError("thinair.yaml is too large (limit 64 KB)")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ProjectConfigError(f"thinair.yaml is not valid YAML: {e}") from e
    if data is None:
        return ProjectConfig()
    if not isinstance(data, dict):
        raise ProjectConfigError("thinair.yaml must be a mapping of settings")
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'file'}: {err['msg']}" for err in e.errors()
        )
        raise ProjectConfigError(f"Invalid thinair.yaml: {problems}") from e
