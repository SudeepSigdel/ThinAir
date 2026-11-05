"""Serves the mobile web UI (static files) and adds security headers to every response.

The page itself holds no data, so it is served without auth; everything it shows comes from
the authenticated `/api` routes. `/sw.js` and the manifest must live at the site root so the
service worker can control the whole app.
"""

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
NO_CACHE = {"Cache-Control": "no-cache"}

# No inline scripts or styles and no third-party origins: injected markup cannot run code.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)
# FastAPI's interactive docs load their assets from a CDN, so they get no CSP.
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

router = APIRouter(include_in_schema=False)


@router.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)


@router.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript", headers=NO_CACHE)


@router.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("X-Frame-Options", "DENY")
        if not request.url.path.startswith(_DOCS_PATHS):
            h.setdefault("Content-Security-Policy", CSP)
        return response


def mount_web(app: FastAPI) -> None:
    app.add_middleware(SecurityHeaders)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
