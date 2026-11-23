"""Runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    app_name: str = "林芯 · 树轮年表 API"
    version: str = "1.0.0"


def get_settings() -> Settings:
    raw = os.environ.get("TREERING_DB", "/data/treering/chronology.sqlite3")
    return Settings(db_path=Path(raw).expanduser())


settings = get_settings()
