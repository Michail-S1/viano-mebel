"""Proven supplier layout equivalences; never fuzzy matching of furniture variants."""
import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal
from .colors import _palette, derive_color

VERSION = 1

def folded(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold().replace("ё", "е")

def header_context(factory, sheet, headers, mapping):
    labels = {h["column"]: folded(h.get("label")) for h in headers}
    name = labels.get(mapping.get("name"), "")
    price = labels.get(mapping.get("price"), "")
    if folded(factory) == "дсв" and "модул" in name and not mapping.get("collection"):
        body = any("корпус" in v and ("цен" in v or v == "корпус") for v in labels.values())
        facade = any("фасад" in v and ("цен" in v or v == "фасад") for v in labels.values())
        total = "модул" in price or "продукц" in price
        if body and facade and total and not re.search("корпус|фасад", price):
            return {"version": VERSION, "kind": "dsv-module", "collection": sheet}
    if folded(factory) == "sv-мебель" and "цветовая гамма" in labels.get(mapping.get("color"), ""):
        return {"version": VERSION, "kind": "sv-colours"}
    return {}

def profile_contexts(db):
    result = {}
    for row in db.execute("SELECT name,factory,config FROM profiles"):
        config = json.loads(row["config"])
        for sheet in config.get("sheets", []):
            result[(row["name"], sheet["name"])] = header_context(
                sheet.get("factory") or row["factory"], sheet["name"],
                sheet.get("headers", []), sheet.get("mapping", {}))
    return result

def colour_options(value):
    # Semicolon options in the explicitly declared supplier colour gamut.
    # Slashes and two-colour combinations remain actual selected finishes.
    return str(value or "").count(";") >= 2 and _palette(folded(value))

def normalize_product(product, context=None):
    result = dict(product)
    context = dict(context or product.get("identity_context") or {})
    kind = context.get("kind")
    if kind == "dsv-module" and folded(product.get("factory")) == "дсв" and folded(product.get("variant")) in ("", "модуль"):
        context.setdefault("original_collection", result.get("collection", ""))
        context.setdefault("original_dimensions", result.get("dimensions", ""))
        context.setdefault("original_variant", result.get("variant", ""))
        result["collection"] = context["collection"]
        result["variant"] = "Модуль"
        size = str(result.get("dimensions") or "").strip()
        scalar = re.fullmatch(r"(?:[Шш](?:ирина)?\s*:\s*)?(\d+(?:[.,]\d+)?)", size)
        if scalar:
            number = format(Decimal(scalar.group(1).replace(",", ".")).normalize(), "f")
            result["dimensions"] = "Ш: " + number
        result["identity_context"] = context
    elif kind == "sv-colours" and folded(product.get("factory")) == "sv-мебель":
        raw = str(result.get("color") or "")
        if colour_options(raw):
            context["palette"] = raw
            collection = str(result.get("collection") or "")
            if collection.endswith(raw):
                context["original_collection"] = collection
                result["collection"] = collection[:-len(raw)].rstrip(" |;,")
            result["color"] = derive_color(result.get("name", ""), collection=result.get("collection", ""))
        if context.get("palette"):
            result["identity_context"] = context
    return result

def normalized_source(row, contexts):
    source = json.loads(row["source_values"])
    context = source.get("identity_context") or contexts.get((source.get("source_file"), source.get("source_sheet")))
    return normalize_product(source, context)

def semantic_key(product):
    normalized = normalize_product(product)
    context = normalized.get("identity_context") or {}
    if context.get("kind") == "dsv-module" and normalized.get("variant") == "Модуль":
        pass
    elif context.get("kind") == "sv-colours" and context.get("palette"):
        pass
    else:
        return None
    from .importer import product_key
    return "supplier-v1:" + product_key(normalized)

def original_key(product):
    """The exact pre-normalization key, used only to flag unresolved legacy collisions."""
    context = product.get("identity_context") or {}
    if not context:
        return None
    original = dict(product)
    for field in ("collection", "dimensions", "variant"):
        if "original_" + field in context:
            original[field] = context["original_" + field]
    if context.get("palette"):
        original["color"] = context["palette"]
    from .importer import product_key
    return product_key(original)


def semantic_index(db, factories=None):
    contexts = profile_contexts(db)
    result = defaultdict(list)
    query, args = "SELECT * FROM products", []
    if factories is not None:
        if not factories:
            return result
        query += " WHERE factory IN (" + ",".join("?" for _ in factories) + ")"
        args = list(factories)
    for row in db.execute(query, args):
        key = semantic_key(normalized_source(row, contexts))
        if key:
            result[key].append(row["id"])
    return result

def repair_plan(db):
    contexts = profile_contexts(db)
    groups = defaultdict(list)
    modes = {r["created_at"]: r["mode"] for r in db.execute("SELECT created_at,mode FROM imports")}
    for row in db.execute("SELECT rowid AS position,* FROM products ORDER BY rowid"):
        product = dict(row)
        product["normalized_source"] = normalized_source(row, contexts)
        key = semantic_key(product["normalized_source"])
        if key:
            groups[key].append(product)
    safe, review = [], []
    for key, products in groups.items():
        if len(products) < 2:
            continue
        if len({folded(p["normalized_source"].get("name")) for p in products}) != 1:
            review.append({"ids": [p["id"] for p in products], "reason": "Разные названия одного артикула"})
            continue
        # Duplicate rows in one supplier sheet can be real option/price groups.
        provenance = [(p["source_file"], p["source_sheet"]) for p in products]
        if len(set(provenance)) != len(products):
            review.append({"ids": [p["id"] for p in products], "reason": "Несколько строк в одном прайсе"})
            continue
        overrides = {}
        conflict = False
        for p in products:
            for field, value in json.loads(p["overrides"]).items():
                if field in overrides and overrides[field] != value:
                    conflict = True
                overrides[field] = value
        if conflict:
            review.append({"ids": [p["id"] for p in products], "reason": "Противоречивые ручные изменения"})
            continue
        keeper = products[0]
        newest = max(products, key=lambda p: (p["imported_at"], p["position"]))
        source = newest if modes.get(newest["imported_at"]) == "update" else keeper
        safe.append({"key": key, "keeper": keeper, "duplicates": products[1:],
                     "source": source["normalized_source"], "imported_at": source["imported_at"],
                     "overrides": overrides})
    from .source_identity import repair_groups
    excluded={p['id'] for g in safe for p in [g['keeper']]+g['duplicates']}
    cross=repair_groups(db,excluded)
    return {"groups": safe+cross['groups'], "review": review+cross['review']}

def apply_plan(catalog, db, plan):
    from .importer import product_key
    fields = ("name", "factory", "collection", "sku", "color", "dimensions", "variant")
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    removed, remapped, moved_photos = 0, {}, 0
    from .source_identity import remember, history
    for group in plan["groups"]:
        keeper = group["keeper"]["id"]
        if group.get("original_sources"):
            originals=list(group["original_sources"])
            for duplicate in group["duplicates"]:
                originals.extend(history(db,duplicate["id"]))
            remember(catalog,db,keeper,originals)
        primary = db.execute("SELECT id FROM photos WHERE product_id=? AND is_primary=1", (keeper,)).fetchone()
        hashes = {}
        for photo in db.execute("SELECT id,filename FROM photos WHERE product_id=?", (keeper,)):
            path = catalog.photos_dir/photo["filename"]
            if path.is_file():
                hashes[hashlib.sha256(path.read_bytes()).hexdigest()] = photo["id"]
        known = set(catalog.meta(db, "excel_photos:"+keeper) or [])
        for duplicate in group["duplicates"]:
            identifier = duplicate["id"]
            for photo in db.execute("SELECT * FROM photos WHERE product_id=? ORDER BY created_at,id", (identifier,)).fetchall():
                path = catalog.photos_dir/photo["filename"]
                digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                if digest and digest in hashes:
                    # Preserve the physical file and the photo URL, remove only its repeated registration.
                    db.execute("DELETE FROM photos WHERE id=?", (photo["id"],))
                else:
                    db.execute("UPDATE photos SET product_id=?,is_primary=0 WHERE id=?", (keeper, photo["id"]))
                    if digest:
                        hashes[digest] = photo["id"]
                    moved_photos += 1
            known.update(catalog.meta(db, "excel_photos:"+identifier) or [])
            db.execute("UPDATE aliases SET product_id=? WHERE product_id=?", (keeper, identifier))
            db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (duplicate["identity_key"], keeper))
            for table in ("avito_links", "avito_plan_items", "avito_decisions"):
                if table in tables:
                    db.execute("UPDATE " + table + " SET product_id=? WHERE product_id=?", (keeper, identifier))
            db.execute("DELETE FROM products WHERE id=?", (identifier,))
            remapped[identifier] = keeper
            removed += 1
        if not primary:
            primary = db.execute("SELECT id FROM photos WHERE product_id=? ORDER BY created_at,id LIMIT 1", (keeper,)).fetchone()
            if primary:
                db.execute("UPDATE photos SET is_primary=1 WHERE id=?", (primary[0],))
        if known:
            catalog.set_meta(db, "excel_photos:"+keeper, sorted(known))
        source, overrides = group["source"], group["overrides"]
        display = {field: overrides.get(field, source.get(field, "")) for field in fields}
        search = " ".join(folded(display[field]) for field in fields)
        db.execute("UPDATE products SET " + ",".join(field+"=?" for field in fields) +
                   ",base_price=?,source_file=?,source_sheet=?,source_row=?,imported_at=?,source_values=?,overrides=?,search_text=? WHERE id=?",
                   [display[field] for field in fields] + [source["base_price"], source["source_file"], source["source_sheet"],
                   source["source_row"], group["imported_at"], json.dumps(source, ensure_ascii=False),
                   json.dumps(overrides, ensure_ascii=False), search, keeper])
        for key in (group["key"], product_key(source)):
            existing = db.execute("SELECT product_id FROM aliases WHERE identity_key=?", (key,)).fetchone()
            if existing and existing[0] != keeper:
                raise ValueError("Противоречивое совпадение ключей: исправление отменено.")
            db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (key, keeper))
    redirects=catalog.meta(db,"product_redirects") or {}
    redirects.update((catalog.meta(db,"catalog_identity_repair") or {}).get("redirects",{}))
    redirects.update(remapped)
    if redirects:
        for identifier in list(redirects):
            seen={identifier};target=redirects[identifier]
            while target in redirects and target not in seen:
                seen.add(target);target=redirects[target]
            redirects[identifier]=target
        catalog.set_meta(db,"product_redirects",redirects)
    if removed:
        catalog.bump(db)
    return {"removed": removed, "groups": len(plan["groups"]), "moved_photos": moved_photos,
            "review": plan["review"], "redirects": remapped}