"""Decide the stack and default commands from the repo's top-level file names, then merge
with `thinair.yaml` overrides into a `ResolvedProject`."""

from pydantic import BaseModel

from thinair.project.config import ProjectConfig, ProjectConfigError

PYTHON_IMAGE = "thinair-sandbox-python:2"
NODE_IMAGE = "thinair-sandbox-node:2"
IMAGES = {"python": PYTHON_IMAGE, "node": NODE_IMAGE}

# Dependencies live inside the workspace volume so they survive between the install container
# and the (separate, network-less) test containers.
_PY_ENV = {
    "PATH": "/workspace/.venv/bin:/usr/local/bin:/usr/bin:/bin",
    "VIRTUAL_ENV": "/workspace/.venv",
}


class ResolvedProject(BaseModel):
    stack: str
    image: str
    install: str
    test: str
    env: dict[str, str]
    workdir: str  # absolute path inside the sandbox
    config: ProjectConfig


def detect_stack(names: set[str]) -> str | None:
    if "package.json" in names:
        return "node"
    if names & {"requirements.txt", "pyproject.toml", "setup.py", "setup.cfg"}:
        return "python"
    return None


def _python_install(names: set[str]) -> str:
    if "requirements.txt" in names:
        deps = "pip install -r requirements.txt"
    else:
        deps = "pip install -e ."
    # pytest is added so projects that only list runtime dependencies are still testable.
    return f"python -m venv /workspace/.venv && {deps} && pip install pytest"


def _node_install(names: set[str]) -> str:
    return "npm ci" if "package-lock.json" in names else "npm install"


def resolve_project(names: set[str], config: ProjectConfig) -> ResolvedProject:
    stack = config.stack if config.stack != "auto" else detect_stack(names)
    if stack is None:
        raise ProjectConfigError(
            "Could not detect the project type (looked for package.json, requirements.txt, "
            "pyproject.toml). Add a thinair.yaml with `stack:`, `install:` and `test:`."
        )
    if stack == "python":
        install = config.install or _python_install(names)
        test = config.test or "python -m pytest -q"
        env = dict(_PY_ENV)
    else:
        install = config.install or _node_install(names)
        test = config.test or "npm test"
        env = {"PATH": "/workspace/node_modules/.bin:/usr/local/bin:/usr/bin:/bin"}
    env.update(config.env)
    workdir = "/workspace" if config.workdir == "." else f"/workspace/{config.workdir.strip('/')}"
    return ResolvedProject(
        stack=stack,
        image=IMAGES[stack],
        install=install,
        test=test,
        env={"HOME": "/tmp", "CI": "1", **env},  # noqa: S108
        workdir=workdir,
        config=config,
    )
