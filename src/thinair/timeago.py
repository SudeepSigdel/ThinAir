"""Short relative times for people ("5 min ago"), for the command line and messages."""

from datetime import UTC, datetime


def relative_time(when: datetime | None, now: datetime | None = None) -> str:
    """`never` for None, else `just now`, `N min ago`, `N h ago` or `N d ago`.

    SQLite returns naive datetimes (everything is stored as UTC), so those are read as UTC. A
    time slightly in the future (clocks differ a little) reads as `just now`.
    """
    if when is None:
        return "never"
    now = now or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    seconds = int((now - when).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"
