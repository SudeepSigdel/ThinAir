"""Auth dependency and login routes.

Two ways to authenticate, both carrying the same API token:
- `Authorization: Bearer <token>`: CLI and scripts.
- `thinair_session` cookie (HttpOnly, SameSite=Strict): the mobile web UI. Browsers' `EventSource`
  cannot set headers, so the cookie is what lets live-log streaming work.
"""

import ipaddress
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from thinair.auth import verify_token
from thinair.db.session import session_scope
from thinair.users import Principal, principal_for

COOKIE_NAME = "thinair_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30

# Failed-login throttle: 10 failures / 60 s per client address (IPv6: per /64, since one host
# owns a whole /64). In-memory (single process), capped so many addresses can't grow it forever.
#
# While an address is throttled, a *long* valid token (>= 32 chars, e.g. every generated
# `thinair_...` token: 256 bits, unguessable) is still accepted. Behind a reverse proxy everyone
# shares one address, so otherwise a stranger sending wrong tokens could lock the owner out.
# Shorter tokens (a hand-picked THINAIR_ADMIN_TOKEN of 20-31 chars) are refused unchecked while
# throttled, which keeps online guessing slow where guessing could matter.
_FAIL_LIMIT, _FAIL_WINDOW = 10, 60.0
_MAX_KEYS = 10_000
STRONG_TOKEN_LEN = 32
_failures: dict[str, deque[float]] = defaultdict(deque)


def _client_key(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _throttled(key: str) -> bool:
    q = _failures.get(key)
    if not q:
        return False
    cutoff = time.monotonic() - _FAIL_WINDOW
    while q and q[0] < cutoff:
        q.popleft()
    if not q:
        del _failures[key]
        return False
    return len(q) >= _FAIL_LIMIT


def record_failure(key: str) -> None:
    if key not in _failures and len(_failures) >= _MAX_KEYS:
        cutoff = time.monotonic() - _FAIL_WINDOW
        for k in [k for k, q in _failures.items() if not q or q[-1] < cutoff]:
            del _failures[k]
        if len(_failures) >= _MAX_KEYS:  # still full: forget the oldest addresses
            for k in list(_failures)[: _MAX_KEYS // 10]:
                del _failures[k]
    _failures[key].append(time.monotonic())


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return request.cookies.get(COOKIE_NAME)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid API token. Send 'Authorization: Bearer <token>' or log in.",
        headers={"WWW-Authenticate": "Bearer"},
    )


# Declares the bearer scheme in the OpenAPI schema, so /docs shows an "Authorize" button. It never
# rejects by itself (auto_error=False): the cookie used by the web app is still accepted below.
_bearer = HTTPBearer(auto_error=False, description="Your Thinair API token")


def _principal(token: str) -> Principal | None:
    with session_scope() as session:
        row = verify_token(session, token)
        return principal_for(session, row) if row is not None else None


def require_auth(
    request: Request, _scheme: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> Principal:
    """FastAPI dependency: returns who is calling, or raises 401/429. A disabled user's tokens
    are revoked, and are refused here even if one slipped through."""
    token = _extract_token(request)
    if not token:
        # Not a guess (e.g. the app checking whether it is logged in), so it neither counts
        # toward nor is blocked by the throttle. Otherwise reloading a logged-out page could
        # lock the owner out.
        raise _unauthorized()
    return _check(request, token)


def _check(request: Request, token: str) -> Principal:
    key = _client_key(request)
    throttled = _throttled(key)
    if throttled and len(token) < STRONG_TOKEN_LEN:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts; wait.")
    who = _principal(token)
    if who is not None:
        return who
    if throttled:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts; wait.")
    record_failure(key)
    raise _unauthorized()


def require_admin(who: Principal = Depends(require_auth)) -> Principal:
    if not who.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"code": "admin_only", "message": "Only an admin can do that."},
        )
    return who


router = APIRouter(prefix="/api", tags=["auth"])


class LoginBody(BaseModel):
    token: str


@router.post("/login", status_code=status.HTTP_204_NO_CONTENT)
def login(body: LoginBody, request: Request, response: Response) -> None:
    _check(request, body.token)
    _set_session(request, response, body.token)


def _set_session(request: Request, response: Response, token: str) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


class PairBody(BaseModel):
    code: str = Field(max_length=200)


@router.post("/pair", status_code=status.HTTP_204_NO_CONTENT)
def pair(body: PairBody, request: Request, response: Response) -> None:
    """Sign this browser in with a one-time code from `thinair qr` (a new token of its own)."""
    from thinair.pairing import redeem

    key = _client_key(request)
    if _throttled(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts; wait.")
    with session_scope() as session:
        token = redeem(session, body.code.strip())
    if token is None:
        record_failure(key)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "bad_pairing_code",
                "message": "This QR code has expired or was already used. Create a new one "
                "with `thinair qr`.",
            },
        )
    _set_session(request, response, token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me")
def me(who: Principal = Depends(require_auth)) -> dict[str, str | bool]:
    return {"token_name": who.token_name, "user": who.name, "is_admin": who.is_admin}
