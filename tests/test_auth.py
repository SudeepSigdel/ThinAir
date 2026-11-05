import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from typer.testing import CliRunner

from thinair import auth
from thinair.api import auth as api_auth
from thinair.api.app import create_app
from thinair.cli.main import app as cli_app
from thinair.config import get_settings
from thinair.db.models import ApiToken


@pytest.fixture(autouse=True)
def _reset_throttle():
    api_auth._failures.clear()


def test_only_hash_is_stored(engine) -> None:
    with Session(engine) as s:
        plain = auth.create_token(s, "t")
        row = s.exec(select(ApiToken)).one()
        assert plain.startswith("thinair_")
        assert plain not in (row.token_hash, row.name)
        assert auth.verify_token(s, plain) is not None
        assert auth.verify_token(s, plain + "x") is None


def test_revoked_token_rejected(engine) -> None:
    with Session(engine) as s:
        plain = auth.create_token(s, "t")
        row = s.exec(select(ApiToken)).one()
        assert auth.revoke_token(s, row.id)
        assert auth.verify_token(s, plain) is None
        assert not auth.revoke_token(s, row.id)


def test_bootstrap_generates_once(engine) -> None:
    with Session(engine) as s:
        first = auth.bootstrap_admin_token(s, None)
        assert first and auth.verify_token(s, first)
        assert auth.bootstrap_admin_token(s, None) is None


def test_bootstrap_env_token_and_rotation(engine) -> None:
    with Session(engine) as s:
        a, b = "a" * 24, "b" * 24
        assert auth.bootstrap_admin_token(s, a) is None
        assert auth.verify_token(s, a)
        auth.bootstrap_admin_token(s, a)  # idempotent
        auth.bootstrap_admin_token(s, b)  # rotation revokes the old one
        assert auth.verify_token(s, a) is None
        assert auth.verify_token(s, b)
        auth.bootstrap_admin_token(s, a)  # re-supplying a revoked token re-enables it
        assert auth.verify_token(s, a)
        assert auth.verify_token(s, b) is None


def test_short_env_token_rejected(engine) -> None:
    with Session(engine) as s, pytest.raises(ValueError):
        auth.bootstrap_admin_token(s, "short")


TOKEN = "t" * 32


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("THINAIR_ADMIN_TOKEN", TOKEN)
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        yield c


def test_api_requires_token(client) -> None:
    assert client.get("/api/me").status_code == 401
    assert client.get("/healthz").status_code == 200  # health stays open


def test_bearer_auth(client) -> None:
    r = client.get("/api/me", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json() == {"token_name": "env-admin"}
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_cookie_login_flow(client) -> None:
    assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
    r = client.post("/api/login", json={"token": TOKEN})
    assert r.status_code == 204
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/me").status_code == 200  # cookie jar carries the session
    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401


def test_failed_attempts_are_throttled(client) -> None:
    wrong = {"Authorization": "Bearer wrong-guess"}
    for _ in range(10):
        assert client.get("/api/me", headers=wrong).status_code == 401
    assert client.get("/api/me", headers=wrong).status_code == 429
    # even a correct token is refused while throttled (limits online guessing)
    assert client.get("/api/me", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 429
    # wrong logins count too
    assert client.post("/api/login", json={"token": "x"}).status_code == 429


def test_requests_without_a_token_never_lock_anyone_out(client) -> None:
    """Regression: a logged-out app checking /api/me on every reload counted as failed guesses,
    so reloading the login page ~10 times locked the owner out."""
    for _ in range(30):
        assert client.get("/api/me").status_code == 401
    assert client.post("/api/login", json={"token": TOKEN}).status_code == 204


def test_cli_token_commands() -> None:
    runner = CliRunner()
    created = runner.invoke(cli_app, ["token", "create", "--name", "phone"])
    assert created.exit_code == 0 and created.stdout.strip().startswith("thinair_")
    listed = runner.invoke(cli_app, ["token", "list"])
    assert "phone" in listed.stdout and "active" in listed.stdout
    token_id = listed.stdout.split()[0]
    assert runner.invoke(cli_app, ["token", "revoke", token_id]).exit_code == 0
    assert runner.invoke(cli_app, ["token", "revoke", token_id]).exit_code == 1
