"""Agent configs: one YAML file per coding-agent CLI. Adding an agent needs a file, not code.

Built-in configs live in `agents/builtin/`. Users add or override agents by dropping
`<id>.yaml` into `$THINAIR_DATA_DIR/agents/` (a user file with the same id wins).
"""

import logging
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from thinair.sandbox.spec import parse_size

log = logging.getLogger("thinair.agents")
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
TASK = "{task}"
_ID = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


class AgentConfigError(ValueError):
    """An agent YAML file is invalid; the message names the file and the problem."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthConfig(_Strict):
    # The agent's persistent login lives in Docker volume `thinair-auth-<id>`, mounted here.
    config_dir: str = "/thinair-auth"
    login_command: list[str] = Field(min_length=1)  # run interactively by `thinair auth <id>`
    check_command: list[str] | None = None  # exit 0 = logged in
    # Host environment variables forwarded into the agent container (e.g. a token you generated
    # yourself). Their values are masked in all output.
    env_passthrough: list[str] = Field(default_factory=list)
    hint: str = "Log in with `thinair auth {id}`."
    login_help: str = "Complete the agent's login, then exit it."  # shown by `thinair auth`

    @field_validator("config_dir")
    @classmethod
    def _abs(cls, v: str) -> str:
        if not v.startswith("/") or ".." in v.split("/"):
            raise ValueError("must be an absolute path inside the container")
        return v

    @field_validator("env_passthrough")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        bad = [n for n in v if not _ENV.match(n)]
        if bad:
            raise ValueError(f"invalid variable names: {bad}")
        return v


class Limits(_Strict):
    cpus: float = Field(2, gt=0, le=64)
    memory: int = 2 * 1024**3
    pids: int = Field(1024, ge=64)
    timeout_s: int = Field(1800, gt=0)

    @field_validator("memory", mode="before")
    @classmethod
    def _mem(cls, v: str | int) -> int:
        return parse_size(v)


class AgentConfig(_Strict):
    id: str
    name: str
    version: str | None = None  # informational; pin the real version in `install`
    install: str  # shell command run as root while building the agent image
    needs_node: bool = False  # copy a Node.js runtime into non-Node project images
    command: list[str] = Field(min_length=1)  # argv; "{task}" must appear once, as its own item
    env: dict[str, str] = Field(default_factory=dict)
    auth: AuthConfig
    output: Literal["stream-json", "text"] = "text"
    auth_error_patterns: list[str] = Field(default_factory=list)  # case-insensitive substrings
    limits: Limits = Limits()
    network: bool = True  # agents must reach their model provider

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError("must be lowercase letters, digits and dashes, starting with a letter")
        return v

    @field_validator("command")
    @classmethod
    def _command(cls, v: list[str]) -> list[str]:
        if v.count(TASK) != 1:
            raise ValueError('must contain "{task}" exactly once, as a separate list item')
        for arg in v:
            if arg != TASK and _PLACEHOLDER.search(arg):
                raise ValueError(f'unknown placeholder in {arg!r}; only "{{task}}" is supported')
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        bad = [k for k in v if not _ENV.match(k)]
        if bad:
            raise ValueError(f"invalid variable names: {bad}")
        return {k: str(x) for k, x in v.items()}

    @property
    def auth_volume(self) -> str:
        return f"thinair-auth-{self.id}"

    @property
    def auth_hint(self) -> str:
        return self.auth.hint.replace("{id}", self.id)

    def argv(self, task: str) -> list[str]:
        """The command with the task as one argument. A task starting with '-' gets a leading
        space so the agent's option parser can never read it as a flag."""
        safe = f" {task}" if task.startswith("-") else task
        return [safe if a == TASK else a for a in self.command]


def parse_agent(text: str, source: str = "<string>") -> AgentConfig:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise AgentConfigError(f"{source}: not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise AgentConfigError(f"{source}: must be a mapping of settings")
    try:
        return AgentConfig.model_validate(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'file'}: {err['msg']}" for err in e.errors()
        )
        raise AgentConfigError(f"{source}: {problems}") from e


def load_agents(user_dir: Path | None = None) -> tuple[dict[str, AgentConfig], list[str]]:
    """Load built-in then user configs. Returns (agents by id, error messages for bad files)."""
    agents: dict[str, AgentConfig] = {}
    errors: list[str] = []
    for folder in (BUILTIN_DIR, user_dir):
        if folder is None or not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.yaml")):
            try:
                cfg = parse_agent(f.read_text(encoding="utf-8"), f.name)
            except AgentConfigError as e:
                errors.append(str(e))
                log.warning("skipping agent config %s", e)
                continue
            agents[cfg.id] = cfg
    return agents, errors
