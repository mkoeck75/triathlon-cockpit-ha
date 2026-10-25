"""JSON-API für Claude (über den HA-MCP-Connector) und die Oberfläche.

Alles unter /api/v1. ``GET /api/v1`` beschreibt sich selbst, damit Claude ohne
Vorwissen die richtigen Aufrufe findet. Löschen ist bewusst nicht vorgesehen –
Korrekturen gehen per PATCH.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any

from aiohttp import web

from . import __version__
from .ai import EstimateError, MealEstimator, parse_when
from .catalog import ENTITY_DOCS, register_catalog_api
from .db import Database
from .sync import SyncService

TYPE_NAMES = {1: "Schwimmen", 2: "Rad", 3: "Laufen", 4: "Koppel", 5: "Crosstraining", 6: "Rennen",
              7: "Notiz", 8: "MTB", 9: "Kraft", 10: "Sonstiges", 11: "Langlauf", 12: "Rudern",
              13: "Gehen", 29: "Kraft/Mobilität", 100: "Sonstiges"}

MEAL_TYPES = ["Frühstück", "Mittagessen", "Abendessen", "Snack", "Training/Wettkampf", "Getränk", "Supplement"]
ACCURACY = ["Exakt (Stamm/Etikett)", "Gut geschätzt", "Grob geschätzt"]
RULE_CHECK = ["OK", "Hinweis", "Warnung"]
MEAL_FIELDS = ["eaten_at", "meal_type", "title", "items", "recipe_id", "recipe_grams", "kcal", "protein_g",
               "carbs_g", "fat_g", "fiber_g", "calcium_mg", "accuracy", "rule_check", "warnings", "note", "source"]
NUM_FIELDS = {"recipe_id", "recipe_grams", "kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "calcium_mg"}
# Felder aus den TP-Rohdaten, die für die Analyse einer Einheit interessant sind
WORKOUT_DETAIL_KEYS = [
    "title", "workoutDay", "startTime", "workoutTypeValueId", "totalTime", "totalTimePlanned",
    "distance", "distancePlanned", "tssActual", "tssPlanned", "if", "ifPlanned", "normalizedPowerActual",
    "powerAverage", "powerMaximum", "heartRateAverage", "heartRateMaximum", "heartRateMinimum",
    "velocityAverage", "velocityMaximum", "velocityPlanned", "normalizedSpeedActual", "cadenceAverage",
    "elevationGain", "elevationLoss", "calories", "energy", "tempAvg", "feeling", "rpe", "description",
    "coachComments", "athleteComments", "userTags", "completed", "structure",
]

API_INDEX = {
    "name": "Triathlon-Cockpit API",
    "hint": "Datumswerte als YYYY-MM-DD, Zeitpunkte als ISO 8601 in Ortszeit (z. B. 2026-10-25T12:30). "
            "Trainingsdaten kommen aus TrainingPeaks (Abgleich alle 30 Min, sofort mit POST /api/v1/sync). "
            "Dauer in Stunden (Dezimal), Distanz in Metern, Geschwindigkeit in m/s. Nichts wird gelöscht.",
    "endpoints": [
        {"method": "GET", "path": "/api/v1/day", "params": "date (Standard: heute)",
         "does": "Tagesüberblick: Einheiten, Form, Erholung + 7-Tage-Schnitt, Mahlzeiten, Summen, Tagesziel, Gewicht"},
        {"method": "GET", "path": "/api/v1/workouts", "params": "start, end (max. 180 Tage), type (optional, z. B. Laufen)",
         "does": "Einheiten im Zeitraum mit Kennzahlen"},
        {"method": "GET", "path": "/api/v1/workouts/{workout_id}",
         "does": "Details einer Einheit inkl. Beschreibung, Kommentare, Struktur, Puls/Leistung/Pace"},
        {"method": "GET", "path": "/api/v1/weeks", "params": "weeks (Standard 8, max. 52)",
         "does": "Wochenübersicht: Stunden/TSS je Sportart (Plan vs. Ist), CTL-Verlauf, Erholung, kcal"},
        {"method": "GET", "path": "/api/v1/recovery", "params": "days (Standard 28)",
         "does": "HRV, Ruhepuls, Schlaf, Body Battery, Stress, Gewicht je Tag"},
        {"method": "GET", "path": "/api/v1/fitness", "params": "days (Standard 120)",
         "does": "CTL/ATL/TSB/TSS je Tag"},
        {"method": "GET", "path": "/api/v1/meals", "params": "date (Standard: heute)", "does": "Mahlzeiten eines Tages"},
        {"method": "POST", "path": "/api/v1/meals",
         "body": {"eaten_at": "ISO-Zeitpunkt (Standard: jetzt)", "title": "Kurzbeschreibung ohne Uhrzeit (Pflicht)",
                  "meal_type": MEAL_TYPES, "items": "Zutaten & Mengen (Text)",
                  "kcal": "Zahl", "protein_g": "Zahl", "carbs_g": "Zahl", "fat_g": "Zahl", "fiber_g": "Zahl",
                  "accuracy": ACCURACY, "rule_check": RULE_CHECK, "warnings": "Text",
                  "recipe_id": "optional", "recipe_grams": "optional"},
         "does": "Mahlzeit eintragen; Antwort enthält die neuen Tagessummen"},
        {"method": "PATCH", "path": "/api/v1/meals/{meal_id}", "body": "beliebige Felder wie bei POST",
         "does": "Mahlzeit korrigieren"},
        {"method": "POST", "path": "/api/v1/weight", "body": {"day": "YYYY-MM-DD (Standard heute)", "weight_kg": "Zahl", "note": "optional"},
         "does": "Gewicht (morgens nüchtern) eintragen"},
        {"method": "PUT", "path": "/api/v1/targets/{day}",
         "body": {"day_type": "Ruhetag|Locker|Mittel|Hart-Lang|Rennen", "kcal": "Zahl", "protein_g": "Zahl",
                  "carbs_g": "Zahl", "fat_g": "Zahl", "note": "optional"},
         "does": "Tagesziel setzen bzw. überschreiben"},
        {"method": "GET", "path": "/api/v1/rules", "params": "on (Datum, Standard heute), all=1 für alle",
         "does": "An diesem Tag gültige, aktive Ernährungsregeln"},
        {"method": "POST", "path": "/api/v1/sync", "does": "Sofort mit TrainingPeaks abgleichen"},
        {"method": "GET", "path": "/api/v1/nutrition", "params": "days (Standard 14)",
         "does": "Ist/Soll je Tag + Gewichtsverlauf"},
        {"method": "GET", "path": "/api/v1/favorites", "does": "Favoriten (häufige Mahlzeiten)"},
        {"method": "POST", "path": "/api/v1/favorites", "body": "{meal_id} oder {title, meal_type, items, kcal, …}",
         "does": "Favorit anlegen/aktualisieren"},
        {"method": "POST", "path": "/api/v1/favorites/{id}/log", "body": {"eaten_at": "optional", "factor": "Menge, Standard 1"},
         "does": "Favorit als Mahlzeit eintragen"},
        *ENTITY_DOCS,
    ],
}


def _bad(msg: str) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text=json.dumps({"error": msg}, ensure_ascii=False), content_type="application/json")


def _day(value: str | None, default: date) -> str:
    if not value:
        return default.isoformat()
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        raise _bad(f"Ungültiges Datum: {value!r} (erwartet YYYY-MM-DD)")


def _int(request: web.Request, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(request.query.get(name, default)), hi))
    except ValueError:
        raise _bad(f"{name} muss eine Zahl sein")


def _avg(values) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _r(v, n=1):
    return None if v is None else round(v, n)


def _workout_row(w: dict) -> dict:
    out = {
        "workout_id": w["workout_id"], "day": w["day"], "title": w["title"],
        "type": TYPE_NAMES.get(w["type_id"], "Einheit"), "completed": bool(w["completed"]),
        "planned_h": _r(w["planned_h"], 2), "actual_h": _r(w["actual_h"], 2),
        "tss_planned": _r(w["tss_planned"]), "tss_actual": _r(w["tss_actual"]),
        "distance_km": _r((w["distance_m"] or 0) / 1000, 2) if w["distance_m"] else None,
        "np_w": _r(w["np_w"], 0), "avg_power_w": _r(w["avg_power_w"], 0), "avg_hr": _r(w["avg_hr"], 0),
        "if": _r(w["if_actual"], 2), "calories": _r(w["calories"], 0),
    }
    if w["avg_speed_ms"]:
        sp = w["avg_speed_ms"]
        out["avg_speed_kmh"] = round(sp * 3.6, 1)
        if w["type_id"] == 3 and sp > 0:  # Laufen: Pace min/km
            sec = 1000 / sp
            out["pace_min_km"] = f"{int(sec // 60)}:{int(sec % 60):02d}"
        if w["type_id"] == 1 and sp > 0:  # Schwimmen: Pace /100 m
            sec = 100 / sp
            out["pace_min_100m"] = f"{int(sec // 60)}:{int(sec % 60):02d}"
    return out


def _meal_totals(db: Database, day: str) -> dict:
    row = db.query(
        "SELECT COUNT(*) AS n, SUM(kcal) AS kcal, SUM(protein_g) AS protein_g, SUM(carbs_g) AS carbs_g, "
        "SUM(fat_g) AS fat_g, SUM(fiber_g) AS fiber_g FROM meals WHERE day = ?", (day,))[0]
    return {k: (_r(v, 0) if k != "n" else v) for k, v in row.items()}


def _clean_meal(body: dict, partial: bool) -> dict:
    data = {k: body[k] for k in MEAL_FIELDS if k in body}
    unknown = set(body) - set(MEAL_FIELDS)
    if unknown:
        raise _bad(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    if not partial and not str(data.get("title", "")).strip():
        raise _bad("title fehlt")
    for k in NUM_FIELDS & set(data):
        if data[k] is not None:
            try:
                data[k] = float(data[k])
            except (TypeError, ValueError):
                raise _bad(f"{k} muss eine Zahl sein")
            if data[k] < 0:
                raise _bad(f"{k} darf nicht negativ sein")
    for k, allowed in (("meal_type", MEAL_TYPES), ("accuracy", ACCURACY), ("rule_check", RULE_CHECK)):
        if data.get(k) is not None and data[k] not in allowed:
            raise _bad(f"{k} muss eines von {allowed} sein")
    if "eaten_at" in data or not partial:
        raw = data.get("eaten_at") or datetime.now().isoformat(timespec="minutes")
        try:
            ts = datetime.fromisoformat(str(raw))
        except ValueError:
            raise _bad(f"eaten_at ungültig: {raw!r}")
        data["eaten_at"] = ts.replace(tzinfo=None).isoformat(timespec="minutes")
        data["day"] = ts.date().isoformat()
    return data


def register_claude_api(app: web.Application, db: Database, sync: SyncService, today,
                        estimator: MealEstimator | None = None) -> None:
    async def index(_):
        return web.json_response({**API_INDEX, "version": __version__, "today": today().isoformat()})

    async def day_view(request: web.Request):
        ds = _day(request.query.get("date"), today())
        d = date.fromisoformat(ds)
        workouts = [_workout_row(w) for w in db.query(
            "SELECT * FROM tp_workouts WHERE day = ? ORDER BY workout_id", (ds,))]
        fit = db.query("SELECT day, ctl, atl, tsb, tss FROM tp_fitness WHERE day <= ? ORDER BY day DESC LIMIT 1", (ds,))
        mets = db.query("SELECT day, hrv, sleep_h, deep_h, rem_h, rhr, bb_min, bb_max, stress_avg, steps, weight_kg "
                        "FROM tp_metrics WHERE day <= ? ORDER BY day DESC LIMIT 8", (ds,))
        rec = mets[0] if mets and mets[0]["day"] == ds else None
        base = mets[1:] if rec else mets
        meals = db.query("SELECT id, eaten_at, meal_type, title, kcal, protein_g, carbs_g, fat_g, fiber_g, "
                         "accuracy, rule_check, warnings FROM meals WHERE day = ? ORDER BY eaten_at", (ds,))
        target = db.query("SELECT * FROM day_targets WHERE day = ?", (ds,))
        weight = db.query("SELECT day, weight_kg, 'eingetragen' AS source FROM body_weight WHERE day <= ? "
                          "UNION ALL SELECT day, weight_kg, 'Garmin/TP' FROM tp_metrics "
                          "WHERE day <= ? AND weight_kg IS NOT NULL ORDER BY day DESC LIMIT 1", (ds, ds))
        totals = _meal_totals(db, ds)
        tgt = target[0] if target else None
        balance = None
        if tgt and tgt.get("kcal") is not None:
            balance = {"kcal_rest": _r(tgt["kcal"] - (totals["kcal"] or 0), 0),
                       "protein_rest_g": _r((tgt["protein_g"] or 0) - (totals["protein_g"] or 0), 0),
                       "carbs_rest_g": _r((tgt["carbs_g"] or 0) - (totals["carbs_g"] or 0), 0),
                       "fat_rest_g": _r((tgt["fat_g"] or 0) - (totals["fat_g"] or 0), 0)}
        return web.json_response({
            "date": ds, "weekday": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][d.weekday()],
            "workouts": workouts, "fitness": fit[0] if fit else None,
            "recovery": rec,
            "recovery_avg_7d": {"hrv": _avg(m["hrv"] for m in base[:7]), "rhr": _avg(m["rhr"] for m in base[:7]),
                                "sleep_h": _avg(m["sleep_h"] for m in base[:7])},
            "meals": meals, "meal_totals": totals, "target": tgt, "remaining": balance,
            "latest_weight": weight[0] if weight else None,
            "sync": {"last_ok": sync.status.last_ok, "error": sync.status.last_error},
        })

    async def workouts(request: web.Request):
        end = _day(request.query.get("end"), today())
        start = _day(request.query.get("start"), date.fromisoformat(end) - timedelta(days=13))
        if (date.fromisoformat(end) - date.fromisoformat(start)).days > 180:
            raise _bad("Zeitraum max. 180 Tage")
        rows = [_workout_row(w) for w in db.query(
            "SELECT * FROM tp_workouts WHERE day BETWEEN ? AND ? ORDER BY day, workout_id", (start, end))]
        typ = request.query.get("type")
        if typ:
            rows = [r for r in rows if r["type"].lower() == typ.lower()]
        return web.json_response({"start": start, "end": end, "count": len(rows), "workouts": rows})

    async def workout_detail(request: web.Request):
        try:
            wid = int(request.match_info["workout_id"])
        except ValueError:
            raise _bad("workout_id muss eine Zahl sein")
        rows = db.query("SELECT * FROM tp_workouts WHERE workout_id = ?", (wid,))
        if not rows:
            raise web.HTTPNotFound(text=json.dumps({"error": "Einheit nicht gefunden"}), content_type="application/json")
        raw = json.loads(rows[0]["raw"])
        detail = {k: raw.get(k) for k in WORKOUT_DETAIL_KEYS if raw.get(k) not in (None, "", [])}
        if isinstance(detail.get("structure"), str):
            try:
                detail["structure"] = json.loads(detail["structure"])
            except ValueError:
                pass
        return web.json_response({"summary": _workout_row(rows[0]), "details": detail})

    async def weeks(request: web.Request):
        n = _int(request, "weeks", 8, 1, 52)
        t = today()
        monday = t - timedelta(days=t.weekday())
        start = monday - timedelta(weeks=n - 1)
        out = []
        for i in range(n):
            ws = start + timedelta(weeks=i)
            we = ws + timedelta(days=6)
            a, b = ws.isoformat(), we.isoformat()
            sports: dict[str, dict] = {}
            for w in db.query("SELECT type_id, completed, planned_h, actual_h, tss_planned, tss_actual "
                              "FROM tp_workouts WHERE day BETWEEN ? AND ?", (a, b)):
                s = sports.setdefault(TYPE_NAMES.get(w["type_id"], "Sonstiges"),
                                      {"n_done": 0, "h_plan": 0.0, "h_done": 0.0, "tss_plan": 0.0, "tss_done": 0.0})
                s["h_plan"] += w["planned_h"] or 0
                s["tss_plan"] += w["tss_planned"] or 0
                if w["completed"]:
                    s["n_done"] += 1
                    s["h_done"] += w["actual_h"] or 0
                    s["tss_done"] += w["tss_actual"] or 0
            for s in sports.values():
                for k in ("h_plan", "h_done", "tss_plan", "tss_done"):
                    s[k] = round(s[k], 1)
            fit = db.query("SELECT ctl, atl, tsb FROM tp_fitness WHERE day <= ? ORDER BY day DESC LIMIT 1", (b,))
            mets = db.query("SELECT hrv, rhr, sleep_h FROM tp_metrics WHERE day BETWEEN ? AND ?", (a, b))
            kcal = db.query("SELECT day, SUM(kcal) AS k FROM meals WHERE day BETWEEN ? AND ? GROUP BY day", (a, b))
            out.append({
                "week_start": a, "week_end": b, "sports": sports,
                "total_h_done": round(sum(s["h_done"] for s in sports.values()), 1),
                "total_tss_done": round(sum(s["tss_done"] for s in sports.values())),
                "ctl_end": _r(fit[0]["ctl"]) if fit else None, "tsb_end": _r(fit[0]["tsb"]) if fit else None,
                "hrv_avg": _avg(m["hrv"] for m in mets), "rhr_avg": _avg(m["rhr"] for m in mets),
                "sleep_avg_h": _avg(m["sleep_h"] for m in mets),
                "kcal_avg_logged_days": _avg(r["k"] for r in kcal), "days_with_meals": len(kcal),
            })
        return web.json_response({"weeks": out})

    async def recovery(request: web.Request):
        n = _int(request, "days", 28, 1, 365)
        start = (today() - timedelta(days=n - 1)).isoformat()
        rows = db.query("SELECT day, hrv, sleep_h, deep_h, rem_h, rhr, bb_min, bb_max, stress_avg, steps, weight_kg "
                        "FROM tp_metrics WHERE day >= ? ORDER BY day", (start,))
        return web.json_response({"days": rows})

    async def fitness(request: web.Request):
        n = _int(request, "days", 120, 1, 730)
        start = (today() - timedelta(days=n - 1)).isoformat()
        return web.json_response({"days": db.query(
            "SELECT day, ctl, atl, tsb, tss FROM tp_fitness WHERE day >= ? ORDER BY day", (start,))})

    async def meals_list(request: web.Request):
        ds = _day(request.query.get("date"), today())
        return web.json_response({"date": ds, "meals": db.query(
            "SELECT * FROM meals WHERE day = ? ORDER BY eaten_at", (ds,)), "totals": _meal_totals(db, ds)})

    async def _json(request: web.Request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise _bad("Body muss JSON sein")
        if not isinstance(body, dict):
            raise _bad("Body muss ein JSON-Objekt sein")
        return body

    async def meal_create(request: web.Request):
        data = _clean_meal(await _json(request), partial=False)
        data.setdefault("source", request.get("caller", "claude"))
        data["created_at"] = datetime.now().isoformat(timespec="seconds")
        cols = ", ".join(data)
        meal_id = db.execute(f"INSERT INTO meals ({cols}) VALUES ({', '.join('?' * len(data))})", list(data.values()))
        return web.json_response({"ok": True, "meal_id": meal_id, "day": data["day"],
                                  "day_totals": _meal_totals(db, data["day"])}, status=201)

    async def meal_update(request: web.Request):
        try:
            mid = int(request.match_info["meal_id"])
        except ValueError:
            raise _bad("meal_id muss eine Zahl sein")
        if not db.query("SELECT id FROM meals WHERE id = ?", (mid,)):
            raise web.HTTPNotFound(text=json.dumps({"error": "Mahlzeit nicht gefunden"}), content_type="application/json")
        data = _clean_meal(await _json(request), partial=True)
        if not data:
            raise _bad("Keine Felder zum Ändern")
        db.execute(f"UPDATE meals SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?", [*data.values(), mid])
        row = db.query("SELECT * FROM meals WHERE id = ?", (mid,))[0]
        return web.json_response({"ok": True, "meal": row, "day_totals": _meal_totals(db, row["day"])})

    async def weight(request: web.Request):
        body = await _json(request)
        ds = _day(body.get("day"), today())
        try:
            kg = float(body["weight_kg"])
        except (KeyError, TypeError, ValueError):
            raise _bad("weight_kg (Zahl) fehlt")
        if not 30 <= kg <= 250:
            raise _bad("weight_kg unplausibel")
        db.execute("INSERT INTO body_weight(day, weight_kg, note) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE "
                   "SET weight_kg=excluded.weight_kg, note=excluded.note", [ds, kg, body.get("note")])
        hist = db.query("SELECT day, weight_kg FROM body_weight ORDER BY day DESC LIMIT 10")
        return web.json_response({"ok": True, "day": ds, "weight_kg": kg, "recent": hist})

    async def target(request: web.Request):
        ds = _day(request.match_info["day"], today())
        body = await _json(request)
        allowed = {"day_type", "kcal", "protein_g", "carbs_g", "fat_g", "note"}
        unknown = set(body) - allowed
        if unknown:
            raise _bad(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
        vals = [ds] + [body.get(k) for k in ("day_type", "kcal", "protein_g", "carbs_g", "fat_g", "note")]
        db.execute("INSERT INTO day_targets(day, day_type, kcal, protein_g, carbs_g, fat_g, note) "
                   "VALUES(?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET day_type=excluded.day_type, "
                   "kcal=excluded.kcal, protein_g=excluded.protein_g, carbs_g=excluded.carbs_g, "
                   "fat_g=excluded.fat_g, note=excluded.note", vals)
        return web.json_response({"ok": True, "target": db.query("SELECT * FROM day_targets WHERE day=?", (ds,))[0]})

    async def sync_now(_):
        st = await sync.run_once()
        return web.json_response({"ok": st.last_error is None, "error": st.last_error, "counts": st.counts})

    # ── Etappe 3: Freitext-Schätzung, Favoriten, Löschen aus der Oberfläche ─────
    def _now() -> datetime:
        return datetime.now().replace(second=0, microsecond=0)

    async def meal_estimate(request: web.Request):
        body = await _json(request)
        if estimator is None:
            raise _bad("KI-Schätzung ist nicht eingerichtet")
        try:
            when = parse_when(body.get("eaten_at"), _now())
            proposal = await estimator.estimate(db, str(body.get("text", "")), when)
        except EstimateError as err:
            return web.json_response({"ok": False, "error": str(err)}, status=422)
        proposal["eaten_at"] = when.isoformat(timespec="minutes")
        proposal["source"] = "ki-task"
        if body.get("save") and not proposal.get("question"):
            data = _clean_meal({k: v for k, v in proposal.items() if k in MEAL_FIELDS}, partial=False)
            data["created_at"] = datetime.now().isoformat(timespec="seconds")
            mid = db.execute(f"INSERT INTO meals ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                             list(data.values()))
            return web.json_response({"ok": True, "saved": True, "meal_id": mid, "proposal": proposal,
                                      "day_totals": _meal_totals(db, data["day"])}, status=201)
        return web.json_response({"ok": True, "saved": False, "proposal": proposal})

    def _ui_only(request: web.Request) -> None:
        if request.get("caller", "ui") != "ui" and os.environ.get("COCKPIT_ALLOW_ALL") != "1":
            raise web.HTTPForbidden(text=json.dumps({"error": "Löschen nur in der Oberfläche"}),
                                    content_type="application/json")

    async def meal_delete(request: web.Request):
        _ui_only(request)
        mid = int(request.match_info["meal_id"])
        row = db.query("SELECT day FROM meals WHERE id = ?", (mid,))
        if not row:
            raise web.HTTPNotFound(text=json.dumps({"error": "Mahlzeit nicht gefunden"}), content_type="application/json")
        db.execute("DELETE FROM meals WHERE id = ?", (mid,))
        return web.json_response({"ok": True, "day_totals": _meal_totals(db, row[0]["day"])})

    FAV_FIELDS = ["title", "meal_type", "items", "kcal", "protein_g", "carbs_g", "fat_g", "fiber_g"]

    async def fav_list(_):
        return web.json_response({"favorites": db.query(
            "SELECT f.*, (SELECT COUNT(*) FROM meals m WHERE m.title = f.title) AS used "
            "FROM favorites f ORDER BY used DESC, title")})

    async def fav_create(request: web.Request):
        body = await _json(request)
        if body.get("meal_id"):
            src = db.query("SELECT * FROM meals WHERE id = ?", (body["meal_id"],))
            if not src:
                raise _bad("Mahlzeit nicht gefunden")
            body = {k: src[0][k] for k in FAV_FIELDS} | {k: v for k, v in body.items() if k in FAV_FIELDS}
        data = {k: body.get(k) for k in FAV_FIELDS}
        if not str(data.get("title") or "").strip():
            raise _bad("title fehlt")
        clean = _clean_meal({k: v for k, v in data.items() if v is not None}, partial=True)
        clean.pop("eaten_at", None), clean.pop("day", None)
        existing = db.query("SELECT id FROM favorites WHERE title = ?", (clean["title"],))
        if existing:
            db.execute(f"UPDATE favorites SET {', '.join(f'{k} = ?' for k in clean)} WHERE id = ?",
                       [*clean.values(), existing[0]["id"]])
            return web.json_response({"ok": True, "id": existing[0]["id"], "updated": True})
        fid = db.execute(f"INSERT INTO favorites ({', '.join(clean)}) VALUES ({', '.join('?' * len(clean))})",
                         list(clean.values()))
        return web.json_response({"ok": True, "id": fid}, status=201)

    async def fav_log(request: web.Request):
        fid = int(request.match_info["fav_id"])
        fav = db.query("SELECT * FROM favorites WHERE id = ?", (fid,))
        if not fav:
            raise web.HTTPNotFound(text=json.dumps({"error": "Favorit nicht gefunden"}), content_type="application/json")
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = {}
        body = body if isinstance(body, dict) else {}
        factor = float(body.get("factor", 1) or 1)
        if not 0.1 <= factor <= 5:
            raise _bad("factor zwischen 0,1 und 5")
        meal = {k: fav[0][k] for k in FAV_FIELDS if fav[0][k] is not None}
        for k in ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g"):
            if k in meal:
                meal[k] = round(meal[k] * factor, 1)
        if factor != 1:
            meal["items"] = f"{factor:g} × Favorit: " + (meal.get("items") or meal["title"])
        meal["eaten_at"] = body.get("eaten_at") or _now().isoformat(timespec="minutes")
        meal["accuracy"] = "Gut geschätzt"
        meal["source"] = "favorit"
        data = _clean_meal(meal, partial=False)
        data["created_at"] = datetime.now().isoformat(timespec="seconds")
        mid = db.execute(f"INSERT INTO meals ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                         list(data.values()))
        return web.json_response({"ok": True, "meal_id": mid, "day_totals": _meal_totals(db, data["day"])},
                                 status=201)

    async def fav_delete(request: web.Request):
        _ui_only(request)
        db.execute("DELETE FROM favorites WHERE id = ?", (int(request.match_info["fav_id"]),))
        return web.json_response({"ok": True})

    async def nutrition(request: web.Request):
        n = _int(request, "days", 14, 1, 120)
        start = (today() - timedelta(days=n - 1)).isoformat()
        ist = {r["day"]: r for r in db.query(
            "SELECT day, SUM(kcal) kcal, SUM(protein_g) protein_g, SUM(carbs_g) carbs_g, SUM(fat_g) fat_g "
            "FROM meals WHERE day >= ? GROUP BY day", (start,))}
        soll = {r["day"]: r for r in db.query("SELECT * FROM day_targets WHERE day >= ?", (start,))}
        out = []
        for i in range(n):
            d = (date.fromisoformat(start) + timedelta(days=i)).isoformat()
            a, s = ist.get(d) or {}, soll.get(d) or {}
            out.append({"day": d, "ist_kcal": _r(a.get("kcal"), 0), "ist_protein_g": _r(a.get("protein_g"), 0),
                        "ist_carbs_g": _r(a.get("carbs_g"), 0), "ist_fat_g": _r(a.get("fat_g"), 0),
                        "soll_kcal": s.get("kcal"), "soll_protein_g": s.get("protein_g"),
                        "soll_carbs_g": s.get("carbs_g"), "soll_fat_g": s.get("fat_g"), "day_type": s.get("day_type")})
        weights = db.query("SELECT day, weight_kg FROM body_weight ORDER BY day DESC LIMIT 30")
        return web.json_response({"days": out, "weights": list(reversed(weights)),
                                  "ki_available": bool(estimator and estimator.available)})

    r = app.router
    r.add_post("/api/v1/meals/estimate", meal_estimate)
    r.add_delete("/api/v1/meals/{meal_id:\\d+}", meal_delete)
    r.add_get("/api/v1/favorites", fav_list)
    r.add_post("/api/v1/favorites", fav_create)
    r.add_post("/api/v1/favorites/{fav_id:\\d+}/log", fav_log)
    r.add_delete("/api/v1/favorites/{fav_id:\\d+}", fav_delete)
    r.add_get("/api/v1/nutrition", nutrition)
    r.add_get("/api/v1", index)
    r.add_get("/api/v1/", index)
    r.add_get("/api/v1/day", day_view)
    r.add_get("/api/v1/workouts", workouts)
    r.add_get("/api/v1/workouts/{workout_id}", workout_detail)
    r.add_get("/api/v1/weeks", weeks)
    r.add_get("/api/v1/recovery", recovery)
    r.add_get("/api/v1/fitness", fitness)
    r.add_get("/api/v1/meals", meals_list)
    r.add_post("/api/v1/meals", meal_create)
    r.add_patch("/api/v1/meals/{meal_id}", meal_update)
    r.add_post("/api/v1/weight", weight)
    r.add_put("/api/v1/targets/{day}", target)
    r.add_post("/api/v1/sync", sync_now)
    register_catalog_api(app, db, _clean_meal)
