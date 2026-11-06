"""Database tables. SQLite today; plain SQLModel/SQLAlchemy types keep Postgres an easy upgrade."""

import secrets
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, LargeBinary
from sqlmodel import Field, SQLModel


def new_id() -> str:
    """Time-sortable unique id (ms timestamp + random), 22 hex chars. No extra dependency."""
    return f"{int(time.time() * 1000):012x}{secrets.token_hex(5)}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobKind(StrEnum):
    AGENT = "agent"
    PROFILE = "profile"
    PR = "pr"  # open a pull request from a finished agent job's patch


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class EventType(StrEnum):
    LOG = "log"
    STATUS = "status"
    AGENT_MSG = "agent_msg"
    PROFILE_RUN = "profile_run"
    ERROR = "error"


class RunOutcome(StrEnum):
    PASSED = "passed"
    TEST_FAILED = "test_failed"
    OOM_KILLED = "oom_killed"
    TIMEOUT = "timeout"
    STARTUP_FAILED = "startup_failed"


class User(SQLModel, table=True):
    """Someone who can sign in. Admins see every job and manage users; members see only their
    own jobs. The bootstrap owner is the user named `admin` (OWNER_NAME in thinair.users)."""

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(index=True, unique=True)
    is_admin: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    disabled_at: datetime | None = None


class Repo(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    url: str = Field(index=True, unique=True)
    name: str
    default_branch: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    repo_id: str = Field(foreign_key="repo.id", index=True)
    kind: JobKind
    status: JobStatus = Field(default=JobStatus.QUEUED, index=True)
    agent_id: str | None = None
    task: str | None = None
    ref: str | None = None
    commit_sha: str | None = None
    # For `pr` jobs: the agent job whose patch is turned into a pull request.
    parent_job_id: str | None = Field(default=None, foreign_key="job.id", index=True)
    # Who the job belongs to (and acts as: agent login, GitHub token policy). None = the owner.
    user_id: str | None = Field(default=None, foreign_key="user.id", index=True)
    limits: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobEvent(SQLModel, table=True):
    """Append-only event log; `seq` is per job and doubles as the SSE `Last-Event-ID`."""

    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    seq: int
    ts: datetime = Field(default_factory=utcnow)
    type: EventType
    stream: str | None = None  # stdout | stderr | system
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class ProfileRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    cpus: float
    mem_bytes: int
    outcome: RunOutcome
    exit_code: int | None = None
    oom_killed: bool = False
    peak_mem_bytes: int | None = None
    cpu_time_s: float | None = None
    wall_s: float | None = None
    attempt: int = 1


class ProfileReport(SQLModel, table=True):
    job_id: str = Field(foreign_key="job.id", primary_key=True)
    min_config: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    recommended: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    report: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    report_md: str = ""


class JobResult(SQLModel, table=True):
    job_id: str = Field(foreign_key="job.id", primary_key=True)
    exit_status: int | None = None
    summary: str | None = None
    diff_text: str | None = None  # size-capped by the runner
    diff_stat: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Exact `git diff --binary` against the job's commit_sha, for opening a pull request.
    # None when there were no changes or the patch exceeded the size limit.
    patch: bytes | None = Field(default=None, sa_column=Column(LargeBinary))
    pr_url: str | None = None


class ApiToken(SQLModel, table=True):
    """Only the SHA-256 of a token is stored; the plaintext is shown once at creation."""

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    token_hash: str = Field(index=True, unique=True)
    user_id: str | None = Field(default=None, foreign_key="user.id", index=True)  # None = owner
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
