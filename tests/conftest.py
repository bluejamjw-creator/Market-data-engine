"""Test configuration: fake credentials and a throwaway SQLite database.

Set before any application module is imported, so tests can never touch
the real database or spend provider quota.
"""
import os

import pytest

os.environ["DATABASE_URL"] = "sqlite:///./_unused_test.db"
os.environ["MDE_DISABLE_SCHEDULER"] = "1"
os.environ["TWELVE_DATA_API_KEY"] = "td-test-key-0001"
os.environ["ALPHA_VANTAGE_API_KEY"] = "av-test-key-0002"
os.environ["COINMARKETCAP_API_KEY"] = "cmc-test-key-0003"
os.environ.pop("ADMIN_TOKEN", None)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh SQLite database with the application schema."""
    pytest.importorskip("sqlalchemy")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    from app import database
    from app.config import get_settings
    from app.models import Base

    get_settings.cache_clear()
    database.reset_engine()
    Base.metadata.create_all(database.get_engine())
    yield database
    database.reset_engine()
    get_settings.cache_clear()
