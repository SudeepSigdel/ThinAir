"""`thinair` command line interface."""

import asyncio
import sys
from pathlib import Path

import typer
import uvicorn

from thinair import __version__
from thinair.auth import create_token, generate_token, list_tokens, revoke_token
from thinair.config import get_settings
from thinair.db.migrate import upgrade
from thinair.db.models import new_id
from thinair.db.session import get_engine, session_scope
from thinair.profiler.report import to_json, to_markdown
from thinair.profiler.runner import profile_source
from thinair.project.prepare import DirSource, GitSource, PrepareError
from thinair.sandbox.manager import SandboxManager
from thinair.sandbox.spec import SandboxError

app = typer.Typer(help="Thinair: run coding agents in sandboxes and profile resource needs.")
token_app = typer.Typer(help="Manage API tokens.")
app.add_typer(token_app, name="token")


@app.command()
def version() -> None:
    """Print the Thinair version."""
    typer.echo(__version__)


ENV_TEMPLATE = """# Created by `thinair init`. See .env.example for every option.
THINAIR_ADMIN_TOKEN={token}
THINAIR_MAX_CONCURRENT_JOBS=1
# Bind address on the host. 127.0.0.1 = this machine only (use a tunnel or the https profile).
THINAIR_BIND=127.0.0.1
"""


@app.command()
def init(
    path: Path = typer.Option(Path(".env"), "--path", help="Where to write the settings file"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file"),
) -> None:
    """Create a .env with a fresh admin token (the secret you log in to the phone UI with)."""
    if path.exists() and not force:
        typer.echo(f"{path} already exists. Use --force to replace it.", err=True)
        raise typer.Exit(1)
    token = generate_token()
    path.write_text(ENV_TEMPLATE.format(token=token), encoding="utf-8")
    typer.echo(f"Wrote {path}.")
    typer.echo("")
    typer.echo("Your admin token (keep it secret):")
    typer.echo(f"  {token}")
    typer.echo("")
    typer.echo("Next: docker compose up -d   then open http://localhost:8080")


@app.command()
def serve() -> None:
    """Run the API server (one process, no background workers while idle)."""
    s = get_settings()
    uvicorn.run("thinair.api.app:app", host=s.host, port=s.port, log_level=s.log_level)


@token_app.command("create")
def token_create(name: str = typer.Option(..., help="Label, e.g. 'phone'")) -> None:
    """Create an API token. The plaintext is shown once."""
    upgrade(get_engine())
    with session_scope() as session:
        typer.echo(create_token(session, name))


@token_app.command("list")
def token_list() -> None:
    """List tokens (never shows secrets)."""
    upgrade(get_engine())
    with session_scope() as session:
        for t in list_tokens(session):
            state = "revoked" if t.revoked_at else "active"
            typer.echo(f"{t.id}  {t.name:<16} {state}")


@token_app.command("revoke")
def token_revoke(token_id: str) -> None:
    """Revoke a token by id."""
    upgrade(get_engine())
    with session_scope() as session:
        if not revoke_token(session, token_id):
            typer.echo("No active token with that id.", err=True)
            raise typer.Exit(1)
    typer.echo("Revoked.")


def _utf8_output() -> None:
    """Reports and sandbox output contain non-ASCII (emoji, test-runner symbols); legacy Windows
    consoles default to cp1252 and would crash on them. Replace unprintable characters instead."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


@app.command()
def profile(
    target: str = typer.Argument(
        ..., help="GitHub URL (private repos need THINAIR_GITHUB_TOKEN), or a local directory"
    ),
    json_out: Path | None = typer.Option(None, "--json", help="Also write the JSON report here"),
    md_out: Path | None = typer.Option(None, "--md", help="Also write the Markdown report here"),
) -> None:
    """Find the minimum CPU/RAM a project needs to pass its tests (runs in Docker sandboxes)."""
    _utf8_output()
    path = Path(target)
    source = DirSource(path) if path.is_dir() else GitSource(target)

    def log(line: str) -> None:
        typer.echo(line, err=True)

    try:
        result, prepared = asyncio.run(profile_source(SandboxManager(), source, new_id(), log=log))
    except (PrepareError, SandboxError) as e:
        typer.echo(f"Error: {getattr(e, 'message', e)}", err=True)
        raise typer.Exit(1) from e
    meta = {
        "project": path.name if path.is_dir() else target.rstrip("/").split("/")[-1],
        "commit": prepared.commit_sha,
        "stack": prepared.resolved.stack,
    }
    markdown = to_markdown(result, **meta)
    typer.echo(markdown)
    if md_out:
        md_out.write_text(markdown, encoding="utf-8")
    if json_out:
        json_out.write_text(to_json(result, **meta), encoding="utf-8")
    if result.status == "baseline_failed":
        raise typer.Exit(2)


@app.command()
def agents() -> None:
    """List configured coding agents (and any agent YAML files with errors)."""
    from thinair.agents.config import load_agents
    from thinair.jobs.executors import agents_dir

    found, errors = load_agents(agents_dir())
    for a in found.values():
        typer.echo(f"{a.id:<14} {a.name} {a.version or ''}".rstrip())
    for e in errors:
        typer.echo(f"error: {e}", err=True)
    if errors:
        raise typer.Exit(1)


def login_command(cfg, image: str) -> list[str]:  # noqa: ANN001
    """`docker run` for an interactive login, hardened like every sandbox. Only the agent's own
    login volume is mounted; tokens from Thinair's environment are deliberately NOT passed."""
    argv = ["docker", "run", "-it", "--rm", "--user", "10001:10001", "--cap-drop", "ALL"]
    argv += ["--security-opt", "no-new-privileges:true", "--pids-limit", "512"]
    argv += ["-v", f"{cfg.auth_volume}:{cfg.auth.config_dir}", "-e", "HOME=/tmp"]
    for key, value in cfg.env.items():
        argv += ["-e", f"{key}={value}"]
    return [*argv, image, *cfg.auth.login_command]


@app.command()
def auth(
    agent_id: str = typer.Argument(..., help="Agent id, e.g. claude (see `thinair agents`)"),
    check: bool = typer.Option(False, "--check", help="Only report whether it is logged in"),
    print_command: bool = typer.Option(
        False, "--print-command", help="Print the docker command instead of running it"
    ),
    stack: str = typer.Option("node", help="Which project image to base the agent on"),
) -> None:
    """Log a coding agent in once, with YOUR account, inside its own Docker volume.

    Thinair never reads or copies the login; it is mounted only into that agent's sandboxes.
    """
    import shlex
    import shutil
    import subprocess

    from thinair.agents.cli_adapter import ensure_agent_image
    from thinair.agents.config import load_agents
    from thinair.jobs.executors import agents_dir
    from thinair.sandbox.spec import SandboxSpec, VolumeMount

    _utf8_output()
    found, _ = load_agents(agents_dir())
    cfg = found.get(agent_id)
    if cfg is None:
        typer.echo(
            f"Unknown agent {agent_id!r}. Configured: {', '.join(found) or 'none'}", err=True
        )
        raise typer.Exit(1)
    mgr = SandboxManager()
    try:
        image = asyncio.run(
            ensure_agent_image(mgr, cfg, stack, lambda line: typer.echo(line, err=True))
        )
        mgr.ensure_persistent_volume(cfg.auth_volume, {"thinair.auth": cfg.id})
    except SandboxError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e

    if check:
        if not cfg.auth.check_command:
            typer.echo(f"{cfg.name} has no login check configured.")
            raise typer.Exit(0)
        spec = SandboxSpec(
            image=image,
            command=cfg.auth.check_command,
            job_id="auth-check",
            env={**cfg.env, "HOME": "/tmp"},  # noqa: S108
            mounts=[VolumeMount(volume=cfg.auth_volume, path=cfg.auth.config_dir)],
            network=True,
            timeout_s=90,
            tmp_noexec=False,
        )
        res, _ = asyncio.run(mgr.run_capture(spec))
        ok = res.exit_code == 0
        typer.echo(f"{cfg.name}: {'logged in' if ok else 'NOT logged in'}")
        raise typer.Exit(0 if ok else 1)

    argv = login_command(cfg, image)
    if print_command or shutil.which("docker") is None:
        typer.echo("Run this on the Docker host (it needs an interactive terminal):")
        typer.echo("")
        typer.echo("  " + shlex.join(argv))
        typer.echo("")
        typer.echo(cfg.auth.login_help)
        return
    typer.echo(cfg.auth.login_help)
    raise typer.Exit(subprocess.call(argv))  # noqa: S603
