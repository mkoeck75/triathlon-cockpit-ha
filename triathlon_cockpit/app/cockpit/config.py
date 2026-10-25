"""Add-on-Konfiguration aus /data/options.json (vom HA-Supervisor geschrieben)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(os.environ.get("COCKPIT_DATA_DIR", "/data"))


@dataclass(frozen=True)
class Config:
    tp_auth_cookie: str = ""
    sync_interval_min: int = 30
    history_days: int = 120
    plan_days_ahead: int = 21
    log_level: str = "info"
    timezone: str = "Europe/Vienna"
    port: int = 8099

    @property
    def db_path(self) -> Path:
        return DATA_DIR / "cockpit.sqlite3"


def load_config() -> Config:
    path = DATA_DIR / "options.json"
    raw: dict = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    known = {k: v for k, v in raw.items() if k in Config.__dataclass_fields__}
    # Der Supervisor setzt TZ passend zur HA-Zeitzone.
    known.setdefault("timezone", os.environ.get("TZ", "Europe/Vienna"))
    if "COCKPIT_PORT" in os.environ:
        known["port"] = int(os.environ["COCKPIT_PORT"])
    return Config(**known)
