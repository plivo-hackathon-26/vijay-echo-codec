"""Shared pytest fixtures.

Tests run against an isolated temp SQLite DB so they don't pollute
the dev mirror.db. We monkeypatch db.DB_PATH for the duration of the
session.
"""

import os
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_db():
    import db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    original = db.DB_PATH
    db.DB_PATH = tmp.name
    db.init_db()
    yield
    db.DB_PATH = original
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
