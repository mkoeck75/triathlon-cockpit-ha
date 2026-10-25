"""Makro-Schätzung für Freitext-Eingaben über den HA-KI-Task (ai_task.generate_data).

Die App ruft nur diesen einen HA-Dienst auf. Als Kontext bekommt das Modell
den Lebensmittel-Stamm, die Rezepte (Werte pro 100 g, Reste) und die heute
gültigen Ernährungsregeln – so rechnet es mit Markus' eigenen Produkten.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any

import aiohttp

from .db import Database

log = logging.getLogger(__name__)

MEAL_TYPES = ["Frühstück", "Mittagessen", "Abendessen", "Snack", "Training/Wettkampf", "Getränk", "Supplement"]
ACCURACY = ["Exakt (Stamm/Etikett)", "Gut geschätzt", "Grob geschätzt"]
RULE_CHECK = ["OK", "Hinweis", "Warnung"]
NUMBERS = ["kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "recipe_grams"]

STRUCTURE = {
    "title": {"selector": {"text": {}}, "required": True,
              "description": "Kurze Beschreibung der Mahlzeit ohne Uhrzeit, deutsch"},
    "meal_type": {"selector": {"select": {"options": MEAL_TYPES}}, "required": True,
                  "description": "Art der Mahlzeit (Uhrzeit beachten)"},
    "items": {"selector": {"text": {}}, "required": True,
              "description": "Zutaten mit Mengen, kurz (max. 200 Zeichen), getrennt mit ' · '"},
    "kcal": {"selector": {"number": {}}, "required": True, "description": "kcal gesamt"},
    "protein_g": {"selector": {"number": {}}, "required": True, "description": "Eiweiß in g"},
    "carbs_g": {"selector": {"number": {}}, "required": True, "description": "Kohlenhydrate in g"},
    "fat_g": {"selector": {"number": {}}, "required": True, "description": "Fett in g"},
    "fiber_g": {"selector": {"number": {}}, "required": True, "description": "Ballaststoffe in g"},
    "accuracy": {"selector": {"select": {"options": ACCURACY}}, "required": True,
                 "description": "Exakt nur bei Stamm/Etikett, sonst Gut oder Grob geschätzt"},
    "rule_check": {"selector": {"select": {"options": RULE_CHECK}}, "required": True,
                   "description": "Ergebnis der Prüfung gegen die gültigen Regeln"},
    "warnings": {"selector": {"text": {}}, "required": False,
                 "description": "Nur bei Regelverstoß: ein Satz mit Alternative, sonst leer"},
    "recipe_name": {"selector": {"text": {}}, "required": False,
                    "description": "Nur wenn ein vorhandenes Rezept verwendet wurde: genauer Rezeptname"},
    "recipe_grams": {"selector": {"number": {}}, "required": False,
                     "description": "Gegessene Gramm vom Rezept, falls Rezept verwendet"},
    "question": {"selector": {"text": {}}, "required": False,
                 "description": "Eine kurze Rückfrage, falls eine Menge sehr unklar ist, sonst leer"},
}


class EstimateError(Exception):
    pass


def _r(v, n=1):
    return None if v is None else round(v, n)


def build_context(db: Database, on: str) -> str:
    foods = db.query("SELECT name, brand, kcal_100, protein_100, carbs_100, fat_100, fiber_100, unit_g "
                     "FROM foods ORDER BY name")
    recipes = db.query("SELECT name, cooked_on, total_weight_g, kcal, protein_g, carbs_g, fat_g, fiber_g, "
                       "leftover_g FROM recipes ORDER BY cooked_on DESC LIMIT 15")
    rules = db.query("SELECT rule, details, severity FROM nutrition_rules WHERE active = 1 "
                     "AND (valid_from IS NULL OR valid_from = '' OR valid_from <= ?) "
                     "AND (valid_to IS NULL OR valid_to = '' OR valid_to >= ?)", (on, on))
    lines = ["LEBENSMITTEL-STAMM (Werte pro 100 g/ml; Stück = Stückgewicht):"]
    for f in foods:
        lines.append(f"- {f['name']} ({f['brand'] or ''}): {f['kcal_100']} kcal, E {f['protein_100']}, "
                     f"KH {f['carbs_100']}, F {f['fat_100']}, BS {f['fiber_100']}"
                     + (f", Stück {f['unit_g']} g" if f["unit_g"] else ""))
    lines.append("REZEPTE (fertiges Gericht; Werte pro 100 g):")
    for r in recipes:
        w = r["total_weight_g"]
        if not w:
            continue
        per = lambda k: _r((r[k] or 0) / w * 100)  # noqa: E731
        lines.append(f"- {r['name']} (gekocht {r['cooked_on']}, gesamt {w:.0f} g, Rest {r['leftover_g'] or 0:.0f} g): "
                     f"{per('kcal')} kcal, E {per('protein_g')}, KH {per('carbs_g')}, F {per('fat_g')}, BS {per('fiber_g')}")
    lines.append("HEUTE GÜLTIGE ERNÄHRUNGSREGELN:")
    lines += [f"- [{r['severity']}] {r['rule']}: {r['details']}" for r in rules] or ["- keine"]
    return "\n".join(lines)


WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def build_instructions(text: str, when: datetime, context: str) -> str:
    return (
        "Schätze die Nährwerte dieser Mahlzeit eines Triathleten in Österreich realistisch "
        "(österreichische Portionsgrößen). Nutze zuerst die Werte unten (Stamm, Rezepte: pro 100 g × Gramm), "
        "sonst Standardwerte (BLS/USDA). Fehlende Mengen plausibel annehmen. Prüfe gegen die Regeln. "
        "Antworte auf Deutsch und KURZ: items max. 200 Zeichen ohne Einzelwerte, warnings max. ein Satz.\n\n"
        f"Zeitpunkt: {WEEKDAYS[when.weekday()]} {when.strftime('%d.%m.%Y %H:%M')}\n"
        f"Mahlzeit: {text}\n\n{context}"
    )


def clean_result(data: dict, db: Database) -> dict:
    if not isinstance(data, dict) or not data.get("title"):
        raise EstimateError("Der KI-Task hat kein verwertbares Ergebnis geliefert.")
    out: dict[str, Any] = {}
    for k in ("title", "items", "warnings", "question"):
        if data.get(k):
            out[k] = str(data[k]).strip()[:2000]
    for k in NUMBERS:
        try:
            v = float(data[k]) if data.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            v = None
        if v is not None and (v < 0 or v > 20000):
            v = None
        if v is not None:
            out[k] = round(v, 1)
    for k, allowed, default in (("meal_type", MEAL_TYPES, "Snack"), ("accuracy", ACCURACY, "Grob geschätzt"),
                                ("rule_check", RULE_CHECK, "OK")):
        out[k] = data.get(k) if data.get(k) in allowed else default
    if "kcal" not in out:
        raise EstimateError("Keine Kalorien geschätzt – bitte genauer beschreiben.")
    name = (data.get("recipe_name") or "").strip()
    if name:
        rec = db.query("SELECT id FROM recipes WHERE name = ? ORDER BY cooked_on DESC LIMIT 1", (name,))
        if rec:
            out["recipe_id"] = rec[0]["id"]
        else:
            out.pop("recipe_grams", None)
    else:
        out.pop("recipe_grams", None)
    return out


class MealEstimator:
    def __init__(self, session: aiohttp.ClientSession, entity_id: str,
                 base_url: str = "http://supervisor/core/api", token: str | None = None):
        self.session = session
        self.entity_id = entity_id
        self.base = base_url.rstrip("/")
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN", "")

    @property
    def available(self) -> bool:
        return bool(self.token and self.entity_id)

    async def estimate(self, db: Database, text: str, when: datetime) -> dict:
        if not self.available:
            raise EstimateError("KI-Task nicht verfügbar (kein HA-Zugang oder keine Entität konfiguriert).")
        text = (text or "").strip()
        if len(text) < 3:
            raise EstimateError("Bitte beschreiben, was du gegessen hast.")
        context = build_context(db, when.date().isoformat())
        last_err: EstimateError | None = None
        # 1. Versuch mit vollem Kontext, 2. Versuch knapp (nur Regeln) – falls die Antwort abbricht
        short = "HEUTE GÜLTIGE" + context.split("HEUTE GÜLTIGE", 1)[-1]
        for attempt, ctx in enumerate((context, short), start=1):
            try:
                return await self._call(db, build_instructions(text[:600], when, ctx))
            except EstimateError as err:
                last_err = err
                log.warning("KI-Schätzung Versuch %s fehlgeschlagen: %s", attempt, err)
        raise last_err or EstimateError("KI-Schätzung fehlgeschlagen.")

    async def _call(self, db: Database, instructions: str) -> dict:
        payload = {"task_name": "Triathlon-Cockpit Makros", "entity_id": self.entity_id,
                   "instructions": instructions, "structure": STRUCTURE}
        try:
            async with self.session.post(
                f"{self.base}/services/ai_task/generate_data?return_response",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload, timeout=aiohttp.ClientTimeout(total=90),
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    log.warning("KI-Task HTTP %s: %s", resp.status, body[:300])
                    raise EstimateError("Die KI-Schätzung ist abgebrochen – bitte nochmal versuchen "
                                        "oder im Chat mit Koch eintragen.")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EstimateError(f"KI-Task nicht erreichbar: {err.__class__.__name__}") from err
        try:
            parsed = json.loads(body)
        except ValueError as err:
            raise EstimateError("Antwort des KI-Tasks nicht lesbar.") from err
        data = (parsed.get("service_response") or {}).get("data") if isinstance(parsed, dict) else None
        return clean_result(data, db)


def parse_when(raw: str | None, now: datetime) -> datetime:
    if not raw:
        return now
    try:
        return datetime.fromisoformat(str(raw)).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(str(raw)[:10]), now.time())
        except ValueError:
            raise EstimateError(f"Zeitpunkt ungültig: {raw!r}")
