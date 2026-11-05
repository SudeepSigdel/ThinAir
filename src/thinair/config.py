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
    # Commit author for pull requests opened from agent jobs (use your GitHub noreply email
    # to have commits linked to your account).
    git_author_name: str = Field(default="Thinair", pattern=r"^[^\r\n<>]{1,100}$")
    git_author_email: str = Field(default="thinair@localhost", pattern=r"^[^\s<>@]+@[^\s<>@]+$")

    # Agent runs whose config lists `egress` hosts can reach only those (via a per-run proxy).
    # "open" turns the allowlist off. Extra hosts (comma-separated) are added for every agent,
    # e.g. registry.npmjs.org,pypi.org,files.pythonhosted.org if agents should install packages.
    agent_egress: Literal["allowlist", "open"] = "allowlist"
    agent_egress_extra_hosts: str = ""

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
