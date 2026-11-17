"""Devices screen API: your own signed-in devices, revoking one, and "Add a phone" (a QR code
drawn as an SVG path, plus the typed code)."""

import re

import pytest
import segno
from fastapi.testclient import TestClient

from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.api.devices_api import qr_path
from thinair.auth import create_token
from thinair.config import get_settings
from thinair.db.models import User
from thinair.db.session import session_scope
from thinair.pairing import redeem

ADMIN = "t" * 32
H = {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("THINAIR_PUBLIC_URL", "https://203-0-113-7.sslip.io")
    get_settings.cache_clear()
    api_auth._failures.clear()


@pytest.fixture
def client():
    with TestClient(create_app({})) as c:
        yield c


def member(name="sam", admin=False) -> tuple[str, dict, str]:
    with session_scope() as s:
        u = User(name=name, is_admin=admin)
        s.add(u)
        s.commit()
        tok = create_token(s, "phone", user_id=u.id)
        return u.id, {"Authorization": f"Bearer {tok}"}, name


def modules(path: str) -> set[tuple[int, int]]:
    """The dark squares an SVG path of `M x y h n v1 h-n z` runs describes."""
    out = set()
    for x, y, n in re.findall(r"M(\d+) (\d+)h(\d+)v1h-\d+z", path):
        out |= {(int(x) + i, int(y)) for i in range(int(n))}
    return out


def test_the_svg_path_is_exactly_the_qr_code() -> None:
    text = "https://203-0-113-7.sslip.io/#pair=thinair_pair_" + "x" * 43
    qr = qr_path(text)
    matrix = [list(r) for r in segno.make(text, error="m").matrix]
    expected = {(x + 2, y + 2) for y, row in enumerate(matrix) for x, v in enumerate(row) if v}
    assert modules(qr["path"]) == expected
    assert qr["size"] == len(matrix) + 4  # two-module quiet zone on every side


def test_devices_need_a_login(client) -> None:
    assert client.get("/api/devices").status_code == 401
    assert client.delete("/api/devices/x").status_code == 401
    assert client.post("/api/devices/pair", json={}).status_code == 401


def test_you_see_only_your_own_devices_and_the_current_one_is_marked(client) -> None:
    _, sam, _ = member()
    mine = client.get("/api/devices", headers=H).json()
    assert mine and sum(d["current"] for d in mine) == 1
    theirs = client.get("/api/devices", headers=sam).json()
    assert len(theirs) == 1 and theirs[0]["current"] and theirs[0]["name"] == "phone"
    assert {d["id"] for d in mine}.isdisjoint({d["id"] for d in theirs})


def test_revoking_signs_a_device_out_and_only_your_own_can_be_revoked(client) -> None:
    _, sam, _ = member()
    sam_id = client.get("/api/devices", headers=sam).json()[0]["id"]
    assert client.delete(f"/api/devices/{sam_id}", headers=H).status_code == 404  # not yours
    assert client.get("/api/devices", headers=sam).status_code == 200  # still signed in
    assert client.delete(f"/api/devices/{sam_id}", headers=sam).status_code == 204
    assert client.get("/api/devices", headers=sam).status_code == 401  # signed out
    assert client.delete("/api/devices/nope", headers=H).status_code == 404


def test_add_a_phone_gives_a_qr_and_a_code_that_sign_in_once(client) -> None:
    r = client.post("/api/devices/pair", json={}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == "https://203-0-113-7.sslip.io" and body["expires_in"] == 600
    assert re.fullmatch(r"[0-9A-Z]{4}-[0-9A-Z]{4}", body["short"])
    assert body["qr"]["size"] > 20 and body["qr"]["path"].startswith("M")
    with session_scope() as s:
        token = redeem(s, body["short"])
        assert token and token.startswith("thinair_")
        assert redeem(s, body["short"]) is None  # one use


def test_a_member_pairs_only_their_own_phone(client) -> None:
    uid, sam, _ = member()
    own = client.post("/api/devices/pair", json={}, headers=sam).json()
    with session_scope() as s:
        token = redeem(s, own["short"])
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["user"] == "sam"  # the new phone is sam, not the owner
    r = client.post("/api/devices/pair", json={"user": "admin"}, headers=sam)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "admin_only"


def test_an_admin_can_pair_someone_elses_phone(client) -> None:
    member("sam")
    r = client.post("/api/devices/pair", json={"user": "sam"}, headers=H)
    assert r.status_code == 200
    with session_scope() as s:
        token = redeem(s, r.json()["short"])
    assert (
        client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()["user"] == "sam"
    )
    assert client.post("/api/devices/pair", json={"user": "ghost"}, headers=H).status_code == 404
