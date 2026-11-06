"""Per-job event log: persisted in SQLite, streamed live over SSE, replayable after a reconnect.

Every event gets a per-job sequence number (`seq`). It doubles as the SSE `id:`, so a phone that
loses its connection reconnects with `Last-Event-ID` and receives exactly what it missed.

Idle cost: a connected stream sleeps on an `asyncio.Event` (woken by `emit`) with a 15 s
keep-alive. Nothing polls the database while nobody is writing.

Write cost: log lines arrive by the thousand (a pip install), so inside the event loop they are
buffered and written in one transaction every `flush_s` (or every `flush_lines`), instead of one
commit (and fsync) per line blocking the loop. Every other event type flushes the buffer and is
written at once, so order is kept and a status change is stored before `emit` returns.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlmodel import select

from thinair.db.models import EventType, Job, JobEvent, JobStatus
from thinair.db.session import session_scope

TERMINAL_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMEOUT}
)
BATCH = 500
log = logging.getLogger("thinair.events")


def format_sse(ev: JobEvent) -> str:
    payload = {"ts": ev.ts.isoformat(), "stream": ev.stream, "data": ev.data}
    return f"id: {ev.seq}\nevent: {ev.type.value}\ndata: {json.dumps(payload)}\n\n"


class EventStore:
    def __init__(
        self,
        max_events_per_job: int = 50_000,
        heartbeat_s: float = 15.0,
        flush_s: float = 0.2,
        flush_lines: int = 200,
    ) -> None:
        self.max_events = max_events_per_job
        self.heartbeat_s = heartbeat_s
        self.flush_s = flush_s
        self.flush_lines = flush_lines
        self._seq: dict[str, int] = {}
        self._waiters: dict[str, set[asyncio.Event]] = {}  # job id -> its open streams
        self._pending: list[JobEvent] = []
        self._flush_handle: asyncio.TimerHandle | None = None

    # ---- writing -------------------------------------------------------------------------
    def emit(
        self,
        job_id: str,
        type_: EventType,
        data: dict[str, Any] | None = None,
        stream: str | None = None,
    ) -> int | None:
        """Append an event and wake streams. Returns its seq, or None if dropped (log cap)."""
        if job_id not in self._seq:
            with session_scope() as s:
                last = s.exec(
                    select(JobEvent.seq)
                    .where(JobEvent.job_id == job_id)
                    .order_by(JobEvent.seq.desc())  # type: ignore[attr-defined]
                    .limit(1)
                ).first()
            self._seq[job_id] = last or 0
        seq = self._seq[job_id] + 1
        if seq > self.max_events and type_ == EventType.LOG:
            if seq != self.max_events + 1:
                return None
            # Say so once, then drop further log lines.
            type_, stream = EventType.LOG, "system"
            data = {
                "line": f"[thinair] log limit reached ({self.max_events} events); "
                "further output is not stored"
            }
            self._seq[job_id] = seq
            self._pending.append(
                JobEvent(job_id=job_id, seq=seq, type=type_, stream=stream, data=data)
            )
            self.flush()
            return None
        self._seq[job_id] = seq
        self._pending.append(
            JobEvent(job_id=job_id, seq=seq, type=type_, stream=stream, data=data or {})
        )
        if type_ != EventType.LOG or len(self._pending) >= self.flush_lines:
            self.flush()
        elif self._flush_handle is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # no event loop (CLI, plain tests): write right away
                self.flush()
            else:
                self._flush_handle = loop.call_later(self.flush_s, self._timed_flush)
        return seq

    def flush(self) -> None:
        """Write buffered events in one transaction and wake streams."""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        with session_scope() as s:
            s.add_all(batch)
            s.commit()
        self._wake({ev.job_id for ev in batch})

    def _timed_flush(self) -> None:
        self._flush_handle = None
        try:
            self.flush()
        except Exception:
            log.exception("could not store job events")

    def forget(self, job_id: str) -> None:
        self.flush()
        self._seq.pop(job_id, None)

    def _wake(self, job_ids: set[str]) -> None:
        for job_id in job_ids:
            for w in self._waiters.get(job_id, ()):
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
        self._waiters.setdefault(job_id, set()).add(wake)
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
            waiters = self._waiters.get(job_id)
            if waiters is not None:
                waiters.discard(wake)
                if not waiters:
                    del self._waiters[job_id]
