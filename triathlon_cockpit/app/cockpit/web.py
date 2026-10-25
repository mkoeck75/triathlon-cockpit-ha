"""Web-Oberfläche + JSON-API, erreichbar nur über HA-Ingress."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import time
from datetime import date, timedelta
from pathlib import Path

from aiohttp import web

from . import __version__
from .api import TYPE_NAMES, register_claude_api
from .db import Database
from .sync import SyncService

STATIC = Path(__file__).parent / "static"
# Der HA-Supervisor leitet Ingress-Anfragen immer von dieser Adresse weiter.
INGRESS_PROXY = ipaddress.ip_address("172.30.32.2")


class TrustedApps:
    """Erlaubt neben dem Ingress-Proxy nur ausgewählte HA-Apps (z. B. den HA-MCP-Server).

    Apps sind im internen HA-Netz unter ihrem Slug als Hostnamen erreichbar
    (``81f33d0f_ha_mcp`` → ``81f33d0f-ha-mcp``). Wir lösen die Namen auf und
    vergleichen die Absenderadresse – ein Passwort ist dafür nicht nötig.
    """

    TTL = 300

    def __init__(self, slugs: list[str]):
        self.hosts = [s.strip().replace("_", "-") for s in slugs if s and s.strip()]
        self._ips: set = set()
        self._resolved_at = 0.0

    async def _refresh(self) -> None:
        loop = asyncio.get_running_loop()
        ips = set()
        for host in self.hosts:
            try:
                for info in await loop.getaddrinfo(host, None):
                    ips.add(ipaddress.ip_address(info[4][0]))
            except (OSError, ValueError):
                continue
        self._ips = ips
        self._resolved_at = time.monotonic()

    async def allows(self, ip) -> bool:
        if not self.hosts:
            return False
        if ip in self._ips:
            return True
        # Adresse unbekannt oder Liste alt → einmal neu auflösen (Apps bekommen nach Neustart neue IPs)
        if time.monotonic() - self._resolved_at > 5:
            await self._refresh()
        return ip in self._ips


def make_guard(trusted: TrustedApps):
    @web.middleware
    async def guard(request: web.Request, handler):
        if request.path == "/api/health" or os.environ.get("COCKPIT_ALLOW_ALL") == "1":
            return await handler(request)
        try:
            ip = ipaddress.ip_address(request.remote or "")
        except ValueError:
            raise web.HTTPForbidden(text="Nur über Home Assistant erreichbar.")
        if ip == INGRESS_PROXY:
            request["caller"] = "ui"
            return await handler(request)
        if await trusted.allows(ip):
            request["caller"] = "claude"
            return await handler(request)
        raise web.HTTPForbidden(text="Nur über Home Assistant erreichbar.")
    return guard


def _avg(values):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def build_app(db: Database, sync: SyncService, today=date.today,
              trusted: TrustedApps | None = None) -> web.Application:
    app = web.Application(middlewares=[make_guard(trusted or TrustedApps([]))])
    register_claude_api(app, db, sync, today)

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
