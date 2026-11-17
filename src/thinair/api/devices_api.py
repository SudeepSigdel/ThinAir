"""Devices: the phones and browsers signed in as you, and "Add a phone" (a pairing QR code).

A device is one of your API tokens (each paired phone has its own). You see and revoke only your
own; an admin may also make a pairing code for another person. The QR code is drawn here, as an
SVG path, so the page needs no QR library and the code is never rendered by a third party.
"""

from datetime import timedelta

import segno
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import select

from thinair.api.auth import Principal, require_auth
from thinair.auth import revoke_token
from thinair.config import get_settings
from thinair.db.models import ApiToken
from thinair.db.session import session_scope
from thinair.pairing import TTL, create_pairing, pairing_url
from thinair.users import OWNER_NAME, get_user

router = APIRouter(prefix="/api/devices", dependencies=[Depends(require_auth)], tags=["devices"])


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


@router.get("")
def list_devices(who: Principal = Depends(require_auth)) -> list[dict]:
    """Your signed-in devices, newest first. `current` marks the one making this request."""
    with session_scope() as s:
        q = select(ApiToken).where(ApiToken.revoked_at.is_(None))  # type: ignore[union-attr]
        q = q.where(
            ApiToken.user_id.is_(None) if who.user_id is None else ApiToken.user_id == who.user_id
        )  # type: ignore[union-attr]
        rows = s.exec(q.order_by(ApiToken.created_at.desc())).all()  # type: ignore[attr-defined]
        return [
            {
                "id": t.id,
                "name": t.name,
                "created_at": t.created_at.isoformat(),
                "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
                "current": t.id == who.token_id,
            }
            for t in rows
        ]


@router.delete("/{token_id}", status_code=204)
def revoke_device(token_id: str, who: Principal = Depends(require_auth)) -> None:
    """Sign a device out (its token stops working and its notifications stop). Only your own."""
    with session_scope() as s:
        t = s.get(ApiToken, token_id)
        mine = t is not None and t.user_id == who.user_id and t.revoked_at is None
        if not mine:
            raise _err(404, "device_not_found", "No such device.")
        revoke_token(s, token_id)


class PairIn(BaseModel):
    user: str | None = Field(None, max_length=40)  # admins only: pair someone else's phone


def qr_path(text: str) -> dict:
    """A QR code as an SVG path on a `size` x `size` grid (with a 2-module quiet zone)."""
    code = segno.make(text, error="m")
    border = 2
    rows = [list(r) for r in code.matrix]
    parts = []
    for y, row in enumerate(rows):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                parts.append(f"M{start + border} {y + border}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    return {"size": len(rows) + 2 * border, "path": "".join(parts)}


@router.post("/pair")
def add_device(body: PairIn, request: Request, who: Principal = Depends(require_auth)) -> dict:
    """A one-time code (QR and typed form) that signs a phone in as you, valid ten minutes."""
    user_id = who.user_id
    if body.user and body.user != (who.name if who.user_id else OWNER_NAME):
        if not who.is_admin:
            raise _err(403, "admin_only", "Only an admin can pair someone else's phone.")
        with session_scope() as s:
            if body.user == OWNER_NAME:
                user_id = None
            else:
                u = get_user(s, body.user)
                if u is None or u.disabled_at is not None:
                    raise _err(404, "user_not_found", "No such person.")
                user_id = u.id
    base = (get_settings().public_url or "").strip() or str(request.base_url).rstrip("/")
    with session_scope() as s:
        long_code, short = create_pairing(s, user_id, timedelta(seconds=int(TTL.total_seconds())))
    link = pairing_url(base, long_code)
    return {
        "url": base,
        "short": short,
        "expires_in": int(TTL.total_seconds()),
        "qr": qr_path(link),
    }
