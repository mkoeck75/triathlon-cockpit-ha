"""Wichtige Kennzahlen als HA-Sensoren (sensor.triathlon_* / binary_sensor.triathlon_*).

Geschrieben über die HA-REST-API (POST /api/states/…), die die App ohnehin für den
KI-Task nutzt – keine zusätzliche Bibliothek, keine weiteren Rechte. Die Sensoren
werden alle 5 Minuten, nach jedem TP-Abgleich und nach Änderungen neu gesetzt
(nach einem HA-Neustart sind sie also spätestens nach 5 Minuten wieder da).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date

import aiohttp

from .api import TYPE_NAMES
from .db import Database
from .sync import SyncService

log = logging.getLogger(__name__)
INTERVAL = 300


def _avg(vals):
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 1) if v else None


def _r(v, n=1):
    return None if v is None else round(v, n)


def collect(db: Database, sync: SyncService, today: date) -> dict[str, tuple]:
    """entity_id → (state, attributes). Reine Datenbank-Logik, ohne Netzwerk (testbar)."""
    ds = today.isoformat()
    out: dict[str, tuple] = {}
    base = {"friendly_name": None}

    fit = db.query("SELECT * FROM tp_fitness WHERE day <= ? ORDER BY day DESC LIMIT 1", (ds,))
    f = fit[0] if fit else {}
    for key, name, field in (("fitness_ctl", "Triathlon Fitness (CTL)", "ctl"),
                             ("ermuedung_atl", "Triathlon Ermüdung (ATL)", "atl"),
                             ("form_tsb", "Triathlon Form (TSB)", "tsb")):
        out[f"sensor.triathlon_{key}"] = (_r(f.get(field)), base | {"friendly_name": name, "state_class": "measurement",
                                                                     "icon": "mdi:chart-bell-curve", "stand": f.get("day")})

    mets = db.query("SELECT day, hrv, rhr, sleep_h FROM tp_metrics WHERE day <= ? ORDER BY day DESC LIMIT 8", (ds,))
    cur = mets[0] if mets and mets[0]["day"] == ds else None
    prev = mets[1:] if cur else mets
    hrv7, rhr7, sleep7 = _avg(m["hrv"] for m in prev[:7]), _avg(m["rhr"] for m in prev[:7]), _avg(m["sleep_h"] for m in prev[:7])
    out["sensor.triathlon_hrv"] = (cur and cur["hrv"], base | {
        "friendly_name": "Triathlon HRV", "unit_of_measurement": "ms", "state_class": "measurement", "icon": "mdi:heart-pulse",
        "schnitt_7_tage": hrv7, "abweichung_prozent": _r((cur["hrv"] - hrv7) / hrv7 * 100, 0) if cur and cur["hrv"] and hrv7 else None})
    out["sensor.triathlon_ruhepuls"] = (cur and cur["rhr"], base | {
        "friendly_name": "Triathlon Ruhepuls", "unit_of_measurement": "bpm", "state_class": "measurement",
        "icon": "mdi:heart", "schnitt_7_tage": rhr7,
        "abweichung": _r(cur["rhr"] - rhr7) if cur and cur["rhr"] and rhr7 else None})
    out["sensor.triathlon_schlaf"] = (_r(cur and cur["sleep_h"], 2), base | {
        "friendly_name": "Triathlon Schlaf", "unit_of_measurement": "h", "state_class": "measurement",
        "icon": "mdi:sleep", "schnitt_7_tage": sleep7})

    tot = db.query("SELECT SUM(kcal) k, SUM(protein_g) e, SUM(carbs_g) kh, SUM(fat_g) f, COUNT(*) n "
                   "FROM meals WHERE day = ?", (ds,))[0]
    tgt = (db.query("SELECT * FROM day_targets WHERE day = ?", (ds,)) or [{}])[0]
    kcal = _r(tot["k"] or 0, 0)
    rest = _r(tgt["kcal"] - kcal, 0) if tgt.get("kcal") is not None else None
    out["sensor.triathlon_kcal_heute"] = (kcal, base | {
        "friendly_name": "Triathlon kcal heute", "unit_of_measurement": "kcal", "state_class": "measurement",
        "icon": "mdi:food-apple", "soll": tgt.get("kcal"), "rest": rest, "eiweiss_g": _r(tot["e"] or 0, 0),
        "kh_g": _r(tot["kh"] or 0, 0), "fett_g": _r(tot["f"] or 0, 0), "mahlzeiten": tot["n"],
        "tagestyp": tgt.get("day_type")})
    out["sensor.triathlon_kcal_rest"] = (rest, base | {
        "friendly_name": "Triathlon kcal übrig", "unit_of_measurement": "kcal", "icon": "mdi:food-variant"})
    out["sensor.triathlon_eiweiss_heute"] = (_r(tot["e"] or 0, 0), base | {
        "friendly_name": "Triathlon Eiweiß heute", "unit_of_measurement": "g", "state_class": "measurement",
        "icon": "mdi:food-steak", "soll": tgt.get("protein_g")})

    w = db.query("SELECT day, weight_kg, 'eingetragen' src FROM body_weight UNION ALL "
                 "SELECT day, weight_kg, 'Garmin/TP' FROM tp_metrics WHERE weight_kg IS NOT NULL "
                 "ORDER BY day DESC LIMIT 1")
    out["sensor.triathlon_gewicht"] = (w[0]["weight_kg"] if w else None, base | {
        "friendly_name": "Triathlon Gewicht", "unit_of_measurement": "kg", "state_class": "measurement",
        "device_class": "weight", "icon": "mdi:scale-bathroom", "datum": w[0]["day"] if w else None,
        "quelle": w[0]["src"] if w else None,
        "tage_seit_letzter_messung": (today - date.fromisoformat(w[0]["day"])).days if w else None})

    todays = db.query("SELECT title, type_id, completed, planned_h, tss_planned FROM tp_workouts WHERE day = ? "
                      "ORDER BY workout_id", (ds,))
    open_today = [x for x in todays if not x["completed"] and x["type_id"] != 7]
    out["sensor.triathlon_einheiten_heute"] = (len([x for x in todays if x["type_id"] != 7]), base | {
        "friendly_name": "Triathlon Einheiten heute", "icon": "mdi:run", "offen": len(open_today),
        "einheiten": [f"{'✓ ' if x['completed'] else ''}{x['title']}" for x in todays if x["type_id"] != 7]})
    nxt = db.query("SELECT day, title, type_id, planned_h, tss_planned FROM tp_workouts WHERE day >= ? "
                   "AND completed = 0 AND type_id != 7 ORDER BY day, workout_id LIMIT 1", (ds,))
    n = nxt[0] if nxt else None
    out["sensor.triathlon_naechste_einheit"] = ((n["title"] or "")[:250] if n else "keine geplant", base | {
        "friendly_name": "Triathlon nächste Einheit", "icon": "mdi:calendar-clock", "tag": n["day"] if n else None,
        "sportart": TYPE_NAMES.get(n["type_id"]) if n else None, "dauer_h": _r(n["planned_h"], 2) if n else None,
        "tss": _r(n["tss_planned"], 0) if n else None})

    pend = db.query("SELECT id, summary FROM training_changes WHERE status = 'proposed' ORDER BY id")
    out["sensor.triathlon_offene_trainingsaenderungen"] = (len(pend), base | {
        "friendly_name": "Triathlon offene Trainingsänderungen", "icon": "mdi:calendar-edit",
        "vorschlaege": [p["summary"] for p in pend]})

    st = sync.status
    out["binary_sensor.triathlon_trainingpeaks_problem"] = ("on" if (st.auth_problem or st.last_error) else "off", base | {
        "friendly_name": "Triathlon TrainingPeaks-Problem", "device_class": "problem",
        "anmeldung_abgelaufen": st.auth_problem, "fehler": st.last_error, "letzter_abgleich": st.last_ok})
    out["sensor.triathlon_letzter_abgleich"] = (st.last_ok, base | {
        "friendly_name": "Triathlon letzter TP-Abgleich", "device_class": "timestamp", "icon": "mdi:sync"})
    return out


class SensorPublisher:
    def __init__(self, db: Database, sync: SyncService, session: aiohttp.ClientSession, today=date.today,
                 base_url: str = "http://supervisor/core/api", token: str | None = None):
        self.db, self.sync, self.session, self.today = db, sync, session, today
        self.base = base_url.rstrip("/")
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")
        self._task: asyncio.Task | None = None
        self._warned = False

    @property
    def available(self) -> bool:
        return bool(self.token)

    async def publish(self) -> int:
        if not self.available:
            return 0
        n = 0
        for entity, (state, attrs) in collect(self.db, self.sync, self.today()).items():
            payload = {"state": "unknown" if state is None else str(state),
                       "attributes": {k: v for k, v in attrs.items() if v is not None}}
            try:
                async with self.session.post(f"{self.base}/states/{entity}", json=payload,
                                             headers={"Authorization": f"Bearer {self.token}"},
                                             timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status < 300:
                        n += 1
                    elif not self._warned:
                        log.warning("Sensor %s: HTTP %s", entity, resp.status)
                        self._warned = True
            except (aiohttp.ClientError, TimeoutError) as err:
                if not self._warned:
                    log.warning("HA nicht erreichbar für Sensoren: %s", err.__class__.__name__)
                    self._warned = True
                return n
        return n

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.publish()
            except Exception:
                log.warning("Sensoren konnten nicht gesetzt werden", exc_info=True)
            await asyncio.sleep(INTERVAL)

    def start(self) -> None:
        if self.available and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="ha-sensors")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
