"""Users and what a job may use on their behalf.

- The **owner** is the original single admin: tokens and jobs with `user_id = NULL`. They use the
  agent login volumes `thinair-auth-<agent>` and Thinair's own environment (API keys, GitHub token).
- Other users are rows in `User`. Admins see and manage everything; members see only their own
  jobs. Each user logs in to agents in their own volume `thinair-auth-<agent>.<user>` (agent ids
  cannot contain dots, so names never collide), so nobody runs on someone else's subscription.
- API keys from Thinair's environment (`auth.env_passthrough`) are passed to the owner's jobs only.
- The server's GitHub token is used for admins' jobs; members' jobs get it only with
  `THINAIR_GITHUB_TOKEN_FOR_MEMBERS=true` (else they can clone public repositories only and
  cannot open pull requests).
"""

import re
from dataclasses import dataclass

from sqlmodel import Session, select

from thinair.config import get_settings
from thinair.db.models import ApiToken, Job, User, utcnow

OWNER_NAME = "admin"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class UserError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Principal:
    """Who is making a request."""

    token_id: str
    token_name: str
    user_id: str | None  # None = the owner
    name: str
    is_admin: bool

    @property
    def is_owner(self) -> bool:
        return self.user_id is None

    def can_see(self, job: Job) -> bool:
        return self.is_admin or job.user_id == self.user_id


def principal_for(session: Session, token: ApiToken) -> Principal | None:
    """The principal behind a valid token, or None if its user is disabled or missing."""
    if token.user_id is None:
        return Principal(token.id, token.name, None, OWNER_NAME, True)
    user = session.get(User, token.user_id)
    if user is None or user.disabled_at is not None:
        return None
    return Principal(token.id, token.name, user.id, user.name, user.is_admin)


def validate_name(name: str) -> str:
    name = name.strip()
    if not NAME_RE.match(name):
        raise UserError(
            "bad_user_name",
            "User names are 1-32 lowercase letters, digits, '-' or '_', starting with a letter.",
        )
    if name == OWNER_NAME:
        raise UserError("reserved_user_name", f"{OWNER_NAME!r} is the owner; pick another name.")
    return name


def create_user(session: Session, name: str, *, admin: bool = False) -> User:
    name = validate_name(name)
    if session.exec(select(User).where(User.name == name)).first() is not None:
        raise UserError("user_exists", f"A user called {name!r} already exists.")
    user = User(name=name, is_admin=admin)
    session.add(user)
    session.commit()
    return user


def get_user(session: Session, name: str) -> User | None:
    return session.exec(select(User).where(User.name == name.strip())).first()


def list_users(session: Session) -> list[User]:
    return list(session.exec(select(User).order_by(User.created_at)))  # type: ignore[arg-type]


def disable_user(session: Session, name: str) -> User:
    """Disable a user and revoke all their tokens. Their jobs and results stay."""
    user = get_user(session, name)
    if user is None:
        raise UserError("user_not_found", f"No user called {name!r}.")
    now = utcnow()
    if user.disabled_at is None:
        user.disabled_at = now
        session.add(user)
    tokens = session.exec(
        select(ApiToken).where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))  # type: ignore[union-attr]
    )
    for t in tokens:
        t.revoked_at = now
        session.add(t)
    session.commit()
    return user


# ---- what a job acts as ------------------------------------------------------------------------
def job_user(session: Session, job: Job) -> User | None:
    return session.get(User, job.user_id) if job.user_id else None


def auth_volume_name(agent_id: str, user: User | None) -> str:
    """Login volume for an agent: shared by nobody. The owner keeps the original name."""
    return f"thinair-auth-{agent_id}" if user is None else f"thinair-auth-{agent_id}.{user.name}"


def github_token_allowed(user: User | None) -> bool:
    return user is None or user.is_admin or get_settings().github_token_for_members
