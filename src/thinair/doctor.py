"""`thinair doctor`: check a Thinair install and say how to fix what's wrong.

Every check is read-only. The GitHub App check reads only local files and the database. Network
requests (all skipped with `--offline`): with a GitHub token configured,
`GET https://api.github.com/rate_limit` with that token (to GitHub only); an
anonymous check for a newer release; with an sslip.io address, one anonymous request for this
machine's public IP (to https://ipv4.icanhazip.com); and an HTTPS request to Thinair's own
address. Nothing is sent to Telegram, and no agent is started.
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
    id: str = ""
    # A one-tap fix for the app's health screen: (kind, target, label) with kind "route"
    # (a #/ address), "post" (an /api/ route) or "copy". `thinair doctor` itself ignores it.
    action: tuple[str, str, str] | None = None


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
    guard("public address", lambda: _public_address(online))
    guard("https", lambda: _https(online))
    guard("github app", _github_app)
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
            id="images",
            action=("post", "/api/health/fix/images", "Prepare now"),
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
                "For agent tasks: sign Claude Code or Codex in on the Agents screen of the app"
                " (no terminal needed), or run `thinair auth claude --print-command`"
                " (or codex, gemini).",
                id="agents",
                action=("route", "#/agents", "Sign in an agent"),
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
        "Installing the app on a phone needs HTTPS. Run the installer again with "
        "THINAIR_PHONE=auto: with a public IP it sets up https://<your-ip>.sslip.io by itself, "
        "otherwise it offers Tailscale. Or use your own domain (THINAIR_PHONE=domain).",
    )


REINSTALL = (
    "Run the installer again on the server (it keeps your settings and updates the address), "
    "then scan the new QR code with your phone."
)


def _public_address(online: bool) -> list[Check]:
    """With an sslip.io address: does it still point at this machine's public IP?"""
    from thinair.netinfo import current_public_ipv4, ip_from_sslip

    s = get_settings()
    domain = (s.domain or "").strip()
    expected = ip_from_sslip(domain)
    if expected is None:
        return []  # own domain, Tailscale, or no public address: nothing derived from the IP
    if not online and not s.public_ip:
        return [Check("ok", "Public address", f"{domain} (not checked online)")]
    current = current_public_ipv4()
    if current is None:
        return [Check("warn", "Public address", "couldn't look up this machine's public IP")]
    if current != expected:
        return [
            Check(
                "fail",
                "Public address",
                f"this machine's public IP is now {current}, but {domain} points at {expected}",
                REINSTALL + " Tip: reserve a static IP with your provider so this can't recur.",
            )
        ]
    return [Check("ok", "Public address", f"{domain} matches this machine ({current})")]


def _https(online: bool) -> list[Check]:
    """Is Thinair's HTTPS address up with a valid certificate? Through Caddy's container when
    it is reachable (inside Compose), so no hairpin routing is needed."""
    from thinair.netinfo import firewall_hint, host_of, https_ready, local_cloud, resolves

    s = get_settings()
    host = (s.domain or "").strip() or host_of(s.public_url)
    if not host or not (s.public_url or "").startswith("https://") or not online:
        return []
    via = "caddy:443" if s.domain and resolves("caddy") else None
    res = https_ready(host, via=via)
    if res.ok:
        return [Check("ok", "HTTPS", res.reason)]
    fix = " ".join(line.strip() for line in firewall_hint(local_cloud())) if s.domain else ""
    if s.domain:
        fix += (
            " Caddy retries by itself once the ports are open (`docker compose logs caddy` "
            "shows why it failed)."
        )
    return [Check("warn", "HTTPS", res.reason, fix.strip())]


def _github_app() -> Check:
    """The GitHub App's key file and installations. Local only: no request leaves the machine."""
    from thinair.github import GitHubAPIError
    from thinair.github_app import SECRET_NAME, active_installations, load_credentials
    from thinair.secrets import secret_mode_problem

    try:
        creds = load_credentials()
    except GitHubAPIError as e:
        return Check("fail", "GitHub App", e.message, "Reconnect GitHub in the app.")
    if creds is None:
        return Check(
            "ok",
            "GitHub App",
            "not connected (a token in .env or public repositories still work)",
            "Connect GitHub in the app for short-lived, per-repository keys.",
            id="github",
            action=("route", "#/setup/github", "Connect GitHub"),
        )
    problem = secret_mode_problem(SECRET_NAME)
    if problem:
        return Check(
            "fail",
            "GitHub App",
            problem,
            "Run: chmod 600 <data dir>/secrets/github-app.json (or reconnect GitHub in the app).",
        )
    count = None
    try:
        if not _db_missing():
            from thinair.db.session import session_scope

            with session_scope() as s:
                count = len(active_installations(s))
    except Exception:  # no tables yet
        count = None
    if count == 0:
        return Check(
            "warn",
            "GitHub App",
            f"{creds.slug} exists but is not installed on any repository",
            "Open Connect GitHub in the app and choose repositories.",
        )
    detail = f"{creds.slug} (owner {creds.owner_login})"
    if count is not None:
        detail += f", {count} installation{'s' if count != 1 else ''}"
    return Check("ok", "GitHub App", detail)


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
