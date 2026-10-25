"""Trainingsänderungen: erst Vorschlag, dann nach Freigabe in TrainingPeaks schreiben.

Ablauf: Claude (oder die Oberfläche) legt einen Vorschlag an → er erscheint im Cockpit
und Claude zeigt ihn im Chat → nach Markus' OK wird er übernommen (Claude ruft /apply
auf, oder Markus tippt in der Oberfläche auf „Übernehmen“). Nur geplante, noch nicht
absolvierte Einheiten dürfen geändert werden. Gelöscht wird nichts.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

from aiohttp import web

from .db import Database
from .sync import SyncService
from .tp import TPAuthError, TPError

log = logging.getLogger(__name__)

from .db import TRAINING_SCHEMA as SCHEMA  # noqa: E402  (Tabelle legt die DB-Migration an)

TYPE_IDS = {"schwimmen": 1, "rad": 2, "laufen": 3, "koppel": 4, "crosstraining": 5, "rennen": 6, "notiz": 7,
            "mtb": 8, "kraft": 9, "sonstiges": 10, "gehen": 13, "mobilität": 29}
# Erlaubte TP-Felder für Änderungen (deutsche Kurznamen → TP-Feld)
FIELDS = {
    "day": "workoutDay", "title": "title", "description": "description", "coach_comments": "coachComments",
    "planned_h": "totalTimePlanned", "tss_planned": "tssPlanned", "distance_planned_m": "distancePlanned",
    "if_planned": "ifPlanned", "structure": "structure", "type": "workoutTypeValueId",
    "order_on_day": "orderOnDay",
}
LABELS = {"workoutDay": "Tag", "title": "Titel", "description": "Beschreibung", "coachComments": "Coach-Hinweis",
          "totalTimePlanned": "Dauer (h)", "tssPlanned": "TSS geplant", "distancePlanned": "Distanz (m)",
          "ifPlanned": "IF geplant", "structure": "Intervall-Struktur", "workoutTypeValueId": "Sportart",
          "orderOnDay": "Reihenfolge"}

DOCS = [
    {"method": "POST", "path": "/api/v1/training/changes",
     "body": {"action": "move | update | create", "workout_id": "bei move/update (aus /workouts)",
              "changes": {k: v for k, v in {
                  "day": "YYYY-MM-DD", "title": "Text", "description": "Text", "coach_comments": "Text",
                  "planned_h": "Stunden dezimal", "tss_planned": "Zahl", "distance_planned_m": "Meter",
                  "if_planned": "0–1,5", "structure": "TP-Struktur (JSON) – für FORM-Brille jede Tätigkeit als eigenes Intervall",
                  "type": "bei create: Schwimmen|Rad|Laufen|Koppel|Kraft|Mobilität|Notiz …"}.items()},
              "reason": "kurze Begründung (wird angezeigt)"},
     "does": "Änderung VORSCHLAGEN – schreibt noch nichts in TrainingPeaks. Antwort enthält vorher/nachher."},
    {"method": "GET", "path": "/api/v1/training/changes", "params": "status=proposed|applied|rejected|failed|all",
     "does": "Vorschläge und erledigte Änderungen"},
    {"method": "POST", "path": "/api/v1/training/changes/{id}/apply", "body": {"confirm": True},
     "does": "Vorschlag in TrainingPeaks übernehmen – NUR nach ausdrücklichem OK von Markus"},
    {"method": "POST", "path": "/api/v1/training/changes/{id}/reject", "does": "Vorschlag verwerfen"},
]


def _bad(msg: str) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text=json.dumps({"error": msg}, ensure_ascii=False), content_type="application/json")


def _num(v, name, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise _bad(f"{name} muss eine Zahl sein")
    if not lo <= f <= hi:
        raise _bad(f"{name} außerhalb {lo}–{hi}")
    return f


def clean_changes(action: str, raw: dict, today: date) -> dict:
    if not isinstance(raw, dict) or not raw:
        raise _bad("changes fehlt")
    unknown = set(raw) - set(FIELDS)
    if unknown:
        raise _bad(f"Unbekannte Felder: {', '.join(sorted(unknown))} (erlaubt: {', '.join(FIELDS)})")
    out: dict = {}
    for k, v in raw.items():
        tp = FIELDS[k]
        if k == "day":
            try:
                d = date.fromisoformat(str(v)[:10])
            except ValueError:
                raise _bad("day muss YYYY-MM-DD sein")
            if d < today:
                raise _bad("Einheiten können nicht in die Vergangenheit verschoben werden")
            out[tp] = f"{d.isoformat()}T00:00:00"
        elif k in ("planned_h",):
            out[tp] = _num(v, k, 0, 12)
        elif k == "tss_planned":
            out[tp] = _num(v, k, 0, 600)
        elif k == "distance_planned_m":
            out[tp] = _num(v, k, 0, 300000)
        elif k == "if_planned":
            out[tp] = _num(v, k, 0, 1.5)
        elif k == "order_on_day":
            out[tp] = int(_num(v, k, 0, 20))
        elif k == "type":
            tid = TYPE_IDS.get(str(v).strip().lower()) if not isinstance(v, int) else v
            if tid is None:
                raise _bad(f"type unbekannt: {v} (erlaubt: {', '.join(TYPE_IDS)})")
            out[tp] = tid
        elif k == "structure":
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except ValueError:
                    raise _bad("structure ist kein gültiges JSON")
            if not isinstance(v, dict) or not isinstance(v.get("structure"), list):
                raise _bad("structure muss ein TP-Strukturobjekt mit Liste 'structure' sein")
            out[tp] = v
        else:
            out[tp] = None if v is None else str(v)[:4000]
    if action == "move" and set(out) - {"workoutDay", "orderOnDay"}:
        raise _bad("move ändert nur day (und ggf. order_on_day) – für mehr action=update verwenden")
    if action == "move" and "workoutDay" not in out:
        raise _bad("move braucht changes.day")
    if action == "create":
        for need in ("workoutDay", "title", "workoutTypeValueId"):
            if need not in out:
                raise _bad(f"create braucht {[k for k, t in FIELDS.items() if t == need][0]}")
    return out


def _short(v) -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, (dict, list)):
        return "Struktur"
    s = str(v)
    if len(s) >= 10 and s[4] == "-" and "T" in s:
        s = s[:10]
    return s if len(s) <= 80 else s[:77] + "…"


def describe(action: str, changes: dict, before: dict | None) -> tuple[str, list[dict]]:
    diff = []
    for tp, new in changes.items():
        old = (before or {}).get(tp)
        diff.append({"field": LABELS.get(tp, tp), "before": _short(old), "after": _short(new)})
    title = (before or {}).get("title") or changes.get("title") or "Einheit"
    if action == "move":
        summary = f"„{title}“ verschieben: {_short((before or {}).get('workoutDay'))} → {_short(changes['workoutDay'])}"
    elif action == "create":
        summary = f"Neue Einheit „{title}“ am {_short(changes['workoutDay'])}"
    else:
        summary = f"„{title}“ ändern: " + ", ".join(d["field"] for d in diff)
    return summary, diff


def register_training_api(app: web.Application, db: Database, sync: SyncService, today) -> None:
    db.executescript(SCHEMA)

    def row_out(r: dict) -> dict:
        r = dict(r)
        changes = json.loads(r["changes"])
        before = json.loads(r["before"]) if r.get("before") else None
        r["changes"] = changes
        r["before"] = None
        _, r["diff"] = describe(r["action"], changes, before)
        return r

    async def _json(request: web.Request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise _bad("Body muss JSON sein")
        if not isinstance(body, dict):
            raise _bad("Body muss ein JSON-Objekt sein")
        return body

    def _local_workout(wid: int) -> dict | None:
        rows = db.query("SELECT raw, completed FROM tp_workouts WHERE workout_id = ?", (wid,))
        if not rows:
            return None
        raw = json.loads(rows[0]["raw"])
        raw["_completed"] = bool(rows[0]["completed"])
        return raw

    async def propose(request: web.Request):
        body = await _json(request)
        action = str(body.get("action", "")).strip()
        if action not in ("move", "update", "create"):
            raise _bad("action muss move, update oder create sein")
        changes = clean_changes(action, body.get("changes") or {}, today())
        before = None
        wid = None
        if action != "create":
            try:
                wid = int(body.get("workout_id"))
            except (TypeError, ValueError):
                raise _bad("workout_id fehlt")
            before = _local_workout(wid)
            if before is None:
                raise web.HTTPNotFound(text=json.dumps({"error": "Einheit nicht gefunden (ggf. erst /sync)"}),
                                       content_type="application/json")
            if before["_completed"]:
                raise _bad("Absolvierte Einheiten werden nicht geändert")
            if str(before.get("workoutDay", ""))[:10] < today().isoformat():
                raise _bad("Einheiten in der Vergangenheit werden nicht geändert")
        summary, diff = describe(action, changes, before)
        cid = db.execute(
            "INSERT INTO training_changes(created_at, action, workout_id, changes, reason, summary, source) "
            "VALUES(?,?,?,?,?,?,?)",
            [datetime.now().isoformat(timespec="seconds"), action, wid, json.dumps(changes, ensure_ascii=False),
             str(body.get("reason") or "")[:1000], summary, request.get("caller", "claude")])
        return web.json_response({"ok": True, "change_id": cid, "status": "proposed", "summary": summary,
                                  "diff": diff, "reason": body.get("reason"),
                                  "next": "Markus fragen; nach OK POST /api/v1/training/changes/{id}/apply mit {\"confirm\": true}"},
                                 status=201)

    async def list_changes(request: web.Request):
        status = request.query.get("status", "proposed")
        if status == "all":
            rows = db.query("SELECT * FROM training_changes ORDER BY id DESC LIMIT 100")
        else:
            rows = db.query("SELECT * FROM training_changes WHERE status = ? ORDER BY id DESC LIMIT 100", (status,))
        return web.json_response({"changes": [row_out(r) for r in rows]})

    def _get(cid: int) -> dict:
        rows = db.query("SELECT * FROM training_changes WHERE id = ?", (cid,))
        if not rows:
            raise web.HTTPNotFound(text=json.dumps({"error": "Vorschlag nicht gefunden"}), content_type="application/json")
        return rows[0]

    async def apply(request: web.Request):
        cid = int(request.match_info["cid"])
        body = await _json(request)
        if body.get("confirm") is not True:
            raise _bad("Nur mit {\"confirm\": true} – erst nach ausdrücklichem OK von Markus")
        ch = _get(cid)
        if ch["status"] != "proposed":
            raise _bad(f"Vorschlag ist bereits {ch['status']}")
        changes = json.loads(ch["changes"])
        now = datetime.now().isoformat(timespec="seconds")
        who = request.get("caller", "claude")
        try:
            if ch["action"] == "create":
                result = await sync.client.create_workout(changes)
                before = None
                days = [changes["workoutDay"][:10]]
            else:
                live = await sync.client.workout(ch["workout_id"])
                if live.get("totalTime") or live.get("completed"):
                    raise TPError("Einheit wurde inzwischen absolviert – nicht geändert")
                before = {k: live.get(k) for k in changes} | {"title": live.get("title"),
                                                              "workoutDay": live.get("workoutDay")}
                result = await sync.client.update_workout(ch["workout_id"], changes)
                days = [str(live.get("workoutDay", ""))[:10], str(changes.get("workoutDay", live.get("workoutDay")))[:10]]
        except TPAuthError as err:
            raise web.HTTPServiceUnavailable(text=json.dumps({"error": f"TrainingPeaks-Anmeldung: {err}"},
                                                             ensure_ascii=False), content_type="application/json")
        except TPError as err:
            db.execute("UPDATE training_changes SET status='failed', decided_at=?, decided_by=?, error=? WHERE id=?",
                       [now, who, str(err)[:500], cid])
            return web.json_response({"ok": False, "status": "failed", "error": str(err)}, status=502)
        new_id = (result or {}).get("workoutId") if isinstance(result, dict) else None
        db.execute("UPDATE training_changes SET status='applied', decided_at=?, decided_by=?, before=?, "
                   "result_workout_id=? WHERE id=?",
                   [now, who, json.dumps(before, ensure_ascii=False) if before else None, new_id, cid])
        log.info("Trainingsänderung %s übernommen: %s", cid, ch["summary"])
        try:
            await sync.refresh_days([d for d in days if d])
        except Exception:  # Anzeige zieht beim nächsten Abgleich nach
            log.warning("Nachladen nach Änderung fehlgeschlagen", exc_info=True)
        return web.json_response({"ok": True, "status": "applied", "summary": ch["summary"],
                                  "workout_id": new_id or ch["workout_id"]})

    async def reject(request: web.Request):
        cid = int(request.match_info["cid"])
        ch = _get(cid)
        if ch["status"] != "proposed":
            raise _bad(f"Vorschlag ist bereits {ch['status']}")
        db.execute("UPDATE training_changes SET status='rejected', decided_at=?, decided_by=? WHERE id=?",
                   [datetime.now().isoformat(timespec="seconds"), request.get("caller", "claude"), cid])
        return web.json_response({"ok": True, "status": "rejected"})

    r = app.router
    r.add_post("/api/v1/training/changes", propose)
    r.add_get("/api/v1/training/changes", list_changes)
    r.add_post("/api/v1/training/changes/{cid:\\d+}/apply", apply)
    r.add_post("/api/v1/training/changes/{cid:\\d+}/reject", reject)
