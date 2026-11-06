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
from thinair.users import (
    OWNER_NAME,
    UserError,
    create_user,
    disable_user,
    get_user,
    list_users,
)

app = typer.Typer(help="Thinair: run coding agents in sandboxes and profile resource needs.")
token_app = typer.Typer(help="Manage API tokens.")
app.add_typer(token_app, name="token")
user_app = typer.Typer(help="Manage users (admins see everything, members only their own jobs).")
app.add_typer(user_app, name="user")


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


def _user_id(session, name: str | None) -> str | None:  # noqa: ANN001
    """None for the owner (no --user, or --user admin)."""
    if not name or name == OWNER_NAME:
        return None
    user = get_user(session, name)
    if user is None or user.disabled_at is not None:
        typer.echo(f"No active user called {name!r} (see `thinair user list`).", err=True)
        raise typer.Exit(1)
    return user.id


@token_app.command("create")
def token_create(
    name: str = typer.Option(..., help="Label, e.g. 'phone'"),
    user: str | None = typer.Option(None, "--user", help="Owner of the token (default: you)"),
) -> None:
    """Create an API token. The plaintext is shown once."""
    upgrade(get_engine())
    with session_scope() as session:
        typer.echo(create_token(session, name, user_id=_user_id(session, user)))


@token_app.command("list")
def token_list() -> None:
    """List tokens (never shows secrets)."""
    upgrade(get_engine())
    with session_scope() as session:
        names = {u.id: u.name for u in list_users(session)}
        for t in list_tokens(session):
            state = "revoked" if t.revoked_at else "active"
            owner = names.get(t.user_id, "?") if t.user_id else OWNER_NAME
            typer.echo(f"{t.id}  {t.name:<16} {owner:<16} {state}")


@user_app.command("add")
def user_add(
    name: str = typer.Argument(..., help="Lowercase name, e.g. alice"),
    admin: bool = typer.Option(False, "--admin", help="Can see all jobs and manage users"),
) -> None:
    """Add a user and print their first token (shown once)."""
    upgrade(get_engine())
    with session_scope() as session:
        try:
            u = create_user(session, name, admin=admin)
        except UserError as e:
            typer.echo(f"error: {e.message}", err=True)
            raise typer.Exit(1) from None
        token = create_token(session, "first", user_id=u.id)
    typer.echo(f"Added {u.name} ({'admin' if u.is_admin else 'member'}). Their token:")
    typer.echo(f"  {token}")
    typer.echo(f"They log in to agents with: thinair auth <agent> --user {u.name}")


@user_app.command("list")
def user_list() -> None:
    """List users."""
    upgrade(get_engine())
    with session_scope() as session:
        typer.echo(f"{OWNER_NAME:<16} admin   active (owner)")
        for u in list_users(session):
            role = "admin" if u.is_admin else "member"
            typer.echo(f"{u.name:<16} {role:<7} {'disabled' if u.disabled_at else 'active'}")


@user_app.command("disable")
def user_disable(name: str) -> None:
    """Disable a user and revoke their tokens (their jobs stay)."""
    upgrade(get_engine())
    with session_scope() as session:
        try:
            disable_user(session, name)
        except UserError as e:
            typer.echo(f"error: {e.message}", err=True)
            raise typer.Exit(1) from None
    typer.echo(f"Disabled {name}.")


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


@app.command("notify-test")
def notify_test() -> None:
    """Send a test notification with the configured Telegram settings."""
    from thinair.notify import NotifyConfigError, TelegramNotifier, notifier_from_settings

    _utf8_output()
    try:
        notifier = notifier_from_settings()
    except NotifyConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    if not isinstance(notifier, TelegramNotifier):
        typer.echo("Notifications are off: set THINAIR_TELEGRAM_BOT_TOKEN and _CHAT_ID.", err=True)
        raise typer.Exit(1)
    try:
        notifier.send_text("👋 Thinair test notification: notifications work.")
    except RuntimeError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    typer.echo("Sent. Check Telegram.")


def login_command(cfg, image: str, volume: str | None = None) -> list[str]:  # noqa: ANN001
    """`docker run` for an interactive login, hardened like every sandbox. Only the agent's own
    login volume is mounted; tokens from Thinair's environment are deliberately NOT passed."""
    argv = ["docker", "run", "-it", "--rm", "--user", "10001:10001", "--cap-drop", "ALL"]
    argv += ["--security-opt", "no-new-privileges:true", "--pids-limit", "512"]
    argv += ["-v", f"{volume or cfg.auth_volume}:{cfg.auth.config_dir}", "-e", "HOME=/tmp"]
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
    user: str | None = typer.Option(
        None, "--user", help="Log in for this Thinair user (default: the owner)"
    ),
) -> None:
    """Log a coding agent in once, with YOUR account, inside its own Docker volume.

    Thinair never reads or copies the login; it is mounted only into that agent's sandboxes.
    Every user has their own login volume: jobs never run on someone else's account.
    """
    import shlex
    import shutil
    import subprocess

    from thinair.agents.cli_adapter import ensure_agent_image
    from thinair.agents.config import load_agents
    from thinair.jobs.executors import agents_dir
    from thinair.sandbox.spec import SandboxSpec, VolumeMount
    from thinair.users import auth_volume_name

    _utf8_output()
    owner = None
    if user and user != OWNER_NAME:
        upgrade(get_engine())
        with session_scope() as session:
            owner = get_user(session, user)
        if owner is None or owner.disabled_at is not None:
            typer.echo(f"No active user called {user!r} (see `thinair user list`).", err=True)
            raise typer.Exit(1)
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
        volume = auth_volume_name(cfg.login_id, owner)
        mgr.ensure_persistent_volume(volume, {"thinair.auth": cfg.login_id})
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
            mounts=[VolumeMount(volume=volume, path=cfg.auth.config_dir)],
            network=True,
            timeout_s=90,
            tmp_noexec=False,
        )
        res, _ = asyncio.run(mgr.run_capture(spec))
        ok = res.exit_code == 0
        typer.echo(f"{cfg.name}: {'logged in' if ok else 'NOT logged in'}")
        raise typer.Exit(0 if ok else 1)

    argv = login_command(cfg, image, volume)
    if print_command or shutil.which("docker") is None:
        typer.echo("Run this on the Docker host (it needs an interactive terminal):")
        typer.echo("")
        typer.echo("  " + shlex.join(argv))
        typer.echo("")
        typer.echo(cfg.auth.login_help)
        return
    typer.echo(cfg.auth.login_help)
    raise typer.Exit(subprocess.call(argv))  # noqa: S603


# ---- remote runners -------------------------------------------------------------------------
runner_app = typer.Typer(help="Other machines that run profile jobs for this server.")
app.add_typer(runner_app, name="runner")


@runner_app.command("add")
def runner_add(name: str) -> None:
    """Register a runner (on the server). Prints its secret once."""
    from thinair.runners import RunnerError, create_runner

    upgrade(get_engine())
    with session_scope() as session:
        try:
            _, token = create_runner(session, name)
        except RunnerError as e:
            typer.echo(e.message, err=True)
            raise typer.Exit(1) from e
    typer.echo(f"Runner {name!r} registered. Its secret (shown once):")
    typer.echo(f"  {token}")
    typer.echo("")
    typer.echo("On the runner machine, put it in THINAIR_RUNNER_TOKEN and run:")
    typer.echo("  thinair runner start --server https://<this server>")


@runner_app.command("list")
def runner_list() -> None:
    """List runners (never shows secrets)."""
    from thinair.runners import list_runners

    upgrade(get_engine())
    with session_scope() as session:
        for r in list_runners(session):
            state = "disabled" if r.disabled_at else "active"
            seen = r.last_seen_at.strftime("%Y-%m-%d %H:%M") if r.last_seen_at else "never"
            cpus = (r.info or {}).get("cpus")
            typer.echo(f"{r.name:<16} {state:<9} last seen {seen}  cpus {cpus or '?'}")


@runner_app.command("disable")
def runner_disable(name: str) -> None:
    """Stop accepting a runner's secret (its current job is lost)."""
    from thinair.runners import RunnerError, disable_runner

    upgrade(get_engine())
    with session_scope() as session:
        try:
            disable_runner(session, name)
        except RunnerError as e:
            typer.echo(e.message, err=True)
            raise typer.Exit(1) from e
    typer.echo(f"Runner {name!r} disabled.")


@runner_app.command("start")
def runner_start(
    server: str = typer.Option(
        ..., "--server", envvar="THINAIR_SERVER_URL", help="The Thinair server, https://..."
    ),
    token_file: Path | None = typer.Option(
        None, "--token-file", help="Read the runner secret from this file"
    ),
    allow_http: bool = typer.Option(
        False,
        "--allow-http",
        envvar="THINAIR_RUNNER_ALLOW_HTTP",
        help="Allow http:// (only on a private network you trust)",
    ),
) -> None:
    """Run profile jobs for a server (on the runner machine). Needs Docker here.

    The secret comes from THINAIR_RUNNER_TOKEN or --token-file, never the command line.
    """
    import contextlib
    import logging
    import os

    from thinair.runner_client import (
        RunnerClient,
        RunnerSetupError,
        check_server_url,
        check_token,
    )

    _utf8_output()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        raw = (
            token_file.read_text(encoding="utf-8")
            if token_file
            else os.environ.get("THINAIR_RUNNER_TOKEN")
        )
        token = check_token(raw)
        url = check_server_url(server, allow_http)
        mgr = SandboxManager()
        info = {"cpus": mgr.host_cpus() or None}
        with contextlib.suppress(Exception):  # only informational
            info["memory_bytes"] = int(mgr.client.info().get("MemTotal") or 0) or None
        typer.echo(f"Runner started; waiting for profile jobs from {url}")
        asyncio.run(RunnerClient(url, token, info=info).run_forever())
    except RunnerSetupError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e
    except OSError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e
    except KeyboardInterrupt:
        typer.echo("Runner stopped.")


@app.command()
def doctor(
    offline: bool = typer.Option(False, "--offline", help="Skip the GitHub token check online"),
) -> None:
    """Check this install (Docker, database, disk, login, phone access, ...) and suggest fixes."""
    from thinair.doctor import render, run_checks

    _utf8_output()
    checks = run_checks(online=not offline)
    typer.echo(render(checks))
    raise typer.Exit(1 if any(c.status == "fail" for c in checks) else 0)


@app.command()
def images() -> None:
    """Prepare the sandbox images now (the first job would otherwise do it)."""
    from thinair.project.prepare import BUILDABLE_IMAGES, ensure_image

    _utf8_output()
    mgr = SandboxManager()

    async def _all() -> None:
        for stack in BUILDABLE_IMAGES:
            tag = await ensure_image(mgr, stack, typer.echo)
            typer.echo(f"ready: {tag}")

    try:
        asyncio.run(_all())
    except SandboxError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e
