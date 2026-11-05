"""API token management. Only SHA-256 hashes are stored; plaintext is shown once at creation.

Tokens are 256-bit random strings, so a plain (unsalted) fast hash is appropriate: there is
nothing to brute-force, and it lets us look a token up by its hash with a unique index.
"""

import hashlib
import secrets
from datetime import timedelta

from sqlmodel import Session, select

from thinair.db.models import ApiToken, utcnow

TOKEN_PREFIX = "thinair_"  # noqa: S105
MIN_ENV_TOKEN_LEN = 20
ENV_TOKEN_NAME = "env-admin"  # noqa: S105
_LAST_USED_GRANULARITY = timedelta(minutes=1)  # avoid a DB write on every request


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(session: Session, name: str, token: str | None = None) -> str:
    """Create a token and return its plaintext (the only time it is available)."""
    plaintext = token or generate_token()
    session.add(ApiToken(name=name, token_hash=hash_token(plaintext)))
    session.commit()
    return plaintext


def verify_token(session: Session, token: str) -> ApiToken | None:
    row = session.exec(select(ApiToken).where(ApiToken.token_hash == hash_token(token))).first()
    if row is None or row.revoked_at is not None:
        return None
    now = utcnow()
    if row.last_used_at is None or now - row.last_used_at.replace(tzinfo=now.tzinfo) > (
        _LAST_USED_GRANULARITY
    ):
        row.last_used_at = now
        session.add(row)
        session.commit()
    return row


def revoke_token(session: Session, token_id: str) -> bool:
    row = session.get(ApiToken, token_id)
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow()
    session.add(row)
    session.commit()
    return True


def list_tokens(session: Session) -> list[ApiToken]:
    return list(session.exec(select(ApiToken).order_by(ApiToken.created_at)))


def bootstrap_admin_token(session: Session, env_token: str | None) -> str | None:
    """Ensure at least one usable token exists. Called once at startup.

    - If THINAIR_ADMIN_TOKEN is set, it is registered (hashed) as `env-admin`; a changed value
      replaces the old one. Nothing is printed.
    - Otherwise, if no active token exists, one is generated and returned so the caller can show
      it exactly once.
    """
    if env_token:
        if len(env_token) < MIN_ENV_TOKEN_LEN:
            raise ValueError(f"THINAIR_ADMIN_TOKEN must be at least {MIN_ENV_TOKEN_LEN} characters")
        wanted = hash_token(env_token)
        current = session.exec(
            select(ApiToken).where(ApiToken.name == ENV_TOKEN_NAME, ApiToken.revoked_at.is_(None))  # type: ignore[union-attr]
        ).all()
        if any(t.token_hash == wanted for t in current):
            return None
        for t in current:
            t.revoked_at = utcnow()
            session.add(t)
        existing = session.exec(select(ApiToken).where(ApiToken.token_hash == wanted)).first()
        if existing:  # token was previously revoked and is now being re-supplied
            existing.revoked_at = None
            session.add(existing)
            session.commit()
        else:
            create_token(session, ENV_TOKEN_NAME, env_token)
        return None

    active = session.exec(select(ApiToken).where(ApiToken.revoked_at.is_(None))).first()  # type: ignore[union-attr]
    if active is None:
        return create_token(session, "admin")
    return None
