"""
Shared test fixtures.

Every test runs against a throwaway SQLite DB + photo dir under a tmp path, and a
known verification token. No live Super Dispatch calls happen anywhere in the suite
— the SD client's HTTP is mocked in the tests that need it.
"""
from __future__ import annotations
import os

import pytest

import config
import db


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point config at a temp data dir + DB and set a test verification token.

    db.py and listener.py read these config attributes at call time, so overriding
    them here is enough — no re-import needed."""
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "DB_PATH", str(data_dir / "test.db"))
    monkeypatch.setattr(config, "PHOTO_DIR", str(data_dir / "photos"))
    monkeypatch.setattr(config, "SD_WEBHOOK_VERIFICATION_TOKEN", "test-token-123")
    monkeypatch.setattr(config, "WORKER_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(config, "TAG_NAME_MARKERS", ())   # no name filter unless a test sets it
    os.makedirs(data_dir, exist_ok=True)
    db.init_db()
    yield
