"""Single-use `state` values for the GitHub redirects.

The two GitHub callbacks are open routes (GitHub's redirect carries no Thinair session). They act
only when they are given a live state that an authenticated admin asked for: random, 10 minutes,
used once, and only its SHA-256 is stored.
"""

import hashlib
import secrets
from datetime import timedelta

from sqlmodel import Session, delete, update

from thinair.db.models import OneTimeState, utcnow

TTL = timedelta(minutes=10)


def _hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def issue_state(session: Session, purpose: str, user_id: str | None = None) -> str:
    now = utcnow()
    session.exec(delete(OneTimeState).where(OneTimeState.expires_at < now))  # type: ignore[call-overload,arg-type]
    state = secrets.token_urlsafe(32)
    session.add(
        OneTimeState(hash=_hash(state), purpose=purpose, user_id=user_id, expires_at=now + TTL)
    )
    session.commit()
    return state


def consume_state_user(
    session: Session, state: str | None, purpose: str
) -> tuple[bool, str | None]:
    """Like `consume_state`, and also the person the state was issued for (None = the owner)."""
    if not consume_state(session, state, purpose):
        return False, None
    row = session.get(OneTimeState, _hash(state or ""))
    return True, row.user_id if row is not None else None


def consume_state(session: Session, state: str | None, purpose: str) -> bool:
    """True exactly once for a live state of this purpose. The update is conditional, so two
    simultaneous callbacks cannot both win."""
    if not state or len(state) > 200:
        return False
    now = utcnow()
    res = session.exec(
        update(OneTimeState)  # type: ignore[call-overload]
        .where(
            OneTimeState.hash == _hash(state),  # type: ignore[arg-type]
            OneTimeState.purpose == purpose,  # type: ignore[arg-type]
            OneTimeState.used_at.is_(None),  # type: ignore[union-attr]
            OneTimeState.expires_at >= now,  # type: ignore[arg-type]
        )
        .values(used_at=now)
    )
    session.commit()
    return res.rowcount == 1  # type: ignore[attr-defined]
