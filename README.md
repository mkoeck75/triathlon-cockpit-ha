# Triathlon-Cockpit für Home Assistant

Home-Assistant-Add-on, das Training (TrainingPeaks), Erholung und Ernährung in einem
Cockpit bündelt – mit eigener Datenbank und (ab Etappe 2) einer Schnittstelle für Claude.

## Installation

1. HA → **Einstellungen → Add-ons → Add-on-Store** → Menü (⋮) → **Repositories**.
2. `https://github.com/mkoeck75/triathlon-cockpit-ha` hinzufügen.
   Da das Repo privat ist, braucht HA dafür Zugriff – siehe unten.
3. **Triathlon-Cockpit** installieren, Konfiguration ausfüllen (siehe `triathlon_cockpit/DOCS.md`), starten.

## Fahrplan

1. **0.1** Grundgerüst: TP lesen, Datenbank, Oberfläche ← *jetzt*
2. **0.2** Claude-Connector (MCP) + Fernzugang
3. **0.3** Ernährung: Freitext/Favoriten, Makros über den HA-KI-Task
4. **0.4** Umzug aus Notion, MQTT-Sensoren, Trainingsänderungen schreiben

## Entwicklung

```bash
pip install aiohttp pytest pytest-aiohttp
pytest
cd triathlon_cockpit/app && COCKPIT_DATA_DIR=/tmp/cockpit COCKPIT_ALLOW_ALL=1 python -m cockpit.main
```
