"""Tests mit einem nachgebauten TrainingPeaks-Server (keine echten Zugangsdaten nötig)."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "triathlon_cockpit" / "app"))

from cockpit.db import Database, parse_metric_day  # noqa: E402
from cockpit.sync import SyncService  # noqa: E402
from cockpit.tp import TPAuthError, TrainingPeaksClient  # noqa: E402
from cockpit.web import build_app  # noqa: E402

TODAY = date(2026, 10, 25)


def fake_tp(cookie_ok: bool = True) -> web.Application:
    app = web.Application()
    state = {"workout_calls": 0}

    async def token(req):
        if not cookie_ok or "Production_tpAuth=good" not in req.headers.get("Cookie", ""):
            return web.json_response({"success": False})
        return web.json_response({"success": True, "token": {"access_token": "T", "expires_in": 3600}})

    def authed(req):
        if req.headers.get("Authorization") != "Bearer T":
            raise web.HTTPUnauthorized()

    async def user(req):
        authed(req)
        return web.json_response({"user": {"userId": 42}})

    async def workouts(req):
        authed(req)
        state["workout_calls"] += 1
        start, end = req.match_info["start"], req.match_info["end"]
        out = []
        if start <= "2026-10-24" <= end:
            out.append({"workoutId": 1, "workoutDay": "2026-10-24T00:00:00", "title": "Lange Ausfahrt",
                        "workoutTypeValueId": 2, "totalTime": 3.5, "tssActual": 121.3,
                        "normalizedPowerActual": 205, "heartRateAverage": 128})
        if start <= "2026-10-26" <= end:
            out.append({"workoutId": 2, "workoutDay": "2026-10-26T00:00:00", "title": "Locker laufen",
                        "workoutTypeValueId": 3, "totalTimePlanned": 0.75, "tssPlanned": 40})
        return web.json_response(out)

    async def fitness(req):
        authed(req)
        body = await req.json()
        assert body["ctlConstant"] == 42
        return web.json_response([
            {"workoutDay": "2026-10-24T00:00:00", "ctl": 51.4, "atl": 68.1, "tsb": -9.5, "tssActual": 121.3},
            {"workoutDay": "2026-10-25T00:00:00", "ctl": 51.8, "atl": 68.2, "tsb": -16.7, "tssActual": 69.1},
        ] if req.match_info["start"] <= "2026-10-25" <= req.match_info["end"] else [])

    async def metrics(req):
        authed(req)
        if not (req.match_info["start"] <= "2026-10-25" <= req.match_info["end"]):
            return web.json_response([])
        return web.json_response([
            {"id": "a", "athleteId": 42, "timeStamp": "2026-10-24T00:00:00", "details": [
                {"label": "HRV", "value": 57}, {"label": "Pulse", "value": 46}, {"label": "Sleep", "value": 8.25}]},
            {"id": "b", "athleteId": 42, "timeStamp": "2026-10-25T00:00:00", "details": [
                {"label": "HRV", "value": 60}, {"label": "Pulse", "value": 45}, {"label": "Sleep", "value": 10.07},
                {"label": "Body Battery", "value": "28,92,65.9"}, {"label": "Stress Level", "value": ",75,19"}]},
        ])

    app.router.add_get("/users/v3/token", token)
    app.router.add_get("/users/v3/user", user)
    app.router.add_get("/fitness/v6/athletes/42/workouts/{start}/{end}", workouts)
    app.router.add_post("/fitness/v1/athletes/42/reporting/performancedata/{start}/{end}", fitness)
    app.router.add_get("/metrics/v3/athletes/42/consolidatedtimedmetrics/{start}/{end}", metrics)
    app["state"] = state
    return app


@pytest.fixture
async def tp_server():
    server = TestServer(fake_tp())
    await server.start_server()
    yield server
    await server.close()


async def _service(tmp_path, server, cookie="good"):
    db = Database(tmp_path / "c.sqlite3")
    session = aiohttp.ClientSession()
    client = TrainingPeaksClient(cookie, session, base_url=str(server.make_url("")))
    sync = SyncService(db, client, interval_min=30, history_days=60, plan_days_ahead=7, today=lambda: TODAY)
    return db, session, sync


async def test_full_sync(tmp_path, tp_server):
    db, session, sync = await _service(tmp_path, tp_server)
    st = await sync.run_once()
    await session.close()
    assert st.last_error is None, st.last_error
    assert st.counts["workouts"] == 2
    w = db.query("SELECT * FROM tp_workouts ORDER BY day")
    assert [x["title"] for x in w] == ["Lange Ausfahrt", "Locker laufen"]
    assert w[0]["completed"] == 1 and w[1]["completed"] == 0
    f = db.query("SELECT * FROM tp_fitness WHERE day='2026-10-25'")[0]
    assert f["tsb"] == -16.7
    m = db.query("SELECT * FROM tp_metrics WHERE day='2026-10-25'")[0]
    assert (m["hrv"], m["rhr"], m["bb_max"], m["stress_avg"]) == (60, 45, 92, 19)


async def test_resync_is_idempotent(tmp_path, tp_server):
    db, session, sync = await _service(tmp_path, tp_server)
    await sync.run_once()
    await sync.run_once()
    await session.close()
    assert db.query("SELECT COUNT(*) AS n FROM tp_workouts")[0]["n"] == 2
    assert db.query("SELECT COUNT(*) AS n FROM tp_fitness")[0]["n"] == 2


async def test_bad_cookie_reports_auth_problem(tmp_path, tp_server):
    db, session, sync = await _service(tmp_path, tp_server, cookie="expired")
    st = await sync.run_once()
    await session.close()
    assert st.auth_problem is True
    assert db.query("SELECT ok FROM sync_log")[0]["ok"] == 0


async def test_client_raises_auth_error(tp_server):
    async with aiohttp.ClientSession() as s:
        c = TrainingPeaksClient("", s, base_url=str(tp_server.make_url("")))
        with pytest.raises(TPAuthError):
            await c.athlete_id()


def test_parse_metric_day_tolerates_missing():
    out = parse_metric_day({"timeStamp": "2026-10-01T00:00:00", "details": [{"label": "HRV", "value": ""}]})
    assert out["day"] == "2026-10-01" and out["hrv"] is None


async def test_web_api_and_ingress_guard(tmp_path, tp_server):
    db, session, sync = await _service(tmp_path, tp_server)
    await sync.run_once()
    await session.close()
    app = build_app(db, sync, today=lambda: TODAY)
    async with TestClient(TestServer(app)) as cl:
        # Von außerhalb des Ingress-Proxys: gesperrt, nur Health offen
        assert (await cl.get("/api/health")).status == 200
        assert (await cl.get("/api/today")).status == 403
        os.environ["COCKPIT_ALLOW_ALL"] = "1"
        try:
            t = await (await cl.get("/api/today")).json()
            assert t["fitness"]["ctl"] == 51.8
            assert t["workouts"][0]["title"] == "Locker laufen"
            assert t["recovery"]["hrv"] == 60 and t["baseline"]["hrv_7"] == 57
            rows = (await (await cl.get("/api/fitness?days=30")).json())["rows"]
            assert len(rows) == 2
            assert (await cl.get("/")).status == 200
        finally:
            del os.environ["COCKPIT_ALLOW_ALL"]
