"""Auth dependency and login routes.

Two ways to authenticate, both carrying the same API token:
- `Authorization: Bearer <token>`: CLI and scripts.
- `thinair_session` cookie (HttpOnly, SameSite=Strict): the mobile web UI. Browsers' `EventSource`
  cannot set headers, so the cookie is what lets live-log streaming work.
"""

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from thinair.auth import verify_token
from thinair.db.models import ApiToken
from thinair.db.session import session_scope

COOKIE_NAME = "thinair_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30

# Failed-login throttle: 10 failures / 60 s per client address. In-memory (single process).
_FAIL_LIMIT, _FAIL_WINDOW = 10, 60.0
_failures: dict[str, deque[float]] = defaultdict(deque)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _throttled(key: str) -> bool:
    q = _failures[key]
    cutoff = time.monotonic() - _FAIL_WINDOW
    while q and q[0] < cutoff:
        q.popleft()
    return len(q) >= _FAIL_LIMIT


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


def require_auth(
    request: Request, _scheme: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> ApiToken:
    """FastAPI dependency: returns the matching token row or raises 401/429."""
    token = _extract_token(request)
    if not token:
        # Not a guess (e.g. the app checking whether it is logged in), so it neither counts
        # toward nor is blocked by the throttle. Otherwise reloading a logged-out page could
        # lock the owner out.
        raise _unauthorized()
    key = _client_key(request)
    if _throttled(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts; wait.")
    with session_scope() as session:
        row = verify_token(session, token)
        if row is not None:
            session.expunge(row)
            return row
    _failures[key].append(time.monotonic())
    raise _unauthorized()


router = APIRouter(prefix="/api", tags=["auth"])


class LoginBody(BaseModel):
    token: str


@router.post("/login", status_code=status.HTTP_204_NO_CONTENT)
def login(body: LoginBody, request: Request, response: Response) -> None:
    key = _client_key(request)
    if _throttled(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts; wait.")
    with session_scope() as session:
        ok = verify_token(session, body.token) is not None
    if not ok:
        _failures[key].append(time.monotonic())
        raise _unauthorized()
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        COOKIE_NAME,
        body.token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me")
def me(token: ApiToken = Depends(require_auth)) -> dict[str, str]:
    return {"token_name": token.name}
