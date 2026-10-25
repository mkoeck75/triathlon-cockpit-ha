"""Schlanker TrainingPeaks-Client.

TrainingPeaks bietet keine API für Privatnutzer. Wir nutzen deshalb dieselbe
Web-API wie die TP-Web-App: Das Login-Cookie (Production_tpAuth) wird gegen ein
kurzlebiges Bearer-Token getauscht. Läuft das Cookie ab, meldet der Client
``TPAuthError`` – das Add-on zeigt das an, statt endlos neu zu versuchen.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

BASE_URL = "https://tpapi.trainingpeaks.com"
TIMEOUT = aiohttp.ClientTimeout(total=30)


class TPError(Exception):
    """Allgemeiner TrainingPeaks-Fehler."""


class TPAuthError(TPError):
    """Cookie fehlt, ist abgelaufen oder wurde abgelehnt."""


@dataclass
class _Token:
    value: str
    expires_at: float


class TrainingPeaksClient:
    def __init__(self, auth_cookie: str, session: aiohttp.ClientSession, base_url: str = BASE_URL):
        self._cookie = (auth_cookie or "").strip()
        self._session = session
        self._base = base_url.rstrip("/")
        self._token: _Token | None = None
        self._athlete_id: int | None = None

    # ── Anmeldung ────────────────────────────────────────────────────────────
    async def _get_token(self) -> str:
        if self._token and time.time() < self._token.expires_at - 60:
            return self._token.value
        if not self._cookie:
            raise TPAuthError("Kein TrainingPeaks-Cookie konfiguriert.")
        async with self._session.get(
            f"{self._base}/users/v3/token",
            headers={"Cookie": f"Production_tpAuth={self._cookie}"},
            timeout=TIMEOUT,
        ) as resp:
            if resp.status in (401, 403):
                raise TPAuthError("TrainingPeaks hat das Cookie abgelehnt (abgelaufen?).")
            if resp.status >= 400:
                raise TPError(f"Token-Abruf fehlgeschlagen: HTTP {resp.status}")
            data = await resp.json(content_type=None)
        if not data or not data.get("success"):
            raise TPAuthError("Cookie konnte nicht gegen ein Token getauscht werden (abgelaufen?).")
        tok = data["token"]
        self._token = _Token(tok["access_token"], time.time() + float(tok.get("expires_in", 3600)))
        return self._token.value

    async def _request(self, method: str, path: str, json_body: Any = None) -> Any:
        for attempt in (1, 2):
            token = await self._get_token()
            async with self._session.request(
                method,
                f"{self._base}{path}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=json_body,
                timeout=TIMEOUT,
            ) as resp:
                if resp.status == 401 and attempt == 1:
                    self._token = None  # Token verworfen → einmal neu holen
                    continue
                if resp.status in (401, 403):
                    raise TPAuthError("TrainingPeaks-Sitzung ungültig (401/403).")
                if resp.status >= 400:
                    text = (await resp.text())[:300]
                    raise TPError(f"HTTP {resp.status} bei {method} {path}: {text}")
                if resp.status == 204:
                    return None
                return await resp.json(content_type=None)
        raise TPError("unerreichbar")

    async def athlete_id(self) -> int:
        if self._athlete_id is None:
            data = await self._request("GET", "/users/v3/user")
            self._athlete_id = int(data["user"]["userId"])
        return self._athlete_id

    # ── Lesen ────────────────────────────────────────────────────────────────
    async def workouts(self, start: str, end: str) -> list[dict]:
        aid = await self.athlete_id()
        return await self._request("GET", f"/fitness/v6/athletes/{aid}/workouts/{start}/{end}") or []

    async def fitness(self, start: str, end: str) -> list[dict]:
        """Tageswerte CTL/ATL/TSB/TSS (Standard-Konstanten 42/7 wie in TP)."""
        aid = await self.athlete_id()
        body = {"atlConstant": 7, "atlStart": 0, "ctlConstant": 42, "ctlStart": 0, "workoutTypes": []}
        return await self._request(
            "POST", f"/fitness/v1/athletes/{aid}/reporting/performancedata/{start}/{end}", body
        ) or []

    async def workout(self, workout_id: int) -> dict:
        aid = await self.athlete_id()
        return await self._request("GET", f"/fitness/v6/athletes/{aid}/workouts/{int(workout_id)}")

    # ── Schreiben (nur geplante Einheiten; Löschen ist bewusst nicht vorgesehen) ──
    async def update_workout(self, workout_id: int, changes: dict) -> dict:
        """TP verlangt beim PUT das vollständige Objekt: holen, zusammenführen, zurückschreiben."""
        aid = await self.athlete_id()
        existing = await self.workout(workout_id)
        merged = {**existing, **changes, "athleteId": aid}
        if merged.get("structure") is not None and not isinstance(merged["structure"], str):
            merged["structure"] = json.dumps(merged["structure"])
        return await self._request("PUT", f"/fitness/v6/athletes/{aid}/workouts/{int(workout_id)}", merged)

    async def create_workout(self, data: dict) -> dict:
        aid = await self.athlete_id()
        structure = data.get("structure")
        if structure is not None and not isinstance(structure, str):
            structure = json.dumps(structure)
        payload = {
            "athleteId": aid, "workoutId": 0, "workoutDay": data["workoutDay"],
            "workoutTypeValueId": data["workoutTypeValueId"], "workoutSubTypeId": data.get("workoutSubTypeId"),
            "title": data["title"], "description": data.get("description"),
            "coachComments": data.get("coachComments"), "athleteComments": None,
            "totalTimePlanned": data.get("totalTimePlanned"), "distancePlanned": data.get("distancePlanned"),
            "tssPlanned": data.get("tssPlanned"), "ifPlanned": data.get("ifPlanned"), "energyPlanned": None,
            "feeling": None, "rpe": None, "orderOnDay": data.get("orderOnDay"), "structure": structure,
            "userTags": data.get("userTags") or "", "workoutComments": [],
        }
        return await self._request("POST", f"/fitness/v6/athletes/{aid}/workouts", payload)

    async def metrics(self, start: str, end: str) -> list[dict]:
        """Gesundheitswerte (u. a. von Garmin synchronisiert): HRV, Schlaf, Ruhepuls …"""
        aid = await self.athlete_id()
        return await self._request(
            "GET", f"/metrics/v3/athletes/{aid}/consolidatedtimedmetrics/{start}/{end}"
        ) or []
