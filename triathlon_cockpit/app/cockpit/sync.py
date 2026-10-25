"""Regelmäßiger Abgleich mit TrainingPeaks (Etappe 1: nur lesen)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .db import Database
from .tp import TPAuthError, TPError, TrainingPeaksClient

log = logging.getLogger(__name__)

# TP erlaubt nur begrenzte Zeiträume je Abfrage – wir holen in Blöcken.
CHUNK_DAYS = 45


@dataclass
class SyncStatus:
    running: bool = False
    last_ok: str | None = None
    last_attempt: str | None = None
    last_error: str | None = None
    auth_problem: bool = False
    counts: dict = field(default_factory=dict)
    first_full_sync_done: bool = False


def _chunks(start: date, end: date, size: int = CHUNK_DAYS):
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=size - 1), end)
        yield cur.isoformat(), stop.isoformat()
        cur = stop + timedelta(days=1)


class SyncService:
    def __init__(self, db: Database, client: TrainingPeaksClient, *, interval_min: int,
                 history_days: int, plan_days_ahead: int, today=date.today):
        self.db = db
        self.client = client
        self.interval = max(15, interval_min) * 60
        self.history_days = history_days
        self.plan_days_ahead = plan_days_ahead
        self.status = SyncStatus()
        self._today = today
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def run_once(self) -> SyncStatus:
        async with self._lock:
            st = self.status
            st.running = True
            st.last_attempt = datetime.now(timezone.utc).isoformat(timespec="seconds")
            sync_id = self.db.log_sync_start()
            today = self._today()
            # Erster Lauf: volle Historie; danach nur die letzten 14 Tage nachziehen
            back = self.history_days if not st.first_full_sync_done else 14
            start = today - timedelta(days=back)
            plan_end = today + timedelta(days=self.plan_days_ahead)
            counts = {"workouts": 0, "fitness": 0, "metrics": 0, "deleted": 0}
            try:
                for a, b in _chunks(start, plan_end):
                    ws = await self.client.workouts(a, b)
                    counts["workouts"] += self.db.upsert_workouts(ws)
                    keep = {int(w["workoutId"]) for w in ws if w.get("workoutId") is not None}
                    counts["deleted"] += self.db.delete_workouts_missing(a, b, keep)
                for a, b in _chunks(start, today):
                    counts["fitness"] += self.db.upsert_fitness(await self.client.fitness(a, b))
                    counts["metrics"] += self.db.upsert_metrics(await self.client.metrics(a, b))
                st.last_ok = datetime.now(timezone.utc).isoformat(timespec="seconds")
                st.last_error = None
                st.auth_problem = False
                st.counts = counts
                st.first_full_sync_done = True
                self.db.log_sync_end(sync_id, True, str(counts))
                log.info("Sync ok: %s", counts)
            except TPAuthError as err:
                st.auth_problem = True
                st.last_error = str(err)
                self.db.log_sync_end(sync_id, False, f"auth: {err}")
                log.warning("Sync: Anmeldung fehlgeschlagen: %s", err)
            except (TPError, OSError, asyncio.TimeoutError) as err:
                st.last_error = str(err) or err.__class__.__name__
                self.db.log_sync_end(sync_id, False, st.last_error)
                log.warning("Sync fehlgeschlagen: %s", st.last_error)
            except Exception as err:  # niemals den Dienst abstürzen lassen
                st.last_error = f"Unerwarteter Fehler: {err!r}"
                self.db.log_sync_end(sync_id, False, st.last_error)
                log.exception("Sync: unerwarteter Fehler")
            finally:
                st.running = False
            return st

    async def _loop(self) -> None:
        while True:
            await self.run_once()
            # Bei abgelaufenem Cookie nicht im Minutentakt bei TP anklopfen.
            wait = self.interval * (4 if self.status.auth_problem else 1)
            await asyncio.sleep(wait)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="tp-sync")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
