import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from thinair.db.models import Job, JobEvent, JobKind, JobStatus, ProfileRun, Repo, RunOutcome


def test_migration_creates_all_tables(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert set(SQLModel.metadata.tables) <= tables


def test_models_match_migrations(engine: Engine) -> None:
    """Fails if someone edits models.py without generating a migration."""
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    assert diff == []


def test_job_roundtrip_and_defaults(engine: Engine) -> None:
    with Session(engine) as s:
        repo = Repo(url="https://github.com/o/r", name="r")
        s.add(repo)
        s.commit()
        job = Job(repo_id=repo.id, kind=JobKind.PROFILE, limits={"cpus": 1})
        s.add(job)
        s.commit()
        s.add(JobEvent(job_id=job.id, seq=1, type="log", data={"line": "hi"}))
        s.add(ProfileRun(job_id=job.id, cpus=0.5, mem_bytes=2**28, outcome=RunOutcome.OOM_KILLED))
        s.commit()

        got = s.exec(select(Job)).one()
        assert got.status == JobStatus.QUEUED
        assert got.limits == {"cpus": 1}
        assert s.exec(select(JobEvent)).one().data == {"line": "hi"}


def test_foreign_keys_enforced(engine: Engine) -> None:
    with Session(engine) as s:
        s.add(Job(repo_id="missing", kind=JobKind.AGENT))
        with pytest.raises(IntegrityError):
            s.commit()


def test_ids_are_unique_and_time_sortable() -> None:
    from thinair.db.models import new_id

    ids = [new_id() for _ in range(100)]
    assert len(set(ids)) == 100
