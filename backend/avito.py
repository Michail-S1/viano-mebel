"""Local matching and dry-run plans only. This module has no network transport."""
import hashlib
import json
import re
import uuid
import time
import zipfile
from xml.etree.ElementTree import ParseError
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from backend import avito_migrations as migrations
from backend.avito_import import parse_export
from backend.avito_export import patch_workbook
from backend.avito_matching import LABELS, compatibility, evaluate, factory_mentions, match_context, norm, plain, search_matches
from backend.avito_workflow import ReviewIndex, exclusion


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(packed(value).encode("utf-8")).hexdigest()


def money_cents(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Некорректная цена")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > 10**10 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError("Укажите положительную цену с точностью до копейки")
        return int(amount * 100)
    except InvalidOperation:
        raise ValueError("Некорректная цена")


def product_snapshot(product):
    return {key: product.get(key, "") for key in
            ("id", "name", "factory", "collection", "sku", "color", "dimensions", "variant")}


def listing_snapshot(listing):
    return {**{key: listing.get(key) for key in ("title", "description", "category", "attributes")},
            "source_characteristics": listing.get("source_details", {}).get("characteristics", {})}


class Avito:
    def __init__(self, catalog, error, now):
        self.catalog, self.error, self.now = catalog, error, now
        self.import_sessions = {}
        self.price_export_sessions = {}
        self.review_cache = {}
        self.review_revision = None

    def _manually_excluded(self, db, identifier):
        row = db.execute("SELECT action FROM avito_decisions WHERE listing_id=? AND action IN ('exclude','restore') ORDER BY rowid DESC LIMIT 1", (identifier,)).fetchone()
        return bool(row and row[0] == 'exclude')

    def _exclusion(self, db, listing):
        return exclusion(listing) or ("Исключено пользователем" if self._manually_excluded(db, listing['id']) else "")

    def setup(self):
        return migrations.migrate(self.catalog, self.error, self.now)

    def ready(self, db):
        version = migrations.installed(self.catalog, db)
        if version != migrations.VERSION:
            raise self.error("Сначала подготовьте раздел Авито с резервной копией.", 409)

    def event(self, db, kind, details, listing_id=None):
        db.execute("INSERT INTO avito_events VALUES (?,?,?,?,?)",
                   (uuid.uuid4().hex, self.now(), kind, listing_id, packed(details)))

    def invalidate(self, db, reason):
        if migrations.installed(self.catalog, db) != migrations.VERSION:
            return
        count = db.execute("UPDATE avito_plans SET state='stale',invalidated_reason=? WHERE state='prepared'",
                           (reason,)).rowcount
        if count:
            self.event(db, "plans_invalidated", {"reason": reason, "count": count})

    def state(self):
        with self.catalog.lock, self.catalog.connection() as db:
            version = migrations.installed(self.catalog, db)
            base = {"installed": version == migrations.VERSION, "schema_version": version,
                    "transport_enabled": False, "export_adapter_ready": True,
                    "autoload_download_enabled": False,
                    "labels": LABELS, "pilot_factory": "Регион 058"}
            if not base["installed"]:
                return base
            return {**base, "listings": db.execute("SELECT count(*) FROM avito_listings").fetchone()[0],
                    "links": db.execute("SELECT count(*) FROM avito_links").fetchone()[0],
                    "plans": [dict(r) for r in db.execute("SELECT * FROM avito_plans ORDER BY created_at DESC LIMIT 30")]}

    def _listing(self, db, identifier):
        row = db.execute("SELECT * FROM avito_listings WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise self.error("Локальная запись объявления не найдена.", 404)
        result = dict(row)
        result["attributes"] = json.loads(result["attributes"])
        result["source_details"] = json.loads(result.get("source_details", "{}"))
        result["price"] = result["price_cents"] / 100 if result["price_cents"] is not None else None
        result["is_demo"] = bool(result["is_demo"])
        return result

    def _checked_listing(self, db, identifier):
        result = self._listing(db, identifier)
        result["factory_mentions"] = factory_mentions(result, [r[0] for r in db.execute("SELECT DISTINCT factory FROM products")])
        result["match_context"] = match_context(result, [r[0] for r in db.execute("SELECT DISTINCT collection FROM products WHERE collection<>''")])
        return result

    def _product(self, db, identifier):
        row = db.execute("SELECT * FROM products WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise self.error("Товар не найден.", 404)
        return self.catalog.serialize_product(db, row)

    def save_listing(self, payload, demo=False, _db=None, source_details=None):
        if not isinstance(payload, dict):
            raise self.error("Некорректная локальная карточка объявления.")
        avito_id = payload.get("avito_id")
        if not isinstance(avito_id, str) or not re.fullmatch(r"DEMO-[A-Za-z0-9-]{1,50}" if demo else r"[0-9]{1,32}", avito_id):
            raise self.error("Укажите номер существующего объявления строкой из цифр.")
        fields = {}
        for key, limit in (("title", 3000), ("description", 30000), ("category", 1000),
                           ("status", 100), ("url", 2000), ("price_source", 1000)):
            value = payload.get(key, "")
            if not isinstance(value, str) or len(value) > limit:
                raise self.error("Некорректное поле: " + key)
            fields[key] = value.strip()
        if not fields["title"] or not fields["price_source"]:
            raise self.error("Нужны название и источник сведений о цене.")
        if fields["url"]:
            url = urlparse(fields["url"])
            if url.scheme != "https" or url.hostname not in ("avito.ru", "www.avito.ru") or url.username or url.password:
                raise self.error("Ссылка должна вести на HTTPS-страницу Авито.")
        attrs = payload.get("attributes", {})
        allowed = {"factory", "collection", "sku", "color", "dimensions", "material", "variant"}
        if not isinstance(attrs, dict) or set(attrs) - allowed or any(not isinstance(v, str) or len(v) > 3000 for v in attrs.values()):
            raise self.error("Некорректные характеристики объявления.")
        observed = payload.get("observed_at") or None
        if observed is not None:
            try:
                value = datetime.fromisoformat(observed)
                if value.tzinfo is None:
                    raise ValueError()
                observed = value.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                raise self.error("Время наблюдения цены укажите с часовым поясом.")
        try:
            price = money_cents(payload.get("price"))
        except ValueError as error:
            raise self.error(str(error))
        with self.catalog.lock, (nullcontext(_db) if _db is not None else self.catalog.connection()) as db:
            self.ready(db)
            old = db.execute("SELECT * FROM avito_listings WHERE avito_id=?", (avito_id,)).fetchone()
            if old and bool(old["is_demo"]) != demo:
                raise self.error("Тестовые и реальные записи нельзя смешивать.", 409)
            identifier = old["id"] if old else uuid.uuid4().hex
            source = packed(source_details) if source_details is not None else (old["source_details"] if old else "{}")
            values = [fields[k] for k in ("title", "description", "category", "status", "url")]
            values += [packed(attrs), price, fields["price_source"], observed, self.now()]
            comparison = ("title", "description", "category", "status", "url", "attributes", "price_cents", "price_source", "observed_at")
            if old and list(old[k] for k in comparison) == values[:-1] and old["source_details"] == source:
                return self._listing(db, identifier)
            if old:
                # Every re-observation is kept in the journal; unverified price changes require review.
                db.execute("UPDATE avito_listings SET title=?,description=?,category=?,status=?,url=?,attributes=?,"
                           "price_cents=?,price_source=?,observed_at=?,received_at=?,version=version+1,"
                           "price_review=max(price_review,?) WHERE id=?",
                           values + [int(old["price_cents"] != price), identifier])
            else:
                db.execute("INSERT INTO avito_listings (id,avito_id,title,description,category,status,url,attributes,"
                           "price_cents,price_source,observed_at,received_at,is_demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           [identifier, avito_id] + values + [int(demo)])
            db.execute("UPDATE avito_listings SET source_details=? WHERE id=?", (source, identifier))
            self.event(db, "listing_observed", {"avito_id": avito_id, "old_price_cents": old["price_cents"] if old else None,
                       "price_cents": price, "source": fields["price_source"], "observed_at": observed, "is_demo": demo}, identifier)
            self.invalidate(db, "Обновлены сведения об объявлении")
            return self._listing(db, identifier)

    def inspect_export(self, data, filename, mode="add", observed_at=None):
        if not filename.lower().endswith(".xlsx") or mode not in ("add", "update"):
            raise self.error("Поддерживается проверенный XLSX; выберите режим добавления или обновления.")
        if len(data) > 30*1024*1024:
            raise self.error("Файл превышает 30 МБ.")
        if observed_at:
            try:
                moment = datetime.fromisoformat(observed_at)
                if moment.tzinfo is None:
                    raise ValueError()
                observed_at = moment.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                raise self.error("Укажите время снимка с часовым поясом либо оставьте неизвестным.")
        with self.catalog.lock:
            with self.catalog.connection() as db:
                self.ready(db)
            identifier = uuid.uuid4().hex
            directory = self.catalog.incoming / "avito" / identifier
            directory.mkdir(parents=True)
            path = directory / "source.xlsx"
            path.write_bytes(data)
            try:
                parsed = parse_export(path, money_cents)
            except (ValueError, OSError, zipfile.BadZipFile, KeyError) as error:
                raise self.error("Не удалось прочитать выгрузку: " + str(error))
            file_hash = hashlib.sha256(data).hexdigest()
            source = "Выгрузка XLSX: " + filename
            versions = {}
            with self.catalog.connection() as db:
                for item in parsed["records"]:
                    item.update({"price_source": source, "observed_at": observed_at})
                    item["source_details"].update({"file_name": filename, "sha256": file_hash})
                    old = db.execute("SELECT version,observed_at FROM avito_listings WHERE avito_id=?", (item["avito_id"],)).fetchone()
                    versions[item["avito_id"]] = old["version"] if old else None
                    item["import_state"] = "conflict" if item.get("duplicate_conflict") else "existing" if old else "new"
                    if mode == "update" and old and old["observed_at"] and (not observed_at or datetime.fromisoformat(old["observed_at"]) > datetime.fromisoformat(observed_at)):
                        item["import_state"] = "conflict"
                        parsed["issues"].append({"avito_id": item["avito_id"], "message": "Нельзя заменить наблюдение цены более старым или недатированным снимком"})
            # Sessions expire, while the frozen workbook stays in the existing
            # incoming/ backup mechanism as source provenance.
            self.import_sessions = {k:v for k,v in self.import_sessions.items() if time.time()-v["created"] < 3600}
            if len(self.import_sessions) >= 5:
                del self.import_sessions[min(self.import_sessions, key=lambda k: self.import_sessions[k]["created"])]
            self.import_sessions[identifier] = {**parsed, "created": time.time(), "mode": mode,
                                                "versions": versions, "committed": None}
            return {"session_id": identifier, "mode": mode, "file_name": filename,
                    "total": len(parsed["records"]), "categories": parsed["categories"],
                    "sheets": parsed["sheets"], "duplicates": parsed["duplicates"], "issues": parsed["issues"][:100],
                    "issues_count": len(parsed["issues"]),
                    "new": sum(i["import_state"] == "new" for i in parsed["records"]),
                    "existing": sum(i["import_state"] == "existing" for i in parsed["records"]),
                    "sample": [{k:i[k] for k in ("avito_id", "title", "category", "price", "status", "import_state")} for i in parsed["records"][:20]],
                    "notice": "Цена — исторический снимок. AvitoDateEnd не является временем наблюдения. Фотографии не загружаются."}

    def commit_export(self, payload):
        with self.catalog.lock, self.catalog.connection() as db:
            self.ready(db)
            session = self.import_sessions.get(payload.get("session_id"))
            if not session or time.time()-session["created"] > 3600:
                raise self.error("Предпросмотр выгрузки устарел. Выберите файл заново.", 409)
            categories = payload.get("categories")
            if not isinstance(categories, list) or not categories or any(c not in session["categories"] for c in categories):
                raise self.error("Выберите категории из предпросмотра.")
            if any(c not in ("Мебель и интерьер", "Товары для детей и игрушки") for c in categories):
                raise self.error("Этот модуль импортирует только мебель и выбранные детские товары; остальные категории исключены.")
            if session["committed"] is not None:
                if categories != session["committed"]["categories"]:
                    raise self.error("Импорт уже сохранён с другим выбором категорий.", 409)
                return session["committed"]
            selected = [i for i in session["records"] if i["category"] in categories]
            for item in selected:
                old = db.execute("SELECT version FROM avito_listings WHERE avito_id=?", (item["avito_id"],)).fetchone()
                if (old[0] if old else None) != session["versions"][item["avito_id"]]:
                    raise self.error("Реестр изменился после предпросмотра. Проверьте файл заново.", 409)
            counts = {"added": 0, "updated": 0, "skipped": 0, "categories": categories}
            for item in selected:
                if item["import_state"] == "conflict" or (item["import_state"] == "existing" and session["mode"] == "add"):
                    counts["skipped"] += 1
                    continue
                self.save_listing(item, _db=db, source_details=item["source_details"])
                counts["added" if item["import_state"] == "new" else "updated"] += 1
            self.event(db, "export_imported", counts)
        # Mark committed only after the SQL transaction has succeeded.
        session["committed"] = counts
        return counts

    def listings(self, params):
        with self.catalog.lock, self.catalog.connection() as db:
            self.ready(db)
            try:
                page = max(1, int(params.get("page", 1)))
            except (TypeError, ValueError):
                raise self.error("Некорректная страница.")
            where, values = ["1=1"], []
            if params.get("q"):
                # SQL LIKE wildcards are not used for literal searches.
                db.create_function("avito_search", 2, search_matches)
                where.append("avito_search(a.avito_id||' '||a.title||' '||a.description,?)")
                values.append(params["q"])
            if params.get("filter") == "unlinked":
                where.append("l.product_id IS NULL")
            elif params.get("filter") == "linked":
                where.append("l.product_id IS NOT NULL")
            elif params.get("filter") == "demo":
                where.append("a.is_demo=1")
            joins = " FROM avito_listings a LEFT JOIN avito_links l ON l.listing_id=a.id LEFT JOIN products p ON p.id=l.product_id WHERE " + " AND ".join(where)
            rows = db.execute("SELECT a.id,l.product_id,p.name product_name,p.factory linked_factory" + joins +
                              " ORDER BY a.title,a.id", values).fetchall()
            revision = self.catalog.meta(db, "revision")
            if revision != self.review_revision or not hasattr(self, 'review_index'):
                self.review_index = ReviewIndex(db.execute("SELECT DISTINCT factory,collection FROM products").fetchall())
                self.review_revision = revision
                self.review_cache.clear()
            items = []
            for row in rows:
                item = self._listing(db, row["id"])
                item.update({k: row[k] for k in ("product_id", "product_name", "linked_factory")})
                manual = self._manually_excluded(db, item['id'])
                key = (item['id'], item['version'], row['linked_factory'], manual)
                if key not in self.review_cache:
                    self.review_cache[key] = self.review_index.classify(item, row['linked_factory'], manual)
                item['review'] = self.review_cache[key]
                items.append(item)
            factory, queue = params.get('factory'), params.get('queue', 'all')
            scoped = [i for i in items if not factory or i['review']['queue'] == 'unassigned' or
                      (i['linked_factory'] or i['review']['suggested_factory']) == factory or
                      (i['review']['queue'] == 'excluded' and not i['review']['suggested_factory'] and not i['linked_factory'])]
            counts = {q: sum(i['review']['queue'] == q for i in scoped) for q in ('suggested','unassigned','linked','excluded')}
            filtered = scoped if queue == 'all' else [i for i in scoped if i['review']['queue'] == queue]
            collections = sorted({c for i in filtered for c in i['review']['collections']})
            types = sorted({i['review']['item_type']['label'] for i in filtered if i['review']['item_type']})
            if params.get('collection'):
                filtered = [i for i in filtered if params['collection'] in i['review']['collections']]
            if params.get('type'):
                filtered = [i for i in filtered if (i['review']['item_type'] or {}).get('label') == params['type']]
            total = len(filtered)
            pages = max(1, (total+11)//12)
            page = min(page, pages)
            return {"items": filtered[(page-1)*12:page*12], "page": page, "pages": pages, "total": total,
                    "counts": counts, "collections": collections, "types": types,
                    "notice": "Подсказка фабрики помогает отобрать объявления, но не подтверждает соответствие."}

    def detail(self, identifier, params):
        with self.catalog.lock, self.catalog.connection() as db:
            self.ready(db)
            listing = self._checked_listing(db, identifier)
            listing['description_text'] = plain(listing.get('description', ''))
            link = db.execute("SELECT * FROM avito_links WHERE listing_id=?", (identifier,)).fetchone()
            unit = params.get('dimension_unit', json.loads(link['product_snapshot']).get('_dimension_unit', '') if link else '')
            if unit not in ('', 'mm', 'cm', 'm'):
                raise self.error('Выберите единицы габаритов каталога.')
            listing['_dimension_unit'] = unit
            confirmed = None
            if link:
                product = self._product(db, link["product_id"])
                check = compatibility(listing, product, json.loads(link["product_snapshot"]))
                if listing_snapshot(listing) != json.loads(link["listing_snapshot"]):
                    check["conflicts"].append("Содержание объявления изменилось после подтверждения")
                confirmed = {"product": product, "check": check, "confirmed_at": link["confirmed_at"]}
            conditions, values = ["1=1"], []
            if params.get("factory"):
                conditions.append("factory=?")
                values.append(params["factory"])
            if params.get("q"):
                db.create_function("avito_search", 2, search_matches)
                conditions.append("avito_search(search_text,?)")
                values.append(params["q"])
            rejected = {r[0] for r in db.execute("SELECT product_id FROM avito_decisions WHERE listing_id=? AND action='reject'", (identifier,))}
            candidates = []
            excluded_reason = self._exclusion(db, listing)
            excluded = {"type": 0, "collection": 0, "model": 0}
            for row in db.execute("SELECT * FROM products WHERE " + " AND ".join(conditions), values):
                if excluded_reason:
                    break
                if row["id"] in rejected:
                    continue
                check = evaluate(listing, dict(row))
                if check["type_mismatch"]:
                    excluded["type"] += 1
                    continue
                if check["collection_mismatch"]:
                    excluded["collection"] += 1
                    continue
                if check["model_mismatch"] and not params.get('q'):
                    excluded["model"] += 1
                    continue
                if check["relevant"] or params.get("q"):
                    candidates.append((check, row))
            if not params.get("q") and any(check["type_match"] for check, _ in candidates):
                # Unnamed modules remain available via explicit text search,
                # but must not displace recognizable beds, dressers, etc.
                candidates = [(check, row) for check, row in candidates if check["type_match"]]
            candidates.sort(key=lambda pair: (-pair[0]["type_match"], bool(pair[0]["conflicts"]), -pair[0]["collection_match"], -pair[0]["rank"], pair[1]["id"]))
            selected = []
            for _, row in candidates[:10]:
                product = self.catalog.serialize_product(db, row)
                selected.append({"product": product, "check": evaluate(listing, product)})
            if excluded_reason:
                category = "excluded"
            elif confirmed:
                category = "conflict" if confirmed["check"]["conflicts"] else "confirmed"
            elif not selected:
                category = "not_found"
            elif all(item["check"]["conflicts"] for item in selected):
                category = "conflict"
            elif len([item for item in selected if not item["check"]["conflicts"]]) > 1:
                category = "multiple"
            else:
                category = selected[0]["check"]["category"]
            return {"listing": listing, "confirmed": confirmed, "candidates": selected,
                    "link_version": link["version"] if link else None,
                    "match_context": listing["match_context"], "excluded": excluded,
                    "excluded_reason": excluded_reason,
                    "auto_excluded": bool(exclusion(listing)),
                    'dimension_unit': unit,
                    "category": category, "label": "Исключено из синхронизации" if excluded_reason else LABELS[category], "candidate_count": len(candidates),
                    "history": [dict(r) for r in db.execute("SELECT * FROM avito_decisions WHERE listing_id=? ORDER BY created_at DESC LIMIT 100", (identifier,))]}

    def decide(self, payload):
        action = payload.get("action")
        if action not in ("confirm", "reject", "unlink", "leave", "exclude", "restore"):
            raise self.error("Неизвестное решение.")
        reason = payload.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 3000:
            raise self.error("Некорректная причина решения.")
        with self.catalog.lock, self.catalog.connection() as db:
            self.ready(db)
            listing = self._checked_listing(db, payload.get("listing_id"))
            if payload.get("listing_version") != listing["version"]:
                raise self.error("Объявление изменилось. Откройте его заново.", 409)
            current_link = db.execute("SELECT version FROM avito_links WHERE listing_id=?", (listing["id"],)).fetchone()
            if payload.get("link_version") != (current_link[0] if current_link else None):
                raise self.error("Связь изменилась в другом окне. Откройте объявление заново.", 409)
            product = None
            if action in ("confirm", "reject"):
                product = self._product(db, payload.get("product_id"))
            if action == "confirm":
                if self._exclusion(db, listing):
                    raise self.error("Объявление исключено из синхронизации: комплект нельзя связать с одним товаром.", 409)
                if payload.get("product_snapshot") != product_snapshot(product):
                    raise self.error("Товар изменился. Обновите список кандидатов.", 409)
                unit = payload.get('dimension_unit', '')
                if unit not in ('', 'mm', 'cm', 'm'):
                    raise self.error('Выберите единицы габаритов каталога.')
                check = evaluate({**listing, '_dimension_unit': unit}, product)
                if check["conflicts"]:
                    raise self.error("Явные характеристики противоречат товару. Исправьте локальные сведения и проверьте связь.", 409)
                if check['warnings'] and payload.get('acknowledged_warnings') != check['warnings']:
                    raise self.error('Отдельно проверьте и подтвердите предупреждения.', 409)
                if not reason.strip():
                    raise self.error("Укажите основание ручного подтверждения, особенно если фабрика отсутствует.")
                previous = db.execute("SELECT version FROM avito_links WHERE listing_id=?", (listing["id"],)).fetchone()
                db.execute("INSERT INTO avito_links VALUES (?,?,?,?,?,?) ON CONFLICT(listing_id) DO UPDATE SET "
                           "product_id=excluded.product_id,product_snapshot=excluded.product_snapshot,listing_snapshot=excluded.listing_snapshot,"
                           "confirmed_at=excluded.confirmed_at,version=excluded.version",
                           (listing["id"], product["id"], packed({**product_snapshot(product), '_dimension_unit': unit, '_reviewed_warnings': check['warnings']}), packed(listing_snapshot(listing)),
                            self.now(), previous[0]+1 if previous else 1))
            elif action in ("unlink", "leave", "exclude"):
                db.execute("DELETE FROM avito_links WHERE listing_id=?", (listing["id"],))
            elif action == "reject":
                existing = db.execute("SELECT product_id FROM avito_links WHERE listing_id=?", (listing["id"],)).fetchone()
                if existing and existing[0] == product["id"]:
                    raise self.error("Сначала отмените подтверждённую связь.", 409)
            db.execute("INSERT INTO avito_decisions VALUES (?,?,?,?,?,?)",
                       (uuid.uuid4().hex, listing["id"], product["id"] if product else None, action, reason, self.now()))
            self.event(db, "link_" + action, {"product_id": product["id"] if product else None, "reason": reason,
                       **({'dimension_unit': unit, 'reviewed_warnings': check['warnings']} if action == 'confirm' else {})}, listing["id"])
            self.invalidate(db, "Изменены решения о сопоставлении")
            return {"ok": True}

    def prepare(self, payload):
        factory = payload.get("factory")
        if not isinstance(factory, str) or not factory.strip():
            raise self.error("Выберите одну фабрику.")
        with self.catalog.lock, self.catalog.connection() as db:
            self.ready(db)
            settings = self.catalog.meta(db, "settings")
            revision = self.catalog.meta(db, "revision")
            items = []
            for link in db.execute("SELECT l.* FROM avito_links l JOIN products p ON p.id=l.product_id WHERE p.factory=? ORDER BY l.listing_id", (factory,)).fetchall():
                product = self._product(db, link["product_id"])
                listing = self._checked_listing(db, link["listing_id"])
                if self._exclusion(db, listing):
                    continue
                check = compatibility(listing, product, json.loads(link["product_snapshot"]))
                if listing_snapshot(listing) != json.loads(link["listing_snapshot"]):
                    check["conflicts"].append("Содержание объявления изменилось после подтверждения")
                try:
                    target = money_cents(product["sale_price"])
                except ValueError:
                    target = None
                if target is None:
                    check["conflicts"].append("Нет корректной розничной цены каталога")
                if check["conflicts"]:
                    state = "conflict"
                elif listing["price_review"]:
                    state = "price_review"
                elif listing["price_cents"] is None or not listing["observed_at"]:
                    state = "price_unknown"
                elif target == listing["price_cents"]:
                    state = "unchanged"
                else:
                    state = "blocked_transport"
                reasons = check["conflicts"] or [{"conflict": "Проверьте связь", "price_review": "Наблюдаемая цена изменилась вне подтверждённой операции; требуется проверка",
                    "price_unknown": "Цена или время наблюдения неизвестны", "unchanged": "Цена совпадает с последней наблюдаемой",
                    "blocked_transport": "Только подготовка: официальный транспорт не подтверждён; актуальность цены проверить на Авито"}[state]]
                if listing["is_demo"]:
                    reasons = ["ТЕСТОВОЕ объявление; никогда не отправляется на Авито"] + reasons
                reasons += check.get('warnings', [])
                items.append({"listing_id": listing["id"], "product_id": product["id"], "avito_id": listing["avito_id"],
                              "previous_cents": listing["price_cents"], "target_cents": target,
                              "price_source": listing["price_source"], "observed_at": listing["observed_at"],
                              "listing_version": listing["version"], "link_version": link["version"],
                              "product_snapshot": product_snapshot(product), "state": state, "reasons": reasons})
            fingerprint = digest({"factory": factory, "settings": settings, "items": items})
            previous = db.execute("SELECT id FROM avito_plans WHERE fingerprint=? AND state='prepared'", (fingerprint,)).fetchone()
            if previous:
                return self._plan(db, previous[0])
            identifier = uuid.uuid4().hex
            db.execute("INSERT INTO avito_plans VALUES (?,?,?,?,?,?,?,?,?)",
                       (identifier, factory, self.now(), revision, packed(settings), "prepared", fingerprint,
                        int(any(self._listing(db, i["listing_id"])["is_demo"] for i in items)), None))
            for item in items:
                db.execute("INSERT INTO avito_plan_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, identifier, item["listing_id"], item["product_id"], item["avito_id"],
                            item["previous_cents"], item["target_cents"], item["price_source"], item["observed_at"],
                            item["listing_version"], item["link_version"], packed(item["product_snapshot"]), item["state"], packed(item["reasons"])))
            self.event(db, "dry_run_prepared", {"plan_id": identifier, "factory": factory, "items": len(items)})
            return self._plan(db, identifier)

    def _plan(self, db, identifier):
        row = db.execute("SELECT * FROM avito_plans WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise self.error("Тестовый план не найден.", 404)
        result = dict(row)
        result["items"] = []
        for row in db.execute("SELECT * FROM avito_plan_items WHERE plan_id=? ORDER BY avito_id", (identifier,)):
            item = dict(row)
            item["product_snapshot"] = json.loads(item["product_snapshot"])
            item["reasons"] = json.loads(item["reasons"])
            result["items"].append(item)
        result["transport_enabled"] = False
        return result

    def plan(self, identifier):
        with self.catalog.lock, self.catalog.connection() as db:
            self.ready(db)
            return self._plan(db, identifier)

    def cancel(self, identifier):
        with self.catalog.lock, self.catalog.connection() as db:
            self.ready(db)
            self._plan(db, identifier)
            db.execute("UPDATE avito_plans SET state='cancelled' WHERE id=?", (identifier,))
            self.event(db, "plan_cancelled", {"plan_id": identifier})
            return {"ok": True}

    def _export_items(self, db, plan_id, identifiers):
        plan = self._plan(db, plan_id)
        if (plan['state'] != 'prepared' or plan['revision'] != self.catalog.meta(db, 'revision')
                or json.loads(plan['settings']) != self.catalog.meta(db, 'settings')):
            raise self.error('Сравнение устарело. Нажмите «Сравнить цены» заново.', 409)
        if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 10
                or any(not isinstance(i, str) for i in identifiers) or len(set(identifiers)) != len(identifiers)):
            raise self.error('Для пилота выберите от 1 до 10 разных объявлений одной фабрики.')
        items = {i['listing_id']: i for i in plan['items']}
        result = []
        for identifier in identifiers:
            item = items.get(identifier)
            if not item:
                raise self.error('Объявление не входит в сравнение выбранной фабрики.', 409)
            listing = self._checked_listing(db, identifier)
            link = db.execute('SELECT * FROM avito_links WHERE listing_id=?', (identifier,)).fetchone()
            if not link or listing['is_demo'] or self._exclusion(db, listing):
                raise self.error('Нет подтверждённой связи, запись исключена или является DEMO.', 409)
            product = self._product(db, link['product_id'])
            if (link['version'] != item['link_version'] or listing['version'] != item['listing_version']
                    or link['product_id'] != item['product_id'] or product['factory'] != plan['factory']
                    or product_snapshot(product) != item['product_snapshot']
                    or listing_snapshot(listing) != json.loads(link['listing_snapshot'])
                    or compatibility(listing, product, json.loads(link['product_snapshot']))['conflicts']
                    or money_cents(product['sale_price']) != item['target_cents'] or item['target_cents'] is None):
                raise self.error('Связь или цена изменилась. Проверьте соответствие и создайте новое сравнение.', 409)
            result.append((item, listing))
        return plan, result

    def preview_price_export(self, data, filename, payload):
        if not filename.lower().endswith('.xlsx') or len(data) > 30*1024*1024:
            raise self.error('Выберите полную свежую выгрузку XLSX размером до 30 МБ.')
        with self.catalog.lock, self.catalog.connection() as db:
            self.ready(db)
            identifiers = payload.get('listing_ids')
            plan, items = self._export_items(db, payload.get('plan_id'), identifiers)
            identifier = uuid.uuid4().hex
            directory = self.catalog.incoming/'avito-price-export'/identifier
            directory.mkdir(parents=True)
            (directory/'source.xlsx').write_bytes(data)
            try:
                parsed = parse_export(directory/'source.xlsx', money_cents)
                by_id = {r['avito_id']: r for r in parsed['records']}
                requests = {}
                source_hash = hashlib.sha256(data).hexdigest()
                for item, listing in items:
                    fresh = by_id.get(listing['avito_id'])
                    export_id = listing['source_details'].get('export_id')
                    if not export_id or not fresh or listing_snapshot(fresh) != listing_snapshot(listing):
                        raise ValueError('Содержание объявления отличается или нет исходного Id: '+listing['avito_id']+'. Импортируйте свежую выгрузку и проверьте связь')
                    if listing['source_details'].get('sha256') != source_hash:
                        raise ValueError('Это не та выгрузка, из которой обновлены выбранные объявления. Импортируйте этот файл в режиме обновления, затем сравните цены заново')
                    requests[listing['avito_id']] = {'export_id': export_id, 'target_cents': item['target_cents']}
                required = [r[0] for r in db.execute('SELECT avito_id FROM avito_listings WHERE is_demo=0')]
                output, report = patch_workbook(data, requests, money_cents, required, strict_coverage=False)
                missing = set(report['missing_registry_ids'])
                report['missing_registry'] = [{'avito_id': r[0], 'title': r[1]} for r in db.execute('SELECT avito_id,title FROM avito_listings WHERE is_demo=0') if r[0] in missing]
            except (ValueError, OSError, KeyError, zipfile.BadZipFile, ParseError) as error:
                raise self.error('Экспорт не подготовлен: '+str(error))
            report.update({'session_id': identifier, 'factory': plan['factory'], 'created_at': self.now(),
                           'upload_allowed': False,
                           'selected_count': len(items), 'selected_found_count': len(items),
                           'selected_items': [{'avito_id': item['avito_id'], 'title': listing['title'],
                               'product_name': item['product_snapshot']['name'], 'target_cents': item['target_cents'],
                               'found_in_source': True} for item, listing in items],
                           'source_name': filename, 'source_sha256': hashlib.sha256(data).hexdigest(),
                           'output_sha256': hashlib.sha256(output).hexdigest(),
                           'filename': 'Avito-prices-'+identifier[:8]+'.xlsx',
                           'notice': 'Только локальная проверка. Скачивание Excel для автозагрузки заблокировано: не подтверждено, что Авито применит только выбранные цены и не затронет остальные строки. Изменение нескольких ячеек в Excel не ограничивает область обработки файла на Авито.'})
            (directory/'prices.xlsx').write_bytes(output)
            (directory/'report.json').write_text(packed(report), encoding='utf-8')
            self.price_export_sessions = {k:v for k,v in self.price_export_sessions.items() if time.time()-v['created'] < 3600}
            if len(self.price_export_sessions) >= 5:
                del self.price_export_sessions[next(iter(self.price_export_sessions))]
            self.price_export_sessions[identifier] = {'created': time.time(), 'directory': directory,
                'plan_id': plan['id'], 'listing_ids': list(identifiers), 'report': report}
            self.event(db, 'price_file_prepared', report)
            return report

    def download_price_export(self, payload):
        # A locally correct workbook is not proof of price-only processing by Avito.
        # Neither a checkbox nor a cached 1.3.1 session can establish that guarantee.
        raise self.error('Скачивание Excel для автозагрузки заблокировано: область применения полного файла на Авито не подтверждена. Доступны только проверка и отчёт изменений. Ранее скачанные файлы пока не загружайте.', 403)

    def journal(self):
        with self.catalog.lock, self.catalog.connection() as db:
            self.ready(db)
            return {"items": [dict(r) for r in db.execute("SELECT * FROM avito_events ORDER BY created_at DESC LIMIT 200")]}
