"""Runtime configuration, read from THINAIR_* environment variables (see .env.example)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="THINAIR_", env_file=".env", extra="ignore")

    data_dir: Path = Path("./data")
    host: str = "0.0.0.0"  # noqa: S104  (inside a container; compose controls exposure)
    port: int = 8080
    log_level: str = "info"
    # Keep at 1 on small VMs: two concurrent sandboxes will thrash a 1 GB machine.
    max_concurrent_jobs: int = 1
    # Docker endpoint. Compose points this at the socket proxy, never the raw socket.
    docker_host: str | None = None

    # Whole-job time limit (clone + install + all test runs, or the agent run).
    job_timeout_s: int = 3600
    # Remove leftover sandbox containers/volumes from a previous crash when the server starts.
    sweep_on_start: bool = True

    # Optional fixed admin token (>= 20 chars). If unset, one is generated on first start.
    admin_token: str | None = None

    # Optional package mirrors for the sandbox install step (useful on restricted networks).
    pip_index_url: str | None = None
    npm_registry: str | None = None

    # Optional GitHub token for cloning private repositories (see thinair/github.py). Given
    # only to the clone container; never stored, logged or passed to install/test/agent runs.
    github_token: SecretStr | None = None
    github_token_file: Path | None = None  # alternative for Docker secrets
    # Whether jobs of *member* users (not admins) may use that token: private clones and pull
    # requests as the token's GitHub account. Off by default (members: public repositories only).
    github_token_for_members: bool = False
    # Commit author for pull requests opened from agent jobs (use your GitHub noreply email
    # to have commits linked to your account).
    git_author_name: str = Field(default="Thinair", pattern=r"^[^\r\n<>]{1,100}$")
    git_author_email: str = Field(default="thinair@localhost", pattern=r"^[^\s<>@]+@[^\s<>@]+$")

    # Agent runs whose config lists `egress` hosts can reach only those (via a per-run proxy).
    # "open" turns the allowlist off. Extra hosts (comma-separated) are added for every agent,
    # e.g. registry.npmjs.org,pypi.org,files.pythonhosted.org if agents should install packages.
    agent_egress: Literal["allowlist", "open"] = "allowlist"
    agent_egress_extra_hosts: str = ""

    # Unfinished (queued or running) jobs a *member* may have at once; 0 = no limit. Admins are
    # never limited. Override per person: `thinair user limit <name> <n>`.
    member_job_limit: int = Field(default=5, ge=0)

    # Log lines of finished jobs are deleted after this many days (0 = keep forever). Results,
    # profile reports, status history and agent messages are kept.
    log_retention_days: int = Field(default=30, ge=0)

    # Agent conversations (jobs started with "keep the conversation open"): how long the agent
    # waits for your next message or an approval before it finishes the job. The sandbox (and a
    # job slot) stays in use while it waits.
    agent_idle_s: int = Field(default=600, ge=30, le=86400)

    # Keep package downloads (pip/uv/npm/yarn caches) per project and user between jobs, so
    # repeat jobs install without re-downloading. `thinair cache clear` frees the space.
    dependency_cache: bool = True

    # Remote runners (other machines that run profile jobs; see thinair/runners.py):
    # auto = a profile job goes to a runner if one is waiting for work, else runs here (and comes
    #        back here if no runner takes it within runner_claim_timeout_s);
    # runners = profile jobs only ever run on runners (they wait for one); local = never.
    runner_placement: Literal["auto", "runners", "local"] = "auto"
    runner_claim_timeout_s: float = 60
    runner_lease_s: float = 90  # a runner silent this long loses its job (runner_lost)

    # Optional Telegram notifications when a job finishes (outbound only; see thinair/notify.py).
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    # The address you open Thinair at (e.g. https://thinair.example.com), for links in messages.
    public_url: str | None = Field(default=None, pattern=r"^(https?://[^\s]+)?$")

    @field_validator("github_token_file", mode="before")
    @classmethod
    def _blank_path_is_unset(cls, v: object) -> object:
        # `THINAIR_GITHUB_TOKEN_FILE=` (blank, as in .env.example) would otherwise become Path(".").
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("git_author_name", "git_author_email", mode="before")
    @classmethod
    def _blank_author_is_default(cls, v: object, info) -> object:
        # A blank value in .env must not stop the server from starting.
        if isinstance(v, str) and not v.strip():
            return cls.model_fields[info.field_name].default
        return v.strip() if isinstance(v, str) else v

    # Optional external database, e.g. postgresql+psycopg://user:pass@host/thinair (needs the
    # `postgres` extra). Default: one SQLite file in data_dir, which suits a single small server.
    database_url: SecretStr | None = None

    @property
    def db_url(self) -> str:
        if self.database_url and self.database_url.get_secret_value().strip():
            return self.database_url.get_secret_value().strip()
        return f"sqlite:///{self.data_dir / 'thinair.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
