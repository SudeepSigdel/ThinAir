from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_add():
    assert client.get("/add", params={"a": 2, "b": 3}).json() == {"sum": 5}


def test_holds_about_100mb():
    """Deliberately holds ~100 MB of touched memory so the profiler has a known floor to find.

    Python + FastAPI + pytest use roughly 100 MB on their own, so the total is ~200 MB: it passes
    in 256m and gets OOM-killed in 128m. (Allocate once: `bytearray(b"x" * n)` would double it.)
    """
    n = 100 * 1024 * 1024
    block = bytearray(n)
    for i in range(0, n, 4096):  # touch every page so it counts as resident memory
        block[i] = 1
    assert block[0] == 1
