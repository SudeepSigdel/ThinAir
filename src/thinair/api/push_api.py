"""Web Push subscriptions: `/api/push`. All routes need a login; a subscription belongs to the API
token that created it and dies with it (see `auth.revoke_token`)."""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import func, select

from thinair import push
from thinair.api.auth import Principal, require_auth
from thinair.config import get_settings
from thinair.db.models import PushSubscription
from thinair.db.session import session_scope

router = APIRouter(prefix="/api/push", dependencies=[Depends(require_auth)], tags=["push"])

MAX_PER_USER = 20  # a few phones and browsers; stops one account filling the table
TEST_COOLDOWN_S = 10.0
_last_test: dict[str, float] = {}


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


def _enabled() -> bool:
    return get_settings().web_push


@router.get("")
def get_push(who: Principal = Depends(require_auth)) -> dict:
    """What the app needs to subscribe: whether it is on, the server's public VAPID key, and
    whether *this* login already has a subscription."""
    if not _enabled():
        return {"enabled": False, "public_key": None, "subscribed": False}
    key = push.load_vapid()
    with session_scope() as s:
        mine = s.exec(select(PushSubscription).where(PushSubscription.token_id == who.token_id))
        subscribed = mine.first() is not None
    return {
        "enabled": True,
        "public_key": key.public_b64u if key else None,
        "subscribed": subscribed,
    }


class Keys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class SubIn(BaseModel):
    endpoint: str = Field(max_length=2000)
    keys: Keys


@router.post("/subscriptions", status_code=201)
def subscribe(body: SubIn, who: Principal = Depends(require_auth)) -> dict:
    if not _enabled():
        raise _err(409, "push_disabled", "Notifications are turned off on this server.")
    if not push.endpoint_allowed(body.endpoint):
        raise _err(
            422,
            "bad_endpoint",
            "That push address isn't one Thinair will send to. Chrome, Firefox, Safari and "
            "Edge work.",
        )
    try:
        push.check_subscription_keys(body.keys.p256dh, body.keys.auth)
    except ValueError as e:
        raise _err(422, "bad_keys", str(e)) from None
    with session_scope() as s:
        existing = s.exec(
            select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
        ).first()
        # One subscription per login: subscribing again replaces the old one.
        for old in s.exec(
            select(PushSubscription).where(PushSubscription.token_id == who.token_id)
        ).all():
            if existing is None or old.id != existing.id:
                s.delete(old)
        if existing is None:
            count = s.exec(
                select(func.count())
                .select_from(PushSubscription)
                .where(
                    PushSubscription.user_id.is_(None)  # type: ignore[union-attr]
                    if who.user_id is None
                    else PushSubscription.user_id == who.user_id
                )
            ).one()
            if count >= MAX_PER_USER:
                raise _err(429, "too_many_devices", "Too many devices are subscribed already.")
            existing = PushSubscription(
                user_id=who.user_id,
                token_id=who.token_id,
                endpoint=body.endpoint,
                p256dh=body.keys.p256dh,
                auth=body.keys.auth,
            )
        else:  # the same browser, a new login: it now belongs to this one
            existing.user_id, existing.token_id = who.user_id, who.token_id
            existing.p256dh, existing.auth, existing.failures = (
                body.keys.p256dh,
                body.keys.auth,
                0,
            )
        s.add(existing)
        s.commit()
    return {"subscribed": True}


class UnsubIn(BaseModel):
    endpoint: str = Field(max_length=2000)


@router.delete("/subscriptions", status_code=204)
def unsubscribe(body: UnsubIn, who: Principal = Depends(require_auth)) -> None:
    with session_scope() as s:
        row = s.exec(
            select(PushSubscription).where(
                PushSubscription.endpoint == body.endpoint,
                PushSubscription.token_id == who.token_id,
            )
        ).first()
        if row is not None:
            s.delete(row)
            s.commit()


@router.post("/test", status_code=202)
async def send_test(who: Principal = Depends(require_auth)) -> dict:
    """A test notification to this person's devices (not other people's)."""
    if not _enabled():
        raise _err(409, "push_disabled", "Notifications are turned off on this server.")
    now = time.monotonic()
    if now - _last_test.get(who.token_id, -1e9) < TEST_COOLDOWN_S:
        raise _err(429, "slow_down", "Wait a few seconds before sending another test.")
    _last_test[who.token_id] = now
    if len(_last_test) > 1000:
        for k in [k for k, t in _last_test.items() if now - t > 3600]:
            del _last_test[k]
    from thinair.notify import WebPushNotifier

    base = (get_settings().public_url or "").rstrip("/")
    payload = {
        "web_push": 8030,
        "notification": {
            "title": "Thinair",
            "body": "Notifications are on. You'll hear from Thinair when a job ends.",
            "navigate": base + "/" if base.startswith("https://") else "/",
            "tag": "test",
        },
    }
    sent = await asyncio.to_thread(WebPushNotifier().deliver, who.user_id, payload)
    return {"sent": sent}
