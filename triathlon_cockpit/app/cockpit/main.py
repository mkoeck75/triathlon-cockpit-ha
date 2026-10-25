"""Startpunkt des Add-ons."""

from __future__ import annotations

import asyncio
import logging
import signal

import aiohttp
from aiohttp import web

from . import __version__
from .config import load_config
from .db import Database
from .sync import SyncService
from .tp import TrainingPeaksClient
from .web import build_app


async def _run() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("cockpit")
    log.info("Triathlon-Cockpit %s startet", __version__)

    db = Database(cfg.db_path)
    log.info("Gesundheitsdaten neu ausgewertet: %s Tage", db.reparse_metrics())
    session = aiohttp.ClientSession(headers={"User-Agent": f"triathlon-cockpit-ha/{__version__}"})
    client = TrainingPeaksClient(cfg.tp_auth_cookie, session)
    sync = SyncService(db, client, interval_min=cfg.sync_interval_min,
                       history_days=cfg.history_days, plan_days_ahead=cfg.plan_days_ahead)

    runner = web.AppRunner(build_app(db, sync), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
    log.info("Oberfläche läuft auf Port %s", cfg.port)

    if cfg.tp_auth_cookie:
        sync.start()
    else:
        sync.status.auth_problem = True
        sync.status.last_error = "Kein TrainingPeaks-Cookie eingetragen (Add-on → Konfiguration)."
        log.warning(sync.status.last_error)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("Beende …")
    await sync.stop()
    await runner.cleanup()
    await session.close()
    db.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
