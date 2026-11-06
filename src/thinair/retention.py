"""Delete old log lines so the database doesn't grow forever.

Log lines are almost all of the database (an install alone prints thousands). Once a finished
job is older than `THINAIR_LOG_RETENTION_DAYS` (default 30, 0 = never), its LOG events are
deleted and `Job.logs_removed_at` is set. Everything else stays: the job, its status history,
profile runs and report, agent messages, summary, diff and patch. The job page therefore still
shows the result, with a note instead of the log.

Runs at startup and then once a day (one sleeping task; nothing polls). Deletes in small
batches so it never holds the database for long. SQLite reuses the freed space for new rows;
the file itself does not shrink.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, update
from sqlmodel import select

from thinair.db.models import EventType, Job, JobEvent, utcnow
from thinair.db.session import session_scope
from thinair.jobs.events import TERMINAL_STATUSES

log = logging.getLogger("thinair.retention")
EVERY_S = 24 * 3600


def remove_old_logs(days: int, *, now: datetime | None = None, batch: int = 50) -> int:
    """Delete the log lines of jobs that finished more than `days` ago. Returns how many jobs
    were cleaned. `days <= 0` does nothing."""
    if days <= 0:
        return 0
    now = now or utcnow()
    cutoff = now - timedelta(days=days)
    cleaned = 0
    while True:
        with session_scope() as s:
            ids = list(
                s.exec(
                    select(Job.id)
                    .where(
                        Job.status.in_(TERMINAL_STATUSES),  # type: ignore[attr-defined]
                        Job.finished_at < cutoff,  # type: ignore[operator]
                        Job.logs_removed_at.is_(None),  # type: ignore[union-attr]
                    )
                    .limit(batch)
                )
            )
            if not ids:
                return cleaned
            s.exec(  # type: ignore[call-overload]
                delete(JobEvent).where(
                    JobEvent.job_id.in_(ids),  # type: ignore[attr-defined]
                    JobEvent.type == EventType.LOG,
                )
            )
            s.exec(  # type: ignore[call-overload]
                update(Job).where(Job.id.in_(ids)).values(logs_removed_at=now)  # type: ignore[attr-defined]
            )
            s.commit()
        cleaned += len(ids)


async def retention_loop(days: int) -> None:
    """Background task: clean up now, then once a day."""
    while True:
        try:
            n = await asyncio.to_thread(remove_old_logs, days)
            if n:
                log.info("removed the logs of %d job(s) older than %d days", n, days)
        except Exception:
            log.exception("log cleanup failed; will try again tomorrow")
        await asyncio.sleep(EVERY_S)
