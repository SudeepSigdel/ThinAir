"""Runtime configuration, read from THINAIR_* environment variables (see .env.example)."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
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

    @field_validator("github_token_file", mode="before")
    @classmethod
    def _blank_path_is_unset(cls, v: object) -> object:
        # `THINAIR_GITHUB_TOKEN_FILE=` (blank, as in .env.example) would otherwise become Path(".").
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'thinair.db'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
