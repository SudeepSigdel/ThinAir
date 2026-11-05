"""Declarative description of one sandbox container run, with security validation.

Design rule: a spec can only ever mount *named Docker volumes*. Host paths (and therefore the
Docker socket) are impossible to express, so no caller can accidentally widen the sandbox.
"""

import re

from pydantic import BaseModel, Field, field_validator

SANDBOX_USER = "10001:10001"
LABEL_MANAGED = "thinair.managed"
LABEL_JOB = "thinair.job"

_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NETWORK_RE = re.compile(r"^thinair-net-[A-Za-z0-9_.-]{1,100}$")
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt]?)b?$", re.IGNORECASE)
_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def parse_size(value: str | int) -> int:
    """'512m' -> bytes. Accepts ints (bytes) and k/m/g/t suffixes (binary multiples)."""
    if isinstance(value, int):
        return value
    m = _SIZE_RE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid size {value!r}; use e.g. 512m or 2g")
    return int(float(m.group(1)) * _UNITS[m.group(2).lower()])


class VolumeMount(BaseModel):
    volume: str
    path: str
    read_only: bool = False

    @field_validator("volume")
    @classmethod
    def _volume_is_a_name(cls, v: str) -> str:
        if not _VOLUME_RE.match(v):
            raise ValueError("mounts must be named Docker volumes, never host paths")
        return v

    @field_validator("path")
    @classmethod
    def _path_ok(cls, v: str) -> str:
        if not v.startswith("/") or "docker.sock" in v or ".." in v.split("/"):
            raise ValueError(f"invalid container path {v!r}")
        return v


class SandboxSpec(BaseModel):
    image: str
    command: list[str] = Field(min_length=1)  # argv; never passed through a shell
    job_id: str
    workdir: str = "/workspace"
    env: dict[str, str] = Field(default_factory=dict)
    mounts: list[VolumeMount] = Field(default_factory=list)

    cpus: float = Field(default=1.0, gt=0, le=64)
    memory_bytes: int = Field(default=1024**3, ge=4 * 1024**2)
    pids_limit: int = Field(default=256, ge=8)
    timeout_s: float = Field(default=600, gt=0)

    network: bool = False  # off unless a phase explicitly needs it
    # Join this Thinair-made internal network instead (egress via the job's allowlist proxy).
    network_name: str | None = None
    read_only_rootfs: bool = True  # writable paths: mounted volumes and /tmp
    tmpfs_tmp_bytes: int = 256 * 1024**2
    # /tmp is noexec by default; the install phase turns this off (installers may run helpers).
    tmp_noexec: bool = True

    # Values to mask in captured output (e.g. tokens passed via env).
    redact: list[str] = Field(default_factory=list)
    max_output_bytes: int = 5 * 1024**2

    @field_validator("network_name")
    @classmethod
    def _network_name(cls, v: str | None) -> str | None:
        if v is not None and not NETWORK_RE.match(v):
            raise ValueError("network_name must be a Thinair network (thinair-net-...)")
        return v

    @field_validator("env")
    @classmethod
    def _env_keys(cls, v: dict[str, str]) -> dict[str, str]:
        bad = [k for k in v if not _ENV_KEY_RE.match(k)]
        if bad:
            raise ValueError(f"invalid environment variable names: {bad}")
        return v


class SandboxResult(BaseModel):
    exit_code: int | None = None
    oom_killed: bool = False
    timed_out: bool = False
    cancelled: bool = False
    wall_s: float = 0.0
    peak_mem_bytes: int | None = None  # sampled ~1/s; may miss very short spikes
    cpu_time_s: float | None = None
    output_truncated: bool = False


class SandboxError(RuntimeError):
    """The sandbox could not be created or started (missing image, Docker down, ...)."""
