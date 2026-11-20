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

from sqlmodel import Session, delete, select, update

from thinair.auth import create_token
from thinair.db.models import PairingCode, User, utcnow

CODE_PREFIX = "thinair_pair_"
TTL = timedelta(minutes=10)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


MAX_TTL = timedelta(hours=1)

# The short form, for a phone with no camera: 8 characters of Crockford base32 (40 bits), shown
# as XXXX-XXXX. 40 bits is guessable in principle, so every wrong short code seen anywhere counts
# against all live short codes and 20 of them burn those (the long QR code is unaffected), on top
# of the per-address throttle in `api/auth.py`.
SHORT_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SHORT_LEN = 8
SHORT_MAX_FAILURES = 20
_CROCKFORD_FIX = str.maketrans({"O": "0", "I": "1", "L": "1"})


def normalise_short(text: str) -> str | None:
    """`k7qm-2xvd` -> `K7QM2XVD`; None if it can't be a short code."""
    t = "".join(text.split()).replace("-", "").upper().translate(_CROCKFORD_FIX)
    if len(t) != SHORT_LEN or any(c not in SHORT_ALPHABET for c in t):
        return None
    return t


def format_short(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def create_pairing_code(session: Session, user_id: str | None = None, ttl: timedelta = TTL) -> str:
    """A fresh code for `user_id` (None = the owner), valid for `ttl` (at most an hour; the
    installer uses 30 minutes so there is time to set up the phone). Expired codes are cleaned
    up here."""
    return create_pairing(session, user_id, ttl, short=False)[0]


def create_pairing(
    session: Session, user_id: str | None = None, ttl: timedelta = TTL, *, short: bool = True
) -> tuple[str, str | None]:
    """`(long code, short code or None)`: two spellings of one pairing (one redemption)."""
    if not timedelta(0) < ttl <= MAX_TTL:
        raise ValueError("a pairing code lives between 1 second and 1 hour")
    now = utcnow()
    session.exec(delete(PairingCode).where(PairingCode.expires_at < now))  # type: ignore[call-overload,arg-type]
    code = CODE_PREFIX + secrets.token_urlsafe(32)
    short_code = (
        "".join(secrets.choice(SHORT_ALPHABET) for _ in range(SHORT_LEN)) if short else None
    )
    session.add(
        PairingCode(
            code_hash=_hash(code),
            user_id=user_id,
            expires_at=now + ttl,
            short_hash=_hash(short_code) if short_code else None,
        )
    )
    session.commit()
    return code, (format_short(short_code) if short_code else None)


def _find(session: Session, code: str) -> PairingCode | None:
    if code.startswith(CODE_PREFIX):
        return session.exec(select(PairingCode).where(PairingCode.code_hash == _hash(code))).first()
    short = normalise_short(code)
    if short is None:
        return None
    row = session.exec(select(PairingCode).where(PairingCode.short_hash == _hash(short))).first()
    if row is None:
        # A wrong short code: count it against every live short code (we can't know whose it
        # was meant to be), so guessing 40 bits online runs out of attempts quickly.
        now = utcnow()
        session.exec(
            update(PairingCode)  # type: ignore[call-overload]
            .where(
                PairingCode.short_hash.is_not(None),  # type: ignore[union-attr]
                PairingCode.used_at.is_(None),  # type: ignore[union-attr]
                PairingCode.expires_at > now,  # type: ignore[arg-type]
            )
            .values(failures=PairingCode.failures + 1)
        )
        session.commit()
        return None
    return None if row.failures >= SHORT_MAX_FAILURES else row


def redeem(session: Session, code: str) -> str | None:
    """Exchange a valid code (long or short) for a new API token (in plain text), or None."""
    row = _find(session, code.strip())
    now = utcnow()
    if row is None or row.used_at is not None or row.expires_at.replace(tzinfo=now.tzinfo) < now:
        return None
    if row.user_id is not None:
        user = session.get(User, row.user_id)
        if user is None or user.disabled_at is not None:
            return None
    # Conditional update, so two simultaneous requests with the same code cannot both win (the
    # checks above only read): exactly one sees a row it actually changed.
    won = session.exec(
        update(PairingCode)  # type: ignore[call-overload]
        .where(PairingCode.code_hash == row.code_hash, PairingCode.used_at.is_(None))  # type: ignore[union-attr]
        .values(used_at=now)
    )
    session.commit()
    if won.rowcount != 1:
        return None
    from thinair.setup_state import mark

    mark("paired", row.user_id)
    return create_token(session, f"phone (paired {now:%Y-%m-%d %H:%M})", user_id=row.user_id)


def pairing_url(public_url: str, code: str) -> str:
    return f"{public_url.rstrip('/')}/#pair={code}"
