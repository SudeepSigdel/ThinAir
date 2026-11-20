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


# Folders that hold other people's code or tool output, never the project itself.
_NOT_PROJECTS = {
    "node_modules",
    "venv",
    "vendor",
    "site-packages",
    "dist",
    "build",
    "__pycache__",
    "third_party",
}
MAX_PROJECT_DEPTH = 2  # subfolder levels below the repository root that are searched
MANIFESTS = ("package.json", "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg")


def choose_workdir(paths: list[str]) -> tuple[str | None, str | None]:
    """Which subfolder holds the project, when the top folder holds none?

    `paths` are manifest files found below the root ("backend/requirements.txt"). Returns
    (folder or None, a note for the job log when there was a choice). One project: that one.
    Several: Python before Node (a profile needs tests, and Python projects ship them more
    often), then the shallowest, then by name; the note names the others so the person can pick
    with `workdir:` in thinair.yaml. Dependency, build and hidden folders never count."""
    by_dir: dict[str, set[str]] = {}
    for path in paths:
        parts = path.strip("/").split("/")
        folders, name = parts[:-1], parts[-1]
        if not folders or len(folders) > MAX_PROJECT_DEPTH:
            continue
        if any(f.startswith(".") or f in _NOT_PROJECTS for f in folders):
            continue
        by_dir.setdefault("/".join(folders), set()).add(name)
    found = {d: detect_stack(names) for d, names in by_dir.items()}
    found = {d: stack for d, stack in found.items() if stack}
    if not found:
        return None, None
    ranked = sorted(found, key=lambda d: (found[d] != "python", d.count("/"), d))
    chosen = ranked[0]
    note = None
    if len(ranked) > 1:
        others = ", ".join(f"{d} ({found[d]})" for d in ranked[1:])
        note = (
            f"found several projects; using {chosen} ({found[chosen]}). Also here: {others}. "
            "Add a thinair.yaml with `workdir:` to choose another."
        )
    return chosen, note


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
        seen = ", ".join(sorted(names)[:15]) or "nothing"
        raise ProjectConfigError(
            "Could not detect the project type. Thinair runs Python and Node.js projects: it "
            "looked for package.json, requirements.txt, pyproject.toml, setup.py or setup.cfg in "
            f"the top folder and up to two levels of subfolders. The top folder has: {seen}. "
            "Add a thinair.yaml to the repository with `workdir:` (the project's folder), "
            "`stack:` (python or node), `install:` and `test:`."
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
