"""Web-Oberfläche + JSON-API, erreichbar nur über HA-Ingress."""

from __future__ import annotations

import ipaddress
import os
from datetime import date, timedelta
from pathlib import Path

from aiohttp import web

from . import __version__
from .db import Database
from .sync import SyncService

STATIC = Path(__file__).parent / "static"
# Der HA-Supervisor leitet Ingress-Anfragen immer von dieser Adresse weiter.
INGRESS_PROXY = ipaddress.ip_address("172.30.32.2")
TYPE_NAMES = {1: "Schwimmen", 2: "Rad", 3: "Laufen", 4: "Koppel", 5: "Crosstraining", 6: "Rennen",
              7: "Notiz", 8: "MTB", 9: "Kraft", 10: "Sonstiges", 11: "Langlauf", 12: "Rudern",
              13: "Gehen", 29: "Kraft/Mobilität", 100: "Sonstiges"}


@web.middleware
async def only_ingress(request: web.Request, handler):
    if request.path == "/api/health" or os.environ.get("COCKPIT_ALLOW_ALL") == "1":
        return await handler(request)
    peer = request.remote or ""
    try:
        if ipaddress.ip_address(peer) == INGRESS_PROXY:
            return await handler(request)
    except ValueError:
        pass
    raise web.HTTPForbidden(text="Nur über Home Assistant erreichbar.")


def _avg(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def build_app(db: Database, sync: SyncService, today=date.today) -> web.Application:
    app = web.Application(middlewares=[only_ingress])

    async def health(_):
        return web.json_response({"ok": True, "version": __version__})

    async def status(_):
        st = sync.status
        return web.json_response({
            "version": __version__,
            "running": st.running,
            "last_ok": st.last_ok,
            "last_attempt": st.last_attempt,
            "last_error": st.last_error,
            "auth_problem": st.auth_problem,
            "counts": st.counts,
        })

    async def trigger_sync(_):
        st = await sync.run_once()
        return web.json_response({"ok": st.last_error is None, "error": st.last_error})

    async def today_view(_):
        d = today()
        ds = d.isoformat()
        workouts = db.query(
            "SELECT workout_id, day, title, type_id, completed, planned_h, actual_h, tss_planned, "
            "tss_actual, distance_m, np_w, avg_hr FROM tp_workouts WHERE day BETWEEN ? AND ? "
            "ORDER BY day, workout_id",
            (ds, (d + timedelta(days=6)).isoformat()),
        )
        for w in workouts:
            w["type"] = TYPE_NAMES.get(w["type_id"], "Einheit")
        fit = db.query("SELECT * FROM tp_fitness WHERE day <= ? ORDER BY day DESC LIMIT 1", (ds,))
        mets = db.query("SELECT day, hrv, sleep_h, rhr, bb_max, stress_avg FROM tp_metrics "
                        "WHERE day <= ? ORDER BY day DESC LIMIT 28", (ds,))
        latest = mets[0] if mets else None
        baseline = {
            "hrv_7": _avg(m["hrv"] for m in mets[1:8]),
            "hrv_28": _avg(m["hrv"] for m in mets[1:]),
            "rhr_7": _avg(m["rhr"] for m in mets[1:8]),
            "sleep_7": _avg(m["sleep_h"] for m in mets[1:8]),
        }
        return web.json_response({
            "today": ds,
            "workouts": workouts,
            "fitness": fit[0] if fit else None,
            "recovery": latest,
            "baseline": baseline,
        })

    async def fitness_series(request: web.Request):
        days = max(7, min(int(request.query.get("days", 120)), 730))
        start = (today() - timedelta(days=days)).isoformat()
        rows = db.query(
            "SELECT f.day, f.ctl, f.atl, f.tsb, f.tss, m.hrv, m.rhr, m.sleep_h FROM tp_fitness f "
            "LEFT JOIN tp_metrics m ON m.day = f.day WHERE f.day >= ? ORDER BY f.day",
            (start,),
        )
        return web.json_response({"rows": rows})

    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    app.router.add_get("/", index)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/sync", trigger_sync)
    app.router.add_get("/api/today", today_view)
    app.router.add_get("/api/fitness", fitness_series)
    return app
