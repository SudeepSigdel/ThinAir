"""Agent configs: one YAML file per coding-agent CLI. Adding an agent needs a file, not code.

Built-in configs live in `agents/builtin/`. Users add or override agents by dropping
`<id>.yaml` into `$THINAIR_DATA_DIR/agents/` (a user file with the same id wins).
"""

import logging
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from thinair.sandbox.spec import parse_size

log = logging.getLogger("thinair.agents")
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
# The ACP client that runs inside the agent sandbox (Node.js, standard library only).
ACP_DRIVER = (Path(__file__).resolve().parent / "acp_driver.js").read_text(encoding="utf-8")
TASK = "{task}"
_ID = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
_HOST = re.compile(r"^(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


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
    # Reuse another agent's login volume (e.g. an ACP adapter for an agent you already logged
    # in to). Its config_dir must be the same.
    shares_login_with: str | None = None
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
    # Copy Debian's CA bundle into the image (node:22-slim has none; native binaries need it).
    needs_ca_certs: bool = False
    # "cli": `command` is the agent's headless argv with "{task}" once, as its own item.
    # "acp": `command` starts the agent as an Agent Client Protocol server on stdio (no "{task}");
    # Thinair's driver (acp_driver.js, needs Node) sends the task. Output format is then "acp".
    protocol: Literal["cli", "acp"] = "cli"
    command: list[str] = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    auth: AuthConfig
    output: Literal["stream-json", "codex-json", "gemini-stream-json", "acp", "text"] = "text"
    auth_error_patterns: list[str] = Field(default_factory=list)  # case-insensitive substrings
    limits: Limits = Limits()
    network: bool = True  # agents must reach their model provider
    # Outbound allowlist: host names (or `*.domain`) this agent may reach over HTTPS through
    # Thinair's per-run proxy. None = unrestricted (only for CLIs whose proxy support and
    # endpoints are not verified). The CLI must honour HTTPS_PROXY.
    egress: list[str] | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError("must be lowercase letters, digits and dashes, starting with a letter")
        return v

    @model_validator(mode="after")
    def _protocol(self) -> "AgentConfig":
        v = self.command
        if self.protocol == "acp":
            if any(TASK in a for a in v):  # nothing is substituted in ACP commands
                raise ValueError(
                    'command: an ACP agent gets the task over the protocol; no "{task}"'
                )
            if not self.needs_node:
                raise ValueError("needs_node: must be true for ACP agents (the driver is Node.js)")
            if self.output not in ("acp", "text"):
                raise ValueError('output: ACP agents always use "acp"')
            self.output = "acp"
            return self
        if self.output == "acp":
            raise ValueError('output: "acp" needs protocol: acp')
        if v.count(TASK) != 1:
            raise ValueError('command: must contain "{task}" exactly once, as a separate list item')
        for arg in v:
            if arg != TASK and _PLACEHOLDER.search(arg):
                raise ValueError(f'command: unknown placeholder in {arg!r}; only "{{task}}" works')
        return self

    @field_validator("auth")
    @classmethod
    def _shared(cls, v: AuthConfig) -> AuthConfig:
        if v.shares_login_with is not None and not _ID.match(v.shares_login_with):
            raise ValueError("shares_login_with: must be an agent id")
        return v

    @field_validator("egress")
    @classmethod
    def _egress(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        bad = [h for h in v if not _HOST.match(h)]
        if bad:
            raise ValueError(
                f"invalid host names: {bad} (use e.g. api.example.com or *.example.com)"
            )
        return [h.lower() for h in v]

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        bad = [k for k in v if not _ENV.match(k)]
        if bad:
            raise ValueError(f"invalid variable names: {bad}")
        return {k: str(x) for k, x in v.items()}

    @property
    def login_id(self) -> str:
        """Whose login volume this agent uses (itself unless `auth.shares_login_with`)."""
        return self.auth.shares_login_with or self.id

    @property
    def auth_volume(self) -> str:
        return f"thinair-auth-{self.login_id}"

    @property
    def auth_hint(self) -> str:
        return self.auth.hint.replace("{id}", self.id)

    def argv(self, task: str) -> list[str]:
        """The command with the task as one argument. A task starting with '-' gets a leading
        space so the agent's option parser can never read it as a flag."""
        safe = f" {task}" if task.startswith("-") else task
        if self.protocol == "acp":
            # `node -e` stops option parsing at "--"; the driver reads [task, *command].
            return ["node", "-e", ACP_DRIVER, "--", task, *self.command]
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
