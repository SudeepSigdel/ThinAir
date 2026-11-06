"""`thinair doctor`: check a Thinair install and say how to fix what's wrong.

Every check is read-only. Only one leaves the machine: with a GitHub token configured,
`GET https://api.github.com/rate_limit` with that token (to GitHub only) to tell a working
token from a rejected one. Nothing is sent to Telegram, and no agent is started.
"""

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from thinair.config import get_settings

Status = Literal["ok", "warn", "fail"]
GiB = 1024**3


@dataclass
class Check:
    status: Status
    name: str
    detail: str
    fix: str = ""


def run_checks(mgr=None, *, online: bool = True) -> list[Check]:  # noqa: ANN001
    """All checks, in display order. `mgr` is a SandboxManager (None = connect with settings)."""
    out: list[Check] = []

    def guard(name: str, fn: Callable[[], list[Check] | Check]) -> None:
        try:
            res = fn()
        except Exception as e:  # a broken check must not hide the others
            res = Check("fail", name, f"check crashed: {type(e).__name__}: {e}")
        out.extend(res if isinstance(res, list) else [res])

    guard("database", _database)
    guard("login", _login)
    guard("disk", _disk)
    docker_checks: list[Check] = []

    def _docker_all() -> list[Check]:
        nonlocal mgr
        from thinair.sandbox.manager import SandboxManager

        if mgr is None:
            mgr = SandboxManager()
        docker_checks.extend(_docker(mgr))
        if docker_checks[0].status != "fail":
            docker_checks.extend(_images(mgr))
            docker_checks.extend(_agents(mgr))
        return docker_checks

    guard("docker", _docker_all)
    guard("phone access", _phone)
    guard("github", lambda: _github(online))
    guard("notifications", _telegram)
    guard("runners", _runners)
    guard("version", lambda: _version(online))
    return out


RELEASES_URL = "https://api.github.com/repos/SudeepSigdel/ThinAir/releases/latest"


def _parse_version(v: str) -> tuple[int, ...] | None:
    parts = v.strip().removeprefix("v").split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return None  # e.g. a dev build


def _version(online: bool) -> Check:
    """Is a newer release out? (Read-only: one anonymous GitHub API call.)"""
    from thinair import __version__

    if not online:
        return Check("ok", "Version", f"{__version__} (not checked online)")
    import requests

    try:
        r = requests.get(
            RELEASES_URL, headers={"Accept": "application/vnd.github+json"}, timeout=10
        )
        latest = r.json().get("tag_name", "") if r.ok else ""
    except (requests.RequestException, ValueError) as e:
        return Check(
            "ok", "Version", f"{__version__} (couldn't check for updates: {type(e).__name__})"
        )
    mine, theirs = _parse_version(__version__), _parse_version(latest)
    if mine is None or theirs is None:
        return Check("ok", "Version", f"{__version__} (latest release: {latest or 'unknown'})")
    if theirs > mine:
        return Check(
            "warn",
            "Version",
            f"{__version__}; {latest} is available",
            "Update: run the installer again (it keeps your settings and data).",
        )
    return Check("ok", "Version", f"{__version__} (up to date)")


# ---- individual checks -------------------------------------------------------------------------
def _db_missing() -> bool:
    """A SQLite file that doesn't exist yet (connecting would create it: doctor changes nothing)."""
    url = get_settings().db_url
    return url.startswith("sqlite:///") and not Path(url.removeprefix("sqlite:///")).exists()


def _database() -> Check:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from thinair.db.migrate import _config
    from thinair.db.session import get_engine

    if _db_missing():
        return Check(
            "warn",
            "Database",
            f"no database yet at {get_settings().db_url.removeprefix('sqlite:///')}",
            "It is created when the server first starts.",
        )
    engine = get_engine()
    kind = "SQLite" if engine.url.get_backend_name() == "sqlite" else engine.url.get_backend_name()
    head = ScriptDirectory.from_config(_config(engine)).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    if current == head:
        return Check("ok", "Database", f"{kind}, schema up to date ({head})")
    if current is None:
        return Check(
            "warn", "Database", f"{kind}, empty", "It is created when the server first starts."
        )
    return Check(
        "warn",
        "Database",
        f"{kind}, schema {current}, this version expects {head}",
        "Restart the server; it upgrades the schema at startup.",
    )


def _login() -> Check:
    from sqlmodel import select

    from thinair.db.models import ApiToken
    from thinair.db.session import session_scope

    s = get_settings()
    if s.admin_token:
        if len(s.admin_token) < 32:
            return Check(
                "warn",
                "Login",
                "THINAIR_ADMIN_TOKEN is shorter than 32 characters",
                "Use a long random one (`thinair init` makes one). Short tokens can be locked out "
                "by strangers sending wrong tokens.",
            )
        return Check("ok", "Login", "admin token set in THINAIR_ADMIN_TOKEN")
    if _db_missing():
        return Check("warn", "Login", "no token yet", "The server prints one on first start.")
    try:
        with session_scope() as session:
            active = session.exec(
                select(ApiToken).where(ApiToken.revoked_at.is_(None))  # type: ignore[union-attr]
            ).first()
    except Exception:  # no tables yet
        active = None
    if active is None:
        return Check(
            "warn",
            "Login",
            "no token yet",
            "The server prints one on first start, or run `thinair token create --name phone`.",
        )
    return Check("ok", "Login", "API tokens exist (see `thinair token list`)")


def _disk() -> Check:
    data = get_settings().data_dir
    probe = data if data.exists() else data.parent if data.parent.exists() else None
    if probe is None:
        return Check("warn", "Disk", f"{data} does not exist yet")
    free = shutil.disk_usage(probe).free
    detail = f"{free / GiB:.1f} GiB free where Thinair keeps its data"
    if free < 2 * GiB:
        return Check(
            "fail" if free < 0.5 * GiB else "warn",
            "Disk",
            detail,
            "Sandbox images and workspaces need a few GB. Free space with `docker system prune`.",
        )
    return Check("ok", "Disk", detail)


def _docker(mgr) -> list[Check]:  # noqa: ANN001
    try:
        info = mgr.client.info()
    except Exception as e:
        return [
            Check(
                "fail",
                "Docker",
                f"not reachable ({type(e).__name__})",
                "Is Docker running? With Compose, is the docker-proxy service up "
                "(`docker compose ps`)?",
            )
        ]
    cpus, mem = info.get("NCPU") or 0, info.get("MemTotal") or 0
    checks = [
        Check(
            "ok",
            "Docker",
            f"{info.get('ServerVersion', '?')}, {cpus} CPU(s), {mem / GiB:.1f} GiB memory",
        )
    ]
    if str(info.get("CgroupVersion", "2")) != "2":
        checks.append(
            Check(
                "warn",
                "cgroup v2",
                f"this host uses cgroup v{info.get('CgroupVersion')}",
                "Profiles then fall back to coarse memory/CPU samples. Use a recent Linux "
                "distribution (cgroup v2 is the default since ~2021).",
            )
        )
    if mem and mem < 1 * GiB:
        checks.append(
            Check(
                "warn",
                "Memory",
                f"only {mem / GiB:.1f} GiB",
                "Keep THINAIR_MAX_CONCURRENT_JOBS=1, or let a remote runner do the test runs.",
            )
        )
    try:
        mgr.client.networks.list(names=["thinair-doctor-none"])
    except Exception:
        checks.append(
            Check(
                "warn",
                "Networks API",
                "the Docker proxy refuses network calls",
                "Agent runs with an outbound allowlist need it: set NETWORKS: 1 on docker-proxy.",
            )
        )
    return checks


def _images(mgr) -> list[Check]:  # noqa: ANN001
    from thinair.project.prepare import BUILDABLE_IMAGES

    missing = [s for s, tag in BUILDABLE_IMAGES.items() if not mgr.image_exists(tag)]
    if not missing:
        return [Check("ok", "Sandbox images", "python, node and git images are ready")]
    return [
        Check(
            "warn",
            "Sandbox images",
            f"not prepared yet: {', '.join(missing)}",
            "The first job prepares them (a minute or two). To do it now: `thinair images`.",
        )
    ]


def _agents(mgr) -> list[Check]:  # noqa: ANN001
    from thinair.agents.config import load_agents
    from thinair.jobs.executors import agents_dir

    agents, errors = load_agents(agents_dir())
    out = [
        Check("fail", "Agent config", e, "Fix or remove that YAML file in the agents folder.")
        for e in errors
    ]
    with_login = []
    for cfg in agents.values():
        if cfg.login_id != cfg.id:
            continue  # ACP variants share another agent's login
        try:
            mgr.client.volumes.get(cfg.auth_volume)
            with_login.append(cfg.id)
        except Exception:  # noqa: S112
            continue
    if with_login:
        out.append(
            Check(
                "ok",
                "Agent logins",
                f"login volume exists for: {', '.join(sorted(with_login))}",
                "",
            )
        )
    else:
        out.append(
            Check(
                "warn",
                "Agent logins",
                "no agent has been logged in yet (profiles work without one)",
                "For agent tasks: `thinair auth claude --print-command` (or codex, gemini).",
            )
        )
    return out


def _phone() -> Check:
    url = get_settings().public_url
    if url and url.startswith("https://"):
        return Check("ok", "Phone access", f"public address {url}")
    return Check(
        "warn",
        "Phone access",
        "no https:// address set (THINAIR_PUBLIC_URL)",
        "Installing the app on a phone needs HTTPS: a tunnel (Tailscale, Cloudflare) or "
        "THINAIR_DOMAIN with `docker compose --profile https up -d`. Then set THINAIR_PUBLIC_URL "
        "so notifications link to it.",
    )


def _github(online: bool) -> Check:
    from thinair.github import GitHubTokenError, github_token

    try:
        token = github_token()
    except GitHubTokenError as e:
        return Check("fail", "GitHub token", str(e), "Fix THINAIR_GITHUB_TOKEN(_FILE).")
    if token is None:
        return Check("ok", "GitHub token", "not set (public repositories only)")
    if not online:
        return Check("ok", "GitHub token", "set (not checked online)")
    import requests

    try:
        r = requests.get(
            "https://api.github.com/rate_limit",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
    except requests.RequestException as e:
        return Check("warn", "GitHub token", f"set; GitHub not reachable ({type(e).__name__})")
    if r.status_code == 401:
        return Check(
            "fail",
            "GitHub token",
            "GitHub rejects the token (expired or revoked?)",
            "Create a new fine-grained token and update THINAIR_GITHUB_TOKEN.",
        )
    if r.ok:
        return Check("ok", "GitHub token", "set and accepted by GitHub")
    return Check("warn", "GitHub token", f"set; GitHub answered HTTP {r.status_code}")


def _telegram() -> Check:
    from thinair.notify import NotifyConfigError, notifier_from_settings

    try:
        notifier = notifier_from_settings()
    except NotifyConfigError as e:
        return Check("fail", "Notifications", str(e), "Fix the TELEGRAM settings in .env.")
    if notifier is None:
        return Check("ok", "Notifications", "off (optional: see README, Telegram)")
    return Check("ok", "Notifications", "Telegram configured; test it with `thinair notify-test`")


def _runners() -> Check:
    from thinair.db.session import session_scope
    from thinair.runners import list_runners

    try:
        if _db_missing():
            raise LookupError
        with session_scope() as s:
            runners = [r for r in list_runners(s) if r.disabled_at is None]
    except Exception:  # no tables yet
        runners = []
    placement = get_settings().runner_placement
    if not runners:
        if placement == "runners":
            return Check(
                "fail",
                "Runners",
                "THINAIR_RUNNER_PLACEMENT=runners but no runner is registered",
                "Profile jobs will wait forever. Add one (`thinair runner add <name>`) or set "
                "THINAIR_RUNNER_PLACEMENT=auto.",
            )
        return Check("ok", "Runners", "none (optional: see docs/runners.md)")
    seen = ", ".join(
        f"{r.name} (last seen {r.last_seen_at:%Y-%m-%d %H:%M})" if r.last_seen_at else r.name
        for r in runners
    )
    return Check("ok", "Runners", seen)


# ---- output ------------------------------------------------------------------------------------
MARK = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[FAIL]"}


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{MARK[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status != "ok":
            lines.append(f"       -> {c.fix}")
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    if fails:
        lines.append(f"\n{fails} problem(s) to fix, {warns} warning(s).")
    elif warns:
        lines.append(f"\nWorking, with {warns} suggestion(s) above.")
    else:
        lines.append("\nEverything looks good.")
    return "\n".join(lines)
