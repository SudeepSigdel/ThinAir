import pytest
from sqlalchemy import Engine

from thinair.config import get_settings
from thinair.db.migrate import upgrade
from thinair.db.session import get_engine, make_engine


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own THINAIR_DATA_DIR so nothing touches ./data."""
    monkeypatch.setenv("THINAIR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("THINAIR_SWEEP_ON_START", "false")  # never touch real Docker in unit tests
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()


@pytest.fixture
def engine(tmp_path) -> Engine:
    e = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    upgrade(e)
    return e
