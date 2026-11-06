"""Sign a phone in by scanning a QR code (`thinair qr`).

The QR code holds `<public url>/#pair=<code>`. The code is one-time and short-lived (10 min),
and only its SHA-256 is stored. The phone's browser sends it to `POST /api/pair` (the `#...`
part of a URL is never sent to servers or proxies, so it doesn't end up in access logs), and
gets a *new* API token of its own as its session cookie. The code left in the phone's history
is then useless, and that device can be revoked on its own (`thinair token revoke`).
"""

import hashlib
import secrets
from datetime import timedelta

from sqlmodel import Session, delete, select

from thinair.auth import create_token
from thinair.db.models import PairingCode, User, utcnow

CODE_PREFIX = "thinair_pair_"
TTL = timedelta(minutes=10)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create_pairing_code(session: Session, user_id: str | None = None) -> str:
    """A fresh code for `user_id` (None = the owner). Expired codes are cleaned up here."""
    now = utcnow()
    session.exec(delete(PairingCode).where(PairingCode.expires_at < now))  # type: ignore[call-overload,arg-type]
    code = CODE_PREFIX + secrets.token_urlsafe(32)
    session.add(PairingCode(code_hash=_hash(code), user_id=user_id, expires_at=now + TTL))
    session.commit()
    return code


def redeem(session: Session, code: str) -> str | None:
    """Exchange a valid code for a new API token (returned in plain text), or None."""
    if not code.startswith(CODE_PREFIX):
        return None
    row = session.exec(select(PairingCode).where(PairingCode.code_hash == _hash(code))).first()
    now = utcnow()
    if row is None or row.used_at is not None or row.expires_at.replace(tzinfo=now.tzinfo) < now:
        return None
    if row.user_id is not None:
        user = session.get(User, row.user_id)
        if user is None or user.disabled_at is not None:
            return None
    row.used_at = now
    session.add(row)
    session.commit()
    return create_token(session, f"phone (paired {now:%Y-%m-%d %H:%M})", user_id=row.user_id)


def pairing_url(public_url: str, code: str) -> str:
    return f"{public_url.rstrip('/')}/#pair={code}"
