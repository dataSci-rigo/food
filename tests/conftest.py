"""Shared fixtures for the food bot test suite."""
from __future__ import annotations

import sys
import os

# Ensure the food package root is on the path so imports work when running
# pytest from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import config


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Redirect config.DB_PATH to a fresh temp file and run init_db()."""
    db_path = str(tmp_path / "test_food.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    import db
    db.init_db()
    return db_path
