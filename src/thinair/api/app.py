"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from thinair import __version__
from thinair.api import auth, routes
from thinair.api.web import mount_web
from thinair.auth import bootstrap_admin_token
from thinair.config import get_settings
from thinair.db.migrate import upgrade
from thinair.db.models import JobKind
from thinair.db.session import get_engine, session_scope
from thinair.jobs.executors import default_executors
from thinair.jobs.service import Executor, JobService

log = logging.getLogger("thinair")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    upgrade(get_engine())  # once at startup; creates/updates the SQLite file
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
    jobs = JobService(
        app.state.executors or default_executors(),
        workers=settings.max_concurrent_jobs,
        job_timeout_s=settings.job_timeout_s,
        sweep_on_start=settings.sweep_on_start,
    )
    await jobs.start()
    app.state.jobs = jobs
    try:
        yield
    finally:
        await jobs.stop()


def create_app(executors: dict[JobKind, Executor] | None = None) -> FastAPI:
    """`executors` lets tests run the real API with fake jobs (no Docker)."""
    app = FastAPI(title="Thinair", version=__version__, lifespan=lifespan)
    app.state.executors = executors
    app.include_router(auth.router)
    app.include_router(routes.router)
    mount_web(app)

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        """Unauthenticated liveness probe (used by the Docker healthcheck)."""
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
