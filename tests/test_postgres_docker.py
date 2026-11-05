"""Thinair on Postgres (THINAIR_DATABASE_URL): migrations, the models/migrations match, and the
job API end to end (incl. the PR job kind added by migration 0002 and binary patches). Needs
Docker (runs a throwaway postgres:17-alpine) and the `postgres` extra."""

import time
import uuid

import docker
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlmodel import SQLModel

from thinair.config import get_settings
from thinair.db.migrate import _config, upgrade
from thinair.db.models import Job, JobKind, JobResult
from thinair.db.session import get_engine, make_engine, session_scope
from thinair.jobs.service import ExecResult

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.docker
PASSWORD = "thinair-test"  # noqa: S105  (throwaway local container)
ADMIN = "a" * 32


@pytest.fixture(scope="module")
def pg_port():
    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        pytest.skip("Docker daemon not available")
    c = client.containers.run(
        "postgres:17-alpine",
        detach=True,
        environment={"POSTGRES_PASSWORD": PASSWORD},
        ports={"5432/tcp": ("127.0.0.1", None)},
        labels={"thinair.test": "postgres"},
        mem_limit="256m",
    )
    try:
        port = None
        deadline = time.time() + 90
        while time.time() < deadline:
            c.reload()
            bindings = c.ports.get("5432/tcp")
            if bindings:
                port = int(bindings[0]["HostPort"])
                try:
                    psycopg.connect(
                        f"postgresql://postgres:{PASSWORD}@127.0.0.1:{port}/postgres",
                        connect_timeout=2,
                    ).close()
                    break
                except psycopg.OperationalError:
                    pass
            time.sleep(1)
        else:
            pytest.fail("postgres did not start")
        yield port
    finally:
        c.remove(force=True, v=True)


@pytest.fixture
def pg_url(pg_port, monkeypatch):
    name = f"t{uuid.uuid4().hex[:12]}"
    with psycopg.connect(
        f"postgresql://postgres:{PASSWORD}@127.0.0.1:{pg_port}/postgres", autocommit=True
    ) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = f"postgresql+psycopg://postgres:{PASSWORD}@127.0.0.1:{pg_port}/{name}"
    monkeypatch.setenv("THINAIR_DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield url
    get_engine().dispose()
    get_engine.cache_clear()


def test_migrations_up_down_up_and_match_the_models(pg_url) -> None:
    engine = make_engine(pg_url)
    upgrade(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    assert diff == []
    command.downgrade(_config(engine), "0001")
    upgrade(engine)  # ADD VALUE IF NOT EXISTS: re-running 0002 is fine
    engine.dispose()


def test_job_api_end_to_end_on_postgres(pg_url, monkeypatch) -> None:
    from thinair.api.app import create_app

    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("THINAIR_GITHUB_TOKEN", "github_pat_" + "Pg1" * 20)
    get_settings.cache_clear()
    patch = b"diff --git a/x b/x\n\x00binary\xff\n"

    async def agent(ctx, _mgr):
        ctx.log("working")
        with session_scope() as s:
            s.add(JobResult(job_id=ctx.job.id, summary="ok", diff_text="+x", patch=patch))
            row = s.get(Job, ctx.job.id)
            row.commit_sha = "b" * 40
            s.add(row)
            s.commit()
        return ExecResult()

    async def pr(ctx, _mgr):
        with session_scope() as s:
            parent = s.get(JobResult, ctx.job.parent_job_id)
            parent.pr_url = "https://github.com/o/r/pull/1"
            s.add(parent)
            s.commit()
        return ExecResult()

    async def profile(ctx, _mgr):
        return ExecResult()

    app = create_app({JobKind.AGENT: agent, JobKind.PR: pr, JobKind.PROFILE: profile})
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {ADMIN}"})

        def finished(job_id: str) -> dict:
            for _ in range(250):
                job = c.get(f"/api/jobs/{job_id}").json()
                if job["finished"]:
                    return job
                time.sleep(0.02)
            raise AssertionError(job)

        body = {
            "repo_url": "https://github.com/o/r",
            "kind": "agent",
            "agent_id": "claude",
            "task": "t",
        }
        aid = c.post("/api/jobs", json=body).json()["id"]
        assert finished(aid)["status"] == "succeeded"
        assert c.get(f"/api/jobs/{aid}/patch").content == patch  # bytea roundtrip
        events = c.get(f"/api/jobs/{aid}/events").text
        assert "working" in events and '"succeeded"' in events
        prj = c.post(f"/api/jobs/{aid}/pr").json()
        assert prj["kind"] == "pr"  # the enum value added by migration 0002
        assert finished(prj["id"])["status"] == "succeeded"
        assert c.get(f"/api/jobs/{aid}/result").json()["pr_url"].endswith("/pull/1")
        assert [j["kind"] for j in c.get("/api/jobs").json()] == ["pr", "agent"]
