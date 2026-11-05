"""Per-job event log: persisted in SQLite, streamed live over SSE, replayable after a reconnect.

Every event gets a per-job sequence number (`seq`). It doubles as the SSE `id:`, so a phone that
loses its connection reconnects with `Last-Event-ID` and receives exactly what it missed.

Idle cost: a connected stream sleeps on an `asyncio.Event` (woken by `emit`) with a 15 s
keep-alive. Nothing polls the database while nobody is writing.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from sqlmodel import select

from thinair.db.models import EventType, Job, JobEvent, JobStatus
from thinair.db.session import session_scope

TERMINAL_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMEOUT}
)
BATCH = 500


def format_sse(ev: JobEvent) -> str:
    payload = {"ts": ev.ts.isoformat(), "stream": ev.stream, "data": ev.data}
    return f"id: {ev.seq}\nevent: {ev.type.value}\ndata: {json.dumps(payload)}\n\n"


class EventStore:
    def __init__(self, max_events_per_job: int = 50_000, heartbeat_s: float = 15.0) -> None:
        self.max_events = max_events_per_job
        self.heartbeat_s = heartbeat_s
        self._seq: dict[str, int] = {}
        self._waiters: set[asyncio.Event] = set()

    # ---- writing -------------------------------------------------------------------------
    def emit(
        self,
        job_id: str,
        type_: EventType,
        data: dict[str, Any] | None = None,
        stream: str | None = None,
    ) -> int | None:
        """Append an event and wake streams. Returns its seq, or None if dropped (log cap)."""
        with session_scope() as s:
            if job_id not in self._seq:
                last = s.exec(
                    select(JobEvent.seq)
                    .where(JobEvent.job_id == job_id)
                    .order_by(JobEvent.seq.desc())  # type: ignore[attr-defined]
                    .limit(1)
                ).first()
                self._seq[job_id] = last or 0
            seq = self._seq[job_id] + 1
            if seq > self.max_events and type_ == EventType.LOG:
                if seq == self.max_events + 1:  # say so once, then drop further log lines
                    self._seq[job_id] = seq
                    s.add(
                        JobEvent(
                            job_id=job_id,
                            seq=seq,
                            type=EventType.LOG,
                            stream="system",
                            data={
                                "line": f"[thinair] log limit reached ({self.max_events} events); "
                                "further output is not stored"
                            },
                        )
                    )
                    s.commit()
                    self._wake()
                return None
            self._seq[job_id] = seq
            s.add(JobEvent(job_id=job_id, seq=seq, type=type_, stream=stream, data=data or {}))
            s.commit()
        self._wake()
        return seq

    def forget(self, job_id: str) -> None:
        self._seq.pop(job_id, None)

    def _wake(self) -> None:
        for w in self._waiters:
            w.set()

    # ---- reading -------------------------------------------------------------------------
    def fetch(self, job_id: str, after: int = 0, limit: int = BATCH) -> list[JobEvent]:
        with session_scope() as s:
            return list(
                s.exec(
                    select(JobEvent)
                    .where(JobEvent.job_id == job_id, JobEvent.seq > after)
                    .order_by(JobEvent.seq)  # type: ignore[arg-type]
                    .limit(limit)
                )
            )

    @staticmethod
    def is_terminal(job_id: str) -> bool:
        with session_scope() as s:
            job = s.get(Job, job_id)
            return job is None or job.status in TERMINAL_STATUSES

    async def stream(self, job_id: str, after: int = 0) -> AsyncIterator[str]:
        """Yield SSE frames for events with seq > `after`, live, until the job has finished."""
        wake = asyncio.Event()
        self._waiters.add(wake)
        last = after
        try:
            while True:
                wake.clear()  # before reading, so an emit during the read is not missed
                # Read "finished" *before* fetching: a finished job's events are all stored.
                finished = self.is_terminal(job_id)
                rows = await asyncio.to_thread(self.fetch, job_id, last)
                for row in rows:
                    last = row.seq
                    yield format_sse(row)
                if rows:
                    continue
                if finished:
                    return
                try:
                    await asyncio.wait_for(wake.wait(), self.heartbeat_s)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            self._waiters.discard(wake)
