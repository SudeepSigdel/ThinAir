"""First-run progress and the local funnel.

Two kinds of rows in `SetupEvent`, one per (user, name): *funnel* events stamped the first time
something happens (`paired`, `github_connected`, `first_job`, ...) and *choices* (`skip:<step>`,
`dismissed`, `preferred_agent`). The setup steps themselves are NOT stored: `steps_for()` computes
`done` from real state every time, so the checklist can never disagree with reality.

Privacy: these rows never leave the server. Thinair has no telemetry and nowhere to send it; the
only way the numbers go anywhere is a person running `thinair stats` and pasting the output.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from thinair.db.models import Job, JobKind, Repo, SetupEvent, utcnow
from thinair.db.session import session_scope

log = logging.getLogger(__name__)

OWNER_KEY = "owner"
FUNNEL = (
    "installed",
    "paired",
    "github_connected",
    "agent_connected",
    "project_added",
    "first_job",
    "first_agent_success",
    "first_pr",
    "push_enabled",
)
STEPS = ("github", "agent", "project", "first_task")
STEP_LABELS = {
    "github": "Connect GitHub",
    "agent": "Sign in to your agent",
    "project": "Pick a repository",
    "first_task": "Try a first task",
}
CHOICES = {"preferred_agent"}


def key_for(user_id: str | None) -> str:
    return user_id or OWNER_KEY


def mark(name: str, user_id: str | None = None) -> bool:
    """Record a funnel event the first time it happens for this user. True if it was new.
    Never raises: bookkeeping must not break the request or job that triggered it."""
    if name not in FUNNEL:
        raise ValueError(f"unknown funnel event {name!r}")
    try:
        with session_scope() as s:
            return _insert(s, key_for(user_id), name)
    except Exception as e:  # noqa: BLE001 - e.g. no tables yet; telemetry is best effort
        log.debug("funnel event %s not recorded (%s)", name, type(e).__name__)
        return False


def _insert(s: Session, user_key: str, name: str, value: str | None = None) -> bool:
    if s.get(SetupEvent, (user_key, name)) is not None:
        return False
    s.add(SetupEvent(user_key=user_key, name=name, value=value))
    try:
        s.commit()
    except IntegrityError:  # another request got there first
        s.rollback()
        return False
    return True


def set_choice(s: Session, user_id: str | None, name: str, value: str | None) -> None:
    """Store or replace a choice (`preferred_agent`) or set a flag (`skip:github`, `dismissed`)."""
    key = key_for(user_id)
    row = s.get(SetupEvent, (key, name))
    if row is None:
        s.add(SetupEvent(user_key=key, name=name, value=value))
    else:
        row.value, row.at = value, utcnow()
        s.add(row)
    s.commit()


def clear(s: Session, user_id: str | None, name: str) -> None:
    row = s.get(SetupEvent, (key_for(user_id), name))
    if row is not None:
        s.delete(row)
        s.commit()


def get(s: Session, user_id: str | None, name: str) -> SetupEvent | None:
    return s.get(SetupEvent, (key_for(user_id), name))


def times(s: Session, user_id: str | None) -> dict[str, datetime]:
    key = key_for(user_id)
    rows = s.exec(select(SetupEvent).where(SetupEvent.user_key == key)).all()
    return {r.name: r.at for r in rows if r.name in FUNNEL}


def funnel(s: Session, user_id: str | None) -> list[dict]:
    """Each event with seconds since the previous one reached, in order of time."""
    t = sorted(times(s, user_id).items(), key=lambda kv: kv[1])
    out, prev = [], None
    for name, at in t:
        out.append(
            {
                "name": name,
                "at": at.isoformat(),
                "after_s": None if prev is None else max(0, int((at - prev).total_seconds())),
            }
        )
        prev = at
    return out


@dataclass
class Facts:
    """What the steps are computed from. Injected so the pure logic is testable."""

    github_connected: bool
    github_detail: str
    agent_ok: Callable[[str], bool]  # does this agent's login for the user look good?
    agent_names: dict[str, str]


def steps_for(s: Session, *, user_id: str | None, is_admin: bool, facts: Facts) -> dict:
    """The checklist for one person. `done` comes from real state; `skipped` from their choice."""
    skipped = {
        r.name.removeprefix("skip:")
        for r in s.exec(select(SetupEvent).where(SetupEvent.user_key == key_for(user_id)))
        if r.name.startswith("skip:")
    }
    pref = get(s, user_id, "preferred_agent")
    preferred = pref.value if pref else None

    # repositories: the owner/admins' projects, or the ones a member has jobs on
    q = select(func.count()).select_from(Repo)
    if not is_admin:
        mine = select(Job.repo_id).where(Job.user_id == user_id)
        q = q.where(Repo.id.in_(mine))  # type: ignore[attr-defined]
    repos = s.exec(q).one()

    jobs_q = (
        select(func.count()).select_from(Job).where(Job.kind.in_([JobKind.AGENT, JobKind.PROFILE]))
    )  # type: ignore[attr-defined]
    jobs_q = jobs_q.where(Job.user_id.is_(None) if user_id is None else Job.user_id == user_id)  # type: ignore[union-attr]
    jobs = s.exec(jobs_q).one()

    agent_done, agent_detail = False, "No agent chosen yet"
    if preferred and preferred != "other":
        name = facts.agent_names.get(preferred, preferred)
        agent_done = facts.agent_ok(preferred)
        agent_detail = f"{name} is signed in" if agent_done else f"{name} isn't signed in yet"
    elif preferred == "other":
        agent_detail = "Add agents later"

    if is_admin:
        gh_done, gh_detail = facts.github_connected, facts.github_detail
    else:
        gh_done, gh_detail = True, "Managed by this server's admin"

    raw = {
        "github": (gh_done, gh_detail),
        "agent": (agent_done, agent_detail),
        "project": (repos > 0, f"{repos} repositor{'y' if repos == 1 else 'ies'}" if repos else ""),
        "first_task": (jobs > 0, f"{jobs} task{'s' if jobs != 1 else ''} run" if jobs else ""),
    }
    steps = [
        {
            "id": sid,
            "label": STEP_LABELS[sid],
            "done": done,
            "skipped": sid in skipped and not done,
            "detail": detail,
        }
        for sid, (done, detail) in raw.items()
    ]
    finished = all(x["done"] or x["skipped"] for x in steps)
    nxt = next((x["id"] for x in steps if not x["done"] and not x["skipped"]), None)
    return {
        "steps": steps,
        "done_count": sum(1 for x in steps if x["done"] or x["skipped"]),
        "total": len(steps),
        "next": nxt,
        "complete": finished,
        "dismissed": get(s, user_id, "dismissed") is not None,
        "preferred_agent": preferred,
    }
