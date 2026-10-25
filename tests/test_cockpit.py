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


def test_sleep_by_type_and_units():
    out = parse_metric_day({"timeStamp": "2026-10-25T00:00:00", "details": [
        {"type": 6, "label": "Sleep Duration", "value": 604, "units": "min"},
        {"type": 60, "label": "HRV (rMSSD)", "value": 61}]})
    assert out["sleep_h"] == round(604 / 60, 2) and out["hrv"] == 61


def test_real_tp_shapes():
    # Formen wie im ersten echten Abgleich protokolliert
    out = parse_metric_day({"timeStamp": "2026-10-25T00:00:00", "details": [
        {"type": 6, "label": "Sleep Hours", "value": 5.1166},
        {"type": 64, "label": "Body Battery", "value": [17, 73, 36.37]},
        {"type": 62, "label": "Stress Level", "value": [None, 90, 27]},
        {"type": 9, "label": "Weight", "value": 86}]})
    assert (out["sleep_h"], out["bb_min"], out["bb_max"], out["stress_avg"], out["weight_kg"]) == \
        (5.1166, 17, 73, 27, 86)


def test_reparse_metrics(tmp_path):
    db = Database(tmp_path / "r.sqlite3")
    db.upsert_metrics([{"timeStamp": "2026-10-25T00:00:00",
                        "details": [{"type": 6, "label": "Irgendwas", "value": 7.5}]}])
    db._conn.execute("UPDATE tp_metrics SET sleep_h=NULL")
    assert db.reparse_metrics() == 1
    assert db.query("SELECT sleep_h FROM tp_metrics")[0]["sleep_h"] == 7.5


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


async def test_claude_api(tmp_path, tp_server):
    db, session, sync = await _service(tmp_path, tp_server)
    await sync.run_once()
    await session.close()
    app = build_app(db, sync, today=lambda: TODAY)
    os.environ["COCKPIT_ALLOW_ALL"] = "1"
    try:
        async with TestClient(TestServer(app)) as cl:
            idx = await (await cl.get("/api/v1")).json()
            assert any(e["path"] == "/api/v1/meals" for e in idx["endpoints"])
            w = await (await cl.get("/api/v1/workouts?start=2026-10-20&end=2026-10-31")).json()
            assert w["count"] == 2
            det = await (await cl.get("/api/v1/workouts/1")).json()
            assert det["details"]["normalizedPowerActual"] == 205
            assert (await cl.get("/api/v1/workouts/999")).status == 404
            # Mahlzeit eintragen, korrigieren, Tagessummen
            r = await cl.post("/api/v1/meals", json={"eaten_at": "2026-10-25T12:30", "title": "Gulasch-Rest + 2 Semmeln",
                                                     "meal_type": "Mittagessen", "kcal": 760, "protein_g": 38,
                                                     "carbs_g": 80, "fat_g": 28, "accuracy": "Gut geschätzt"})
            assert r.status == 201
            m = await r.json()
            assert m["day"] == "2026-10-25" and m["day_totals"]["kcal"] == 760
            p = await (await cl.patch(f"/api/v1/meals/{m['meal_id']}", json={"kcal": 800})).json()
            assert p["day_totals"]["kcal"] == 800
            assert (await cl.post("/api/v1/meals", json={"kcal": 1})).status == 400
            assert (await cl.post("/api/v1/meals", json={"title": "x", "meal_type": "Brunch"})).status == 400
            assert (await cl.post("/api/v1/meals", json={"title": "x", "bogus": 1})).status == 400
            # Gewicht + Tagesziel
            assert (await cl.post("/api/v1/weight", json={"day": "2026-10-25", "weight_kg": 85.6})).status == 200
            assert (await cl.post("/api/v1/weight", json={"weight_kg": 5})).status == 400
            t = await (await cl.put("/api/v1/targets/2026-10-25", json={"day_type": "Mittel", "kcal": 2900,
                                                                          "protein_g": 160, "carbs_g": 400, "fat_g": 80})).json()
            assert t["target"]["kcal"] == 2900
            day = await (await cl.get("/api/v1/day?date=2026-10-25")).json()
            assert day["remaining"]["kcal_rest"] == 2100
            assert day["latest_weight"]["weight_kg"] == 85.6
            assert day["recovery"]["hrv"] == 60 and day["recovery_avg_7d"]["hrv"] == 57
            wk = await (await cl.get("/api/v1/weeks?weeks=2")).json()
            assert wk["weeks"][-1]["sports"]["Rad"]["h_done"] == 3.5
            assert (await cl.get("/api/v1/day?date=gestern")).status == 400
    finally:
        del os.environ["COCKPIT_ALLOW_ALL"]


async def test_trusted_app_guard(tmp_path):
    from cockpit.web import TrustedApps
    db = Database(tmp_path / "g.sqlite3")
    t = TrustedApps(["localhost"])  # löst auf 127.0.0.1 auf
    app = build_app(db, SyncService(db, None, interval_min=30, history_days=14, plan_days_ahead=0),
                    today=lambda: TODAY, trusted=t)
    async with TestClient(TestServer(app, host="127.0.0.1")) as cl:
        assert (await cl.get("/api/v1")).status == 200
    t2 = TrustedApps(["does-not-exist.invalid"])
    app2 = build_app(db, SyncService(db, None, interval_min=30, history_days=14, plan_days_ahead=0),
                     today=lambda: TODAY, trusted=t2)
    async with TestClient(TestServer(app2, host="127.0.0.1")) as cl:
        assert (await cl.get("/api/v1")).status == 403
