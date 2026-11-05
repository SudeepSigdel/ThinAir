"""Mobile web UI: served, installable as a PWA, locked down, and free of data without auth."""

import json
import re
import struct

import pytest
from fastapi.testclient import TestClient

from thinair.api.app import create_app
from thinair.api.web import STATIC_DIR


@pytest.fixture
def client():
    with TestClient(create_app({})) as c:
        yield c


def test_page_is_served_without_auth_and_contains_no_data(client) -> None:
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"  # upgrades reach phones immediately
    assert '<link rel="manifest" href="/manifest.webmanifest">' in r.text
    assert "thinair_" not in r.text.replace('placeholder="thinair_…"', "")  # no secrets baked in


def test_every_asset_the_page_references_exists(client) -> None:
    html = client.get("/").text
    refs = re.findall(r'(?:href|src)="(/[^"#]+)"', html)
    assert refs, "expected asset references"
    for ref in refs:
        assert client.get(ref).status_code == 200, ref


def test_security_headers_on_pages_and_api(client) -> None:
    for path in ("/", "/static/app.js", "/healthz", "/api/jobs"):
        h = client.get(path).headers
        assert h["x-content-type-options"] == "nosniff", path
        assert h["x-frame-options"] == "DENY", path
        csp = h["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp, path
        assert "unsafe-inline" not in csp, path
    # FastAPI's docs need their CDN assets, so they are exempt from the CSP
    assert "content-security-policy" not in client.get("/docs").headers


def test_no_inline_scripts_or_styles_so_the_csp_holds() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script> found"
    assert "<style" not in html and " style=" not in html and "onclick" not in html.lower()


def test_ui_code_never_writes_html_from_data() -> None:
    """XSS guard: server text (log lines, repo names) must only go in via textContent."""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in js, sink


def test_manifest_makes_the_app_installable(client) -> None:
    r = client.get("/manifest.webmanifest")
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["display"] == "standalone" and m["start_url"] == "/" and m["name"]
    sizes = {i["sizes"] for i in m["icons"] if i["type"] == "image/png"}
    assert {"192x192", "512x512"} <= sizes
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]


@pytest.mark.parametrize("size", [192, 512])
def test_png_icons_have_the_declared_size(size: int) -> None:
    data = (STATIC_DIR / f"icon-{size}.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (size, size)


def test_service_worker_is_at_the_root_and_never_caches_api_calls(client) -> None:
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache"
    sw = r.text
    assert 'startsWith("/api/")' in sw  # API traffic always goes to the network
    for path in json.loads(
        re.search(r"SHELL = (\[.*?\]);", sw, re.S).group(1).replace(",\n]", "]")
    ):
        assert client.get(path).status_code == 200, path
