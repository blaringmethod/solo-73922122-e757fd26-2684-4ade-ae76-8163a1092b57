from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "chronology.sqlite3"
    monkeypatch.setenv("TREERING_DB", str(path))
    import app.settings as settings_mod

    importlib.reload(settings_mod)
    import app.main as main_mod

    importlib.reload(main_mod)
    monkeypatch.setattr(main_mod, "settings", settings_mod.settings)
    return path


@pytest.fixture()
def client(db_path):
    import app.main as main_mod

    with TestClient(main_mod.app) as c:
        yield c


@pytest.fixture()
def mods(db_path):
    import app.chronology as chronology
    import app.db as db
    import app.main as main_mod
    import app.settings as settings_mod

    db.init_db(str(db_path))
    return {
        "chronology": chronology,
        "db": db,
        "main": main_mod,
        "settings": settings_mod,
        "path": str(db_path),
    }
