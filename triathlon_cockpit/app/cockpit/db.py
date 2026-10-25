"""SQLite-Datenbank des Cockpits (liegt in /data → in HA-Backups enthalten)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

# Version 1: Trainingsdaten aus TP + bereits das Gerüst für Ernährung (Etappe 3/4).
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Training (Quelle: TrainingPeaks)
CREATE TABLE IF NOT EXISTS tp_workouts (
    workout_id    INTEGER PRIMARY KEY,
    day           TEXT NOT NULL,
    title         TEXT,
    type_id       INTEGER,
    completed     INTEGER,
    planned_h     REAL,
    actual_h      REAL,
    tss_planned   REAL,
    tss_actual    REAL,
    distance_m    REAL,
    np_w          REAL,
    avg_power_w   REAL,
    avg_hr        REAL,
    avg_speed_ms  REAL,
    calories      REAL,
    if_actual     REAL,
    description   TEXT,
    raw           TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tp_workouts_day ON tp_workouts(day);

CREATE TABLE IF NOT EXISTS tp_fitness (
    day  TEXT PRIMARY KEY,
    ctl  REAL, atl REAL, tsb REAL, tss REAL
);

CREATE TABLE IF NOT EXISTS tp_metrics (
    day          TEXT PRIMARY KEY,
    hrv          REAL,
    sleep_h      REAL,
    deep_h       REAL,
    rem_h        REAL,
    rhr          REAL,
    bb_min       REAL,
    bb_max       REAL,
    stress_avg   REAL,
    steps        REAL,
    weight_kg    REAL,
    raw          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started   TEXT NOT NULL,
    finished  TEXT,
    ok        INTEGER,
    message   TEXT
);

-- Ernährung (ab Etappe 3; Tabellen schon angelegt, damit der Notion-Umzug ein Ziel hat)
CREATE TABLE IF NOT EXISTS foods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kcal_100 REAL, protein_100 REAL, carbs_100 REAL, fat_100 REAL, fiber_100 REAL,
    unit_g REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cooked_on TEXT,
    ingredients TEXT,
    portions REAL,
    total_weight_g REAL,
    weighed INTEGER DEFAULT 0,
    kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL, fiber_g REAL,
    leftover_g REAL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eaten_at TEXT NOT NULL,
    day TEXT NOT NULL,
    meal_type TEXT,
    title TEXT NOT NULL,
    items TEXT,
    recipe_id INTEGER REFERENCES recipes(id),
    recipe_grams REAL,
    kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL, fiber_g REAL,
    accuracy TEXT,
    rule_check TEXT,
    warnings TEXT,
    source TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_meals_day ON meals(day);
CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE,
    meal_type TEXT,
    items TEXT,
    kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL, fiber_g REAL
);
CREATE TABLE IF NOT EXISTS body_weight (
    day TEXT PRIMARY KEY,
    weight_kg REAL NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS day_targets (
    day TEXT PRIMARY KEY,
    day_type TEXT,
    kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS nutrition_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule TEXT NOT NULL,
    phase TEXT,
    kind TEXT,
    applies_to TEXT,
    details TEXT,
    severity TEXT,
    valid_from TEXT,
    valid_to TEXT,
    active INTEGER DEFAULT 1,
    source_id INTEGER REFERENCES sources(id)
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    url TEXT,
    kind TEXT,
    topic TEXT,
    retrieved_on TEXT,
    key_points TEXT,
    excerpt TEXT,
    file_path TEXT
);
CREATE TABLE IF NOT EXISTS training_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    start_day TEXT NOT NULL,
    end_day TEXT,
    goal TEXT,
    assessment TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Database:
    def __init__(self, path: Path | str):
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA_V1)
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock, self._conn:
            return int(self._conn.execute(sql, tuple(params)).lastrowid or 0)

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    # ── TP-Daten schreiben ───────────────────────────────────────────────────
    def upsert_workouts(self, workouts: list[dict]) -> int:
        rows = []
        now = _now()
        for w in workouts:
            if w.get("workoutId") is None:
                continue
            rows.append((
                int(w["workoutId"]), str(w.get("workoutDay", ""))[:10], w.get("title"),
                w.get("workoutTypeValueId"), 1 if w.get("completed") or w.get("totalTime") else 0,
                _num(w.get("totalTimePlanned")), _num(w.get("totalTime")),
                _num(w.get("tssPlanned")), _num(w.get("tssActual")),
                _num(w.get("distance")) or _num(w.get("distancePlanned")),
                _num(w.get("normalizedPowerActual")), _num(w.get("powerAverage")),
                _num(w.get("heartRateAverage")), _num(w.get("velocityAverage")),
                _num(w.get("calories")), _num(w.get("if")),
                w.get("description"), json.dumps(w, ensure_ascii=False), now,
            ))
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO tp_workouts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(workout_id) DO UPDATE SET
                     day=excluded.day, title=excluded.title, type_id=excluded.type_id,
                     completed=excluded.completed, planned_h=excluded.planned_h,
                     actual_h=excluded.actual_h, tss_planned=excluded.tss_planned,
                     tss_actual=excluded.tss_actual, distance_m=excluded.distance_m,
                     np_w=excluded.np_w, avg_power_w=excluded.avg_power_w,
                     avg_hr=excluded.avg_hr, avg_speed_ms=excluded.avg_speed_ms,
                     calories=excluded.calories, if_actual=excluded.if_actual,
                     description=excluded.description, raw=excluded.raw,
                     updated_at=excluded.updated_at""",
                rows,
            )
        return len(rows)

    def delete_workouts_missing(self, start: str, end: str, keep_ids: set[int]) -> int:
        """In TP gelöschte Einheiten im abgefragten Zeitraum entfernen."""
        existing = self.query("SELECT workout_id FROM tp_workouts WHERE day BETWEEN ? AND ?", (start, end))
        gone = [r["workout_id"] for r in existing if r["workout_id"] not in keep_ids]
        if gone:
            with self._lock, self._conn:
                self._conn.executemany("DELETE FROM tp_workouts WHERE workout_id=?", [(g,) for g in gone])
        return len(gone)

    def upsert_fitness(self, rows: list[dict]) -> int:
        data = [
            (str(r.get("workoutDay", ""))[:10], _num(r.get("ctl")), _num(r.get("atl")),
             _num(r.get("tsb")), _num(r.get("tssActual")))
            for r in rows if r.get("workoutDay")
        ]
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO tp_fitness VALUES (?,?,?,?,?)
                   ON CONFLICT(day) DO UPDATE SET ctl=excluded.ctl, atl=excluded.atl,
                     tsb=excluded.tsb, tss=excluded.tss""",
                data,
            )
        return len(data)

    def upsert_metrics(self, days: list[dict]) -> int:
        data = [parse_metric_day(d) for d in days if d.get("timeStamp")]
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO tp_metrics VALUES
                   (:day,:hrv,:sleep_h,:deep_h,:rem_h,:rhr,:bb_min,:bb_max,:stress_avg,:steps,:weight_kg,:raw)
                   ON CONFLICT(day) DO UPDATE SET hrv=excluded.hrv, sleep_h=excluded.sleep_h,
                     deep_h=excluded.deep_h, rem_h=excluded.rem_h, rhr=excluded.rhr,
                     bb_min=excluded.bb_min, bb_max=excluded.bb_max, stress_avg=excluded.stress_avg,
                     steps=excluded.steps, weight_kg=excluded.weight_kg, raw=excluded.raw""",
                data,
            )
        return len(data)

    def reparse_metrics(self) -> int:
        """Gespeicherte Rohdaten neu auswerten (nach Verbesserungen am Auslesen)."""
        days = [json.loads(r["raw"]) for r in self.query("SELECT raw FROM tp_metrics")]
        return self.upsert_metrics(days) if days else 0

    # ── Sync-Protokoll ───────────────────────────────────────────────────────
    def log_sync_start(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("INSERT INTO sync_log(started) VALUES(?)", (_now(),))
            return int(cur.lastrowid)

    def log_sync_end(self, sync_id: int, ok: bool, message: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sync_log SET finished=?, ok=?, message=? WHERE id=?",
                (_now(), 1 if ok else 0, message[:500], sync_id),
            )
            self._conn.execute("DELETE FROM sync_log WHERE id < ?", (sync_id - 500,))


# TP-Typnummern (stabiler als die Bezeichnungen) …
_TYPES = {5: "rhr", 6: "sleep_h", 9: "weight_kg", 58: "steps", 60: "hrv"}
# … und Bezeichnungen als Rückfallebene.
_LABELS = {
    "hrv": "hrv",
    "sleep": "sleep_h",
    "sleep hours": "sleep_h",
    "total sleep": "sleep_h",
    "time asleep": "sleep_h",
    "time in deep sleep": "deep_h",
    "time in rem sleep": "rem_h",
    "pulse": "rhr",
    "resting heart rate": "rhr",
    "steps": "steps",
    "weight": "weight_kg",
}


def describe_metric_fields(days: list[dict]) -> list[str]:
    """Welche Felder liefert TP? (für das Protokoll – nur Namen, Typ, Einheit, Beispielwert)"""
    seen: dict[tuple, Any] = {}
    for day in days:
        for d in day.get("details") or []:
            k = (d.get("type"), d.get("label"), d.get("units") or d.get("formatedUnits"))
            seen.setdefault(k, d.get("value"))
    return [f"type={t} label={l!r} units={u!r} z.B.={v!r} → {_metric_key({'type': t, 'label': l})}"
            for (t, l, u), v in sorted(seen.items(), key=lambda x: str(x[0]))]


def _metric_key(d: dict) -> str | None:
    label = " ".join(str(d.get("label", "")).strip().lower().split())
    if label in _LABELS:
        return _LABELS[label]
    try:
        return _TYPES.get(int(d.get("type")))
    except (TypeError, ValueError):
        return None


def _triple(value: Any) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [p.strip() for p in str(value).split(",")] if value not in (None, "") else []


def _hours(value: Any, units: str) -> float | None:
    v = _num(value)
    if v is None:
        return None
    u = (units or "").lower()
    if u.startswith("min"):
        return round(v / 60, 2)
    if u.startswith("s"):
        return round(v / 3600, 2)
    return v


def parse_metric_day(day: dict) -> dict:
    out: dict[str, Any] = {k: None for k in
                           ("hrv", "sleep_h", "deep_h", "rem_h", "rhr", "bb_min", "bb_max",
                            "stress_avg", "steps", "weight_kg")}
    out["day"] = str(day["timeStamp"])[:10]
    for d in day.get("details") or []:
        label = str(d.get("label", "")).strip().lower()
        value = d.get("value")
        key = _metric_key(d)
        if key in ("sleep_h", "deep_h", "rem_h"):
            h = _hours(value, str(d.get("units") or d.get("formatedUnits") or ""))
            if h is not None:
                out[key] = h
        elif key:
            if _num(value) is not None:
                out[key] = _num(value)
        elif label == "body battery" or str(d.get("type")) == "64":
            # TP liefert [min, max, durchschnitt] (früher auch als Text "min,max,durchschnitt")
            parts = _triple(value)
            if len(parts) >= 2:
                out["bb_min"], out["bb_max"] = _num(parts[0]), _num(parts[1])
        elif label == "stress level" or str(d.get("type")) == "62":
            # [min, max, durchschnitt] – wir merken uns den Durchschnitt
            parts = _triple(value)
            if parts:
                out["stress_avg"] = _num(parts[-1])
    out["raw"] = json.dumps(day, ensure_ascii=False)
    return out
