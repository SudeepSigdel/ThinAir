from fastapi.testclient import TestClient

from thinair.api.app import create_app


def test_healthz_and_startup_migration() -> None:
    with TestClient(create_app()) as client:  # context manager runs lifespan (migrations)
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
