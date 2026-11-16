"""FastAPI application factory."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from thinair import __version__, setup_state
from thinair.agents.login import DockerLoginBackend, LoginBackend, LoginHub
from thinair.api import auth, github_api, login_api, push_api, routes, runner_api, setup_api
from thinair.api.web import mount_web
from thinair.auth import bootstrap_admin_token
from thinair.config import get_settings
from thinair.db.migrate import upgrade
from thinair.db.models import Job, JobKind
from thinair.db.session import get_engine, session_scope
from thinair.jobs.executors import default_executors
from thinair.jobs.service import Executor, JobService
from thinair.notify import NotifyConfigError, WebPushNotifier, notifier_from_settings
from thinair.retention import retention_loop
from thinair.runners import RunnerHub, make_remote_executor

log = logging.getLogger("thinair")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    upgrade(get_engine())  # once at startup; creates/updates the SQLite file
    setup_state.mark("installed")  # first start only; the funnel never leaves the server
    with session_scope() as session:
        new_token = bootstrap_admin_token(session, get_settings().admin_token)
    if new_token:
        # Shown exactly once: only its hash is stored. Printed (not logged) to keep it out of
        # structured log pipelines.
        print(
            "\n" + "=" * 64 + "\n"
            "  Thinair admin token (shown once, save it now):\n\n"
            f"    {new_token}\n\n"
            "  Lost it? Run: thinair token create --name phone\n" + "=" * 64 + "\n",
            flush=True,
        )
    settings = get_settings()
    try:
        telegram = notifier_from_settings()
    except NotifyConfigError as e:
        logging.getLogger("thinair").error("telegram notifications disabled: %s", e)
        telegram = None
    webpush = WebPushNotifier() if settings.web_push else None
    notifier = telegram
    hub = RunnerHub()

    def goes_remote(job: Job) -> bool:
        placement = get_settings().runner_placement
        if job.kind != JobKind.PROFILE or placement == "local":
            return False
        return placement == "runners" or hub.has_idle_runner()

    jobs = JobService(
        app.state.executors or default_executors(),
        workers=settings.max_concurrent_jobs,
        job_timeout_s=settings.job_timeout_s,
        sweep_on_start=settings.sweep_on_start,
        notifier=notifier,
        push_notifier=webpush,
        attention_notifier=webpush.attention if webpush else None,
        remote_executor=make_remote_executor(hub),
        goes_remote=goes_remote,
    )
    app.state.runners = hub
    # Agent sign-ins from the phone: in memory only, nothing runs until someone starts one.
    logins = LoginHub(app.state.login_backend or DockerLoginBackend())
    app.state.logins = logins
    await jobs.start()
    app.state.jobs = jobs
    # Once per start, in the background: has the public IP moved away from an sslip.io name?
    origin = None
    if settings.domain:
        origin = asyncio.create_task(_origin_check(app), name="thinair-origin-check")
    cleanup = None
    if settings.log_retention_days > 0:
        cleanup = asyncio.create_task(
            retention_loop(settings.log_retention_days), name="thinair-log-retention"
        )
    try:
        yield
    finally:
        if origin is not None and not origin.done():
            origin.cancel()
        if cleanup is not None:
            cleanup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup
        await logins.close_all()  # their containers go too
        await jobs.stop()


def create_app(
    executors: dict[JobKind, Executor] | None = None, login_backend: LoginBackend | None = None
) -> FastAPI:
    """`executors` and `login_backend` let tests run the real API with fakes (no Docker)."""
    docs = get_settings().api_docs
    app = FastAPI(
        title="Thinair",
        version=__version__,
        lifespan=lifespan,
        # "auth" serves them below, behind login; "off" not at all.
        docs_url="/docs" if docs == "open" else None,
        redoc_url="/redoc" if docs == "open" else None,
        openapi_url="/openapi.json" if docs == "open" else None,
    )
    app.state.executors = executors
    app.state.login_backend = login_backend
    app.state.origin_warning = None
    if docs == "auth":
        _docs_behind_login(app)
    app.include_router(auth.router)
    app.include_router(routes.router)
    app.include_router(login_api.router)
    app.include_router(github_api.router)
    app.include_router(github_api.open_router)
    app.include_router(setup_api.router)
    app.include_router(push_api.router)
    app.include_router(setup_api.open_router)
    app.include_router(runner_api.admin_router)
    app.include_router(runner_api.runner_router)
    mount_web(app)

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        """Unauthenticated liveness probe (used by the Docker healthcheck)."""
        return {"status": "ok", "version": __version__}

    return app


def _docs_behind_login(app: FastAPI) -> None:
    """The interactive API docs, only for signed-in callers (the server may be on the
    internet; the schema maps every route). Log in to the web app first, then open /docs."""
    from fastapi import Depends
    from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
    from fastapi.responses import HTMLResponse, JSONResponse

    from thinair.api.auth import require_auth

    guard = [Depends(require_auth)]

    @app.get("/openapi.json", include_in_schema=False, dependencies=guard)
    def openapi() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False, dependencies=guard)
    def docs() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi.json", title="Thinair API")

    @app.get("/redoc", include_in_schema=False, dependencies=guard)
    def redoc() -> HTMLResponse:
        return get_redoc_html(openapi_url="/openapi.json", title="Thinair API")


def check_origin(fetch=None) -> str | None:  # noqa: ANN001
    """If Thinair is served on an sslip.io name, does that name still match this machine's
    public IP? Returns a warning (with the fix) or None. One request; never repeated."""
    from thinair.netinfo import current_public_ipv4, ip_from_sslip

    expected = ip_from_sslip(get_settings().domain)
    if expected is None:
        return None
    current = current_public_ipv4(fetch)
    if current is None or current == expected:
        return None
    return (
        f"This server's public IP is now {current}, but its address "
        f"{get_settings().domain} points at {expected}, so phones can't reach it. "
        "Run the installer again on the server (it updates the address), then scan the new "
        "QR code with your phone."
    )


async def _origin_check(app: FastAPI) -> None:
    warning = await asyncio.to_thread(check_origin)
    app.state.origin_warning = warning
    if warning:
        log.warning("%s", warning)


app = create_app()
