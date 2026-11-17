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


SCRIPTS = sorted(p.name for p in STATIC_DIR.glob("*.js"))


def test_ui_code_never_writes_html_from_data() -> None:
    """XSS guard: server text (log lines, repo names, diffs) must only go in via textContent.
    Covers every script the page loads (the UI is split into several plain files)."""
    assert {"app.js", "diff.js", "sw.js"} | {
        f"views-{n}.js" for n in ("jobs", "account", "setup", "settings", "diff")
    } <= set(SCRIPTS)
    for name in SCRIPTS:
        js = (STATIC_DIR / name).read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert sink not in js, f"{name}: {sink}"


def test_every_script_file_is_loaded_by_the_page_and_cached_by_the_service_worker() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    sw = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    for name in SCRIPTS:
        if name == "sw.js":
            continue
        assert f'src="/static/{name}"' in html, f"{name} is not loaded by index.html"
        assert f'"/static/{name}"' in sw, f"{name} is not in the service worker's shell"
    assert "/static/icons.svg" in sw


# ---- design tokens: WCAG AA contrast for every text/background pair (docs/plans/onboarding-v2.md 5.2)
def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def _blocks() -> tuple[dict[str, str], dict[str, str]]:
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    light = _tokens(css[css.index(":root {") : css.index("@media (prefers-color-scheme: dark)")])
    dark_start = css.index("@media (prefers-color-scheme: dark)")
    dark = _tokens(css[dark_start : css.index("* { box-sizing", dark_start)])
    return light, dark


def _luminance(hex_color: str) -> float:
    def chan(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT_PAIRS = [
    ("ink", "bg"), ("ink", "surface"), ("ink", "surface-2"),
    ("ink-2", "bg"), ("ink-2", "surface"), ("ink-2", "surface-2"),
    ("ink-3", "bg"), ("ink-3", "surface"), ("ink-3", "surface-2"),  # captions, placeholders
    ("accent", "bg"), ("accent", "surface"),  # links
    ("accent-ink", "accent"),  # button labels
    ("danger-ink", "danger"),
    ("ok", "bg"), ("ok", "surface"), ("warn", "bg"), ("warn", "surface"),
    ("danger", "bg"), ("danger", "surface"), ("info", "bg"), ("info", "surface"),
    ("code-ink", "code-bg"),
    ("ink", "add-bg"), ("ink", "del-bg"), ("ink-3", "add-bg"), ("ink-3", "del-bg"),
    ("ok", "add-bg"), ("danger", "del-bg"),  # the +N / -N numbers on diff lines
    ("ink", "accent-soft"),
]  # fmt: skip


@pytest.mark.parametrize("mode", [0, 1], ids=["light", "dark"])
def test_palette_meets_wcag_aa_in_light_and_dark(mode) -> None:
    light, dark = _blocks()
    tokens = light if mode == 0 else {**light, **dark}  # dark overrides light
    failures = []
    for fg, bg in TEXT_PAIRS:
        ratio = contrast(tokens[fg], tokens[bg])
        if ratio < 4.5:
            failures.append(f"{fg} on {bg}: {ratio:.2f}")
    assert not failures, failures


def test_the_accent_fix_that_motivated_the_new_palette() -> None:
    light, _ = _blocks()
    assert contrast("#ffffff", "#0d9488") < 4.5  # yesterday's accent failed AA for button labels
    assert contrast(light["accent-ink"], light["accent"]) >= 5.0


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


@pytest.fixture
def docs_mode(monkeypatch):
    from thinair.config import get_settings

    def make(mode: str | None):
        if mode is None:
            monkeypatch.delenv("THINAIR_API_DOCS", raising=False)
        else:
            monkeypatch.setenv("THINAIR_API_DOCS", mode)
        monkeypatch.setenv("THINAIR_ADMIN_TOKEN", "d" * 40)
        get_settings.cache_clear()
        return TestClient(create_app({}))

    return make


def test_api_docs_need_a_login_by_default(docs_mode) -> None:
    """The server may be on the internet: the schema maps every route, so it is not public."""
    with docs_mode(None) as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path).status_code == 401, path
        auth = {"Authorization": "Bearer " + "d" * 40}
        assert "swagger" in c.get("/docs", headers=auth).text.lower()
        schema = c.get("/openapi.json", headers=auth).json()
        assert "/api/jobs" in schema["paths"]
        assert "/openapi.json" not in schema["paths"]  # the docs routes stay out of the schema
        assert "content-security-policy" not in c.get("/docs", headers=auth).headers


def test_api_docs_open_and_off(docs_mode) -> None:
    with docs_mode("open") as c:
        assert c.get("/docs").status_code == 200 and c.get("/openapi.json").status_code == 200
    with docs_mode("off") as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert c.get(path).status_code in (404, 405), path


def test_hsts_only_over_https_and_never_for_subdomains() -> None:
    with TestClient(create_app({}), base_url="https://thinair.test") as c:
        h = c.get("/healthz").headers["strict-transport-security"]
        assert h == "max-age=31536000"  # sslip.io subdomains belong to other people
    with TestClient(create_app({})) as c:
        assert "strict-transport-security" not in c.get("/healthz").headers
