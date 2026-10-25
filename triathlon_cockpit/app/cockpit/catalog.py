"""Stammdaten des Kochs (Lebensmittel, Rezepte, Regeln, Quellen) + Import aus Notion."""

from __future__ import annotations

import json
from datetime import date, datetime

from aiohttp import web

from .db import Database

ENTITIES = {
    "foods": {
        "table": "foods", "title": "name",
        "fields": ["name", "brand", "category", "kcal_100", "protein_100", "carbs_100", "fat_100", "fiber_100",
                   "calcium_100", "salt_100", "sugar_100", "unit_g", "nutrient_source", "note"],
        "numbers": {"kcal_100", "protein_100", "carbs_100", "fat_100", "fiber_100", "calcium_100", "salt_100",
                    "sugar_100", "unit_g"},
        "search": ["name", "brand"],
    },
    "recipes": {
        "table": "recipes", "title": "name",
        "fields": ["name", "cooked_on", "ingredients", "portions", "total_weight_g", "weighed", "kcal", "protein_g",
                   "carbs_g", "fat_g", "fiber_g", "calcium_mg", "leftover_g", "rule_check", "warnings", "note"],
        "numbers": {"portions", "total_weight_g", "kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "calcium_mg",
                    "leftover_g"},
        "search": ["name", "ingredients"],
    },
    "rules": {
        "table": "nutrition_rules", "title": "rule",
        "fields": ["rule", "phase", "kind", "applies_to", "details", "severity", "valid_from", "valid_to", "active",
                   "source_title"],
        "numbers": set(),
        "search": ["rule", "details", "applies_to"],
    },
    "sources": {
        "table": "sources", "title": "title",
        "fields": ["title", "url", "kind", "topic", "retrieved_on", "key_points", "excerpt", "notion_url"],
        "numbers": set(),
        "search": ["title", "key_points", "topic"],
    },
}

ENTITY_DOCS = [
    {"method": "GET", "path": "/api/v1/{foods|recipes|rules|sources}", "params": "q (Suchtext, optional)",
     "does": "Lebensmittel-Stamm, Rezepte (inkl. Werte pro 100 g fertig), Ernährungsregeln, Quellen auflisten"},
    {"method": "GET", "path": "/api/v1/{foods|recipes|rules|sources}/{id}", "does": "Einen Eintrag lesen"},
    {"method": "POST", "path": "/api/v1/{foods|recipes|rules|sources}",
     "body": {k: v["fields"] for k, v in ENTITIES.items()}, "does": "Eintrag anlegen"},
    {"method": "PATCH", "path": "/api/v1/{foods|recipes|rules|sources}/{id}", "does": "Eintrag ändern"},
    {"method": "POST", "path": "/api/v1/import",
     "body": "{foods, recipes, rules, sources, targets, weights, meals}: Listen mit notion_id; mehrfach ausführbar",
     "does": "Einmaliger Umzug aus Notion (aktualisiert statt doppelt anzulegen)"},
]


def _bad(msg: str) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text=json.dumps({"error": msg}, ensure_ascii=False), content_type="application/json")


def _clean(entity: str, body: dict, partial: bool, allow_notion: bool = False) -> dict:
    spec = ENTITIES[entity]
    allowed = set(spec["fields"]) | ({"notion_id"} if allow_notion else set())
    unknown = set(body) - allowed
    if unknown:
        raise _bad(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    data = {k: body[k] for k in body if k in allowed}
    if not partial and not str(data.get(spec["title"], "") or "").strip():
        raise _bad(f"{spec['title']} fehlt")
    for k in spec["numbers"] & set(data):
        if data[k] in ("", None):
            data[k] = None
            continue
        try:
            data[k] = float(data[k])
        except (TypeError, ValueError):
            raise _bad(f"{k} muss eine Zahl sein")
    for k in ("active", "weighed"):
        if k in data and data[k] is not None:
            data[k] = 1 if data[k] in (True, 1, "1", "true", "ja", "__YES__") else 0
    for k, v in list(data.items()):
        if isinstance(v, (list, dict)):
            data[k] = ", ".join(map(str, v)) if isinstance(v, list) else json.dumps(v, ensure_ascii=False)
    return data


def _with_per100(row: dict) -> dict:
    w = row.get("total_weight_g")
    if w:
        for k, out in (("kcal", "kcal_100"), ("protein_g", "protein_100"), ("carbs_g", "carbs_100"),
                       ("fat_g", "fat_100")):
            if row.get(k) is not None:
                row[out] = round(row[k] / w * 100, 1)
    return row


def _upsert(db: Database, table: str, data: dict, key: str) -> int:
    existing = db.query(f"SELECT id FROM {table} WHERE {key} = ?", (data[key],))
    if existing:
        sets = ", ".join(f"{k} = ?" for k in data if k != key)
        if sets:
            db.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?",
                       [v for k, v in data.items() if k != key] + [data[key]])
        return int(existing[0]["id"])
    cols = ", ".join(data)
    return db.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(data))})", list(data.values()))


def register_catalog_api(app: web.Application, db: Database, clean_meal) -> None:
    def spec_of(request: web.Request) -> tuple[str, dict]:
        entity = request.match_info["entity"]
        if entity not in ENTITIES:
            raise web.HTTPNotFound(text=json.dumps({"error": f"Unbekannt: {entity}"}), content_type="application/json")
        return entity, ENTITIES[entity]

    async def _json(request: web.Request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise _bad("Body muss JSON sein")
        if not isinstance(body, dict):
            raise _bad("Body muss ein JSON-Objekt sein")
        return body

    async def list_items(request: web.Request):
        entity, spec = spec_of(request)
        q = (request.query.get("q") or "").strip()
        where, params = [], []
        if q:
            where.append("(" + " OR ".join(f"{c} LIKE ?" for c in spec["search"]) + ")")
            params += [f"%{q}%"] * len(spec["search"])
        if entity == "rules" and request.query.get("all") != "1":
            # Standard: nur die am Tag `on` (Standard heute) gültigen, aktiven Regeln
            try:
                on = date.fromisoformat(request.query.get("on") or date.today().isoformat()).isoformat()
            except ValueError:
                raise _bad("on muss YYYY-MM-DD sein")
            where.append("active = 1 AND (valid_from IS NULL OR valid_from = '' OR valid_from <= ?) "
                         "AND (valid_to IS NULL OR valid_to = '' OR valid_to >= ?)")
            params += [on, on]
        sql = f"SELECT * FROM {spec['table']}" + (" WHERE " + " AND ".join(where) if where else "")
        sql += f" ORDER BY {'cooked_on DESC, ' if entity == 'recipes' else ''}id"
        rows = db.query(sql, params)
        if entity == "recipes":
            rows = [_with_per100(r) for r in rows]
        return web.json_response({entity: rows, "count": len(rows)})

    async def get_item(request: web.Request):
        entity, spec = spec_of(request)
        rows = db.query(f"SELECT * FROM {spec['table']} WHERE id = ?", (request.match_info["id"],))
        if not rows:
            raise web.HTTPNotFound(text=json.dumps({"error": "nicht gefunden"}), content_type="application/json")
        return web.json_response(_with_per100(rows[0]) if entity == "recipes" else rows[0])

    async def create_item(request: web.Request):
        entity, spec = spec_of(request)
        data = _clean(entity, await _json(request), partial=False)
        if entity == "foods" and db.query("SELECT id FROM foods WHERE name = ?", (data["name"],)):
            raise _bad("Lebensmittel mit diesem Namen gibt es schon – bitte PATCH verwenden")
        cols = ", ".join(data)
        new_id = db.execute(f"INSERT INTO {spec['table']} ({cols}) VALUES ({', '.join('?' * len(data))})",
                            list(data.values()))
        return web.json_response({"ok": True, "id": new_id}, status=201)

    async def update_item(request: web.Request):
        entity, spec = spec_of(request)
        item_id = request.match_info["id"]
        if not db.query(f"SELECT id FROM {spec['table']} WHERE id = ?", (item_id,)):
            raise web.HTTPNotFound(text=json.dumps({"error": "nicht gefunden"}), content_type="application/json")
        data = _clean(entity, await _json(request), partial=True)
        if not data:
            raise _bad("Keine Felder zum Ändern")
        db.execute(f"UPDATE {spec['table']} SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                   [*data.values(), item_id])
        row = db.query(f"SELECT * FROM {spec['table']} WHERE id = ?", (item_id,))[0]
        return web.json_response({"ok": True, "item": row})

    async def import_all(request: web.Request):
        body = await _json(request)
        report: dict[str, int] = {}
        for entity in ("foods", "sources", "rules", "recipes"):
            n = 0
            for item in body.get(entity) or []:
                if not item.get("notion_id"):
                    raise _bad(f"{entity}: notion_id fehlt")
                _upsert(db, ENTITIES[entity]["table"], _clean(entity, item, partial=False, allow_notion=True),
                        "notion_id")
                n += 1
            report[entity] = n
        n = 0
        for t in body.get("targets") or []:
            ds = date.fromisoformat(str(t["day"])[:10]).isoformat()
            db.execute("INSERT INTO day_targets(day, day_type, kcal, protein_g, carbs_g, fat_g, note, notion_id) "
                       "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET day_type=excluded.day_type, "
                       "kcal=excluded.kcal, protein_g=excluded.protein_g, carbs_g=excluded.carbs_g, "
                       "fat_g=excluded.fat_g, note=excluded.note, notion_id=excluded.notion_id",
                       [ds, t.get("day_type"), t.get("kcal"), t.get("protein_g"), t.get("carbs_g"),
                        t.get("fat_g"), t.get("note"), t.get("notion_id")])
            n += 1
        report["targets"] = n
        n = 0
        for w in body.get("weights") or []:
            ds = date.fromisoformat(str(w["day"])[:10]).isoformat()
            db.execute("INSERT INTO body_weight(day, weight_kg, note) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE "
                       "SET weight_kg=excluded.weight_kg, note=excluded.note",
                       [ds, float(w["weight_kg"]), w.get("note") or "aus Notion"])
            n += 1
        report["weights"] = n
        n = 0
        for m in body.get("meals") or []:
            m = dict(m)
            nid = m.pop("notion_id", None)
            rec_nid = m.pop("recipe_notion_id", None)
            if not nid:
                raise _bad("meals: notion_id fehlt")
            data = clean_meal(m, partial=False)
            if rec_nid:
                rec = db.query("SELECT id FROM recipes WHERE notion_id = ?", (rec_nid,))
                if rec:
                    data["recipe_id"] = rec[0]["id"]
            data.setdefault("source", "notion")
            data["notion_id"] = nid
            data.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
            _upsert(db, "meals", data, "notion_id")
            n += 1
        report["meals"] = n
        return web.json_response({"ok": True, "imported": report})

    r = app.router
    r.add_post("/api/v1/import", import_all)
    r.add_get("/api/v1/{entity:foods|recipes|rules|sources}", list_items)
    r.add_post("/api/v1/{entity:foods|recipes|rules|sources}", create_item)
    r.add_get("/api/v1/{entity:foods|recipes|rules|sources}/{id:\\d+}", get_item)
    r.add_patch("/api/v1/{entity:foods|recipes|rules|sources}/{id:\\d+}", update_item)
