"""Engine and session helpers. Default: one small SQLite file, WAL so readers (SSE) don't block
writers. `THINAIR_DATABASE_URL` switches to Postgres."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, event
from sqlmodel import Session, create_engine

from thinair.config import get_settings


def _sqlite_pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    # Safe with WAL (the database can't be corrupted; a power cut may lose the last commits) and
    # avoids an fsync on every commit, which is slow on small VMs' network disks.
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})
        event.listen(engine, "connect", _sqlite_pragmas)
        return engine
    # Postgres (or another server database): a small pool; check connections after idle periods.
    return create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True)


@lru_cache
def get_engine() -> Engine:
    s = get_settings()
    if s.db_url.startswith("sqlite"):
        s.data_dir.mkdir(parents=True, exist_ok=True)
    return make_engine(s.db_url)


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session
