"""Local furniture catalog. Personal data stays local; network is used only for updates."""
import argparse
import base64
import collections
import csv
import hashlib
import io
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))
from backend.importer import inspect_file, parse_file, product_key, normalize_text
from backend.colors import derive_color
from backend.images import product_pictures
from backend.dimensions import make_matcher
from backend.avito import Avito
from launcher import update as updater
try:
    from PIL import Image, ImageOps
except ImportError:
    Image = ImageOps = None

VERSION = "1.3.0"
TEXT_FIELDS = ("name", "factory", "collection", "sku", "color", "dimensions", "variant")
SOURCE_FIELDS = TEXT_FIELDS + ("base_price", "source_file", "source_sheet", "source_row")
DEFAULTS = {"markup_percent": 20, "fixed_add": 1000, "rounding": "ruble"}
MAX_BODY = 240 * 1024 * 1024

class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status

def now():
    return datetime.now(timezone.utc).isoformat()

def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

def sale_price(base, settings):
    if base is None:
        return None
    amount = Decimal(str(base)) * (1 + Decimal(str(settings["markup_percent"])) / 100)
    amount += Decimal(str(settings["fixed_add"]))
    unit = {"none": None, "ruble": 1, "ten": 10, "hundred": 100}[settings["rounding"]]
    if unit is not None:
        amount = (amount / unit).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * unit
    else:
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(amount)

def decode_file(item, limit=MAX_BODY):
    content = item.get("content", "")
    if not isinstance(content, str):
        raise ApiError("Некорректное содержимое файла.")
    if content.startswith("data:"):
        content = content.split(",", 1)[-1]
    try:
        value = base64.b64decode(content, validate=True)
    except Exception:
        raise ApiError("Не удалось прочитать файл: некорректный формат данных.")
    if not value or len(value) > limit:
        raise ApiError("Файл пустой или превышает допустимый размер.")
    return value

def clean_name(name):
    return Path(str(name).replace("\\", "/")).name[:200]

def search_normalize(value):
    return normalize_text(value).casefold().replace("ё", "е")

def profile_family(name):
    text = search_normalize(Path(name).stem)
    text = re.sub(r"\d{1,2}[._/-]\d{1,2}[._/-]\d{2,4}", "", text)
    text = re.sub(r"\b(?:от|с)\b", "", text)
    return re.sub(r"\s+", " ", text).strip()

class Catalog:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "catalog.sqlite3"
        self.photos_dir = self.data_dir / "photos"
        self.incoming = self.data_dir / "incoming"
        self.photos_dir.mkdir(exist_ok=True)
        self.incoming.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.sessions = {}
        self.stop_event = threading.Event()
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS products (
                    id TEXT PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, factory TEXT NOT NULL, collection TEXT NOT NULL DEFAULT '',
                    sku TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '',
                    dimensions TEXT NOT NULL DEFAULT '', variant TEXT NOT NULL DEFAULT '',
                    base_price REAL, source_file TEXT NOT NULL, source_sheet TEXT NOT NULL,
                    source_row INTEGER, imported_at TEXT NOT NULL,
                    source_values TEXT NOT NULL, overrides TEXT NOT NULL DEFAULT '{}',
                    search_text TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_products_factory ON products(factory,collection);
                CREATE TABLE IF NOT EXISTS aliases (identity_key TEXT PRIMARY KEY, product_id TEXT NOT NULL
                    REFERENCES products(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS photos (
                    id TEXT PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL UNIQUE, original_name TEXT NOT NULL,
                    is_primary INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_photos_product ON photos(product_id);
                CREATE INDEX IF NOT EXISTS idx_aliases_product ON aliases(product_id);
                CREATE TABLE IF NOT EXISTS imports (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, mode TEXT NOT NULL, files TEXT NOT NULL,
                    added INTEGER NOT NULL, updated INTEGER NOT NULL, duplicates INTEGER NOT NULL,
                    issues INTEGER NOT NULL, details TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS profiles (
                    family TEXT PRIMARY KEY, name TEXT NOT NULL, factory TEXT NOT NULL, config TEXT NOT NULL,
                    updated_at TEXT NOT NULL);
            """)
            for key, value in (("settings", DEFAULTS), ("revision", 0),
                               ("folder", {"path": "", "watch": False, "mode": "add", "status": "idle",
                                           "last_scan": None, "error": None, "pending_session": None}),
                               ("folder_hashes", {})):
                db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, compact(value)))
        with self.connection() as db:
            folder = self.meta(db, "folder") or {}
            if folder.get("pending_session"):
                folder.update({"pending_session": None, "status": "watching" if folder.get("watch") else "idle"})
                self.set_meta(db, "folder", folder)
        self.avito = Avito(self, ApiError, now)
        self.watcher = threading.Thread(target=self.watch_loop, name="Folder watcher", daemon=True)
        self.watcher.start()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def meta(self, db, key):
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, db, key, value):
        db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, compact(value)))

    def bump(self, db):
        self.set_meta(db, "revision", int(self.meta(db, "revision") or 0) + 1)
        if hasattr(self, "avito"):
            self.avito.invalidate(db, "Каталог или настройки изменились; подготовьте план заново")

    def serialize_product(self, db, row, settings=None):
        result = dict(row)
        result.pop("identity_key", None)
        result.pop("search_text", None)
        result.pop("source_values", None)
        result["overrides"] = json.loads(result.get("overrides", "{}"))
        if "color" not in result["overrides"]:
            result["color"] = derive_color(result["name"], collection=result["collection"],
                                           variant=result["variant"], existing=result["color"])
        result["sale_price"] = sale_price(result["base_price"], settings or self.meta(db, "settings"))
        result["photos"] = [dict(p) for p in db.execute(
            "SELECT id,filename,original_name,is_primary FROM photos WHERE product_id=? "
            "ORDER BY is_primary DESC,created_at,id", (result["id"],))]
        for photo in result["photos"]:
            photo["url"] = "/photos/" + photo.pop("filename")
            photo["is_primary"] = bool(photo["is_primary"])
        result["photo_url"] = result["photos"][0]["url"] if result["photos"] else None
        return result

    def product(self, product_id):
        with self.lock, self.connection() as db:
            row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if row is None:
                raise ApiError("Товар не найден.", 404)
            return self.serialize_product(db, row)

    def state(self):
        with self.lock, self.connection() as db:
            factories = [dict(r) for r in db.execute(
                "SELECT factory name,count(*) count FROM products GROUP BY factory ORDER BY factory")]
            collections_list = [dict(r) for r in db.execute(
                "SELECT collection name,factory,count(*) count FROM products "
                "WHERE collection<>'' GROUP BY factory,collection ORDER BY factory,collection")]
            imports = [dict(r) for r in db.execute("SELECT * FROM imports ORDER BY created_at DESC LIMIT 30")]
            for entry in imports:
                entry["files"] = json.loads(entry["files"])
                all_details = json.loads(entry["details"])
                entry["details_count"] = len(all_details)
                entry["details"] = all_details[:50]
            profiles = [dict(r) for r in db.execute(
                "SELECT family,name,factory,updated_at FROM profiles ORDER BY updated_at DESC")]
            total = db.execute("SELECT count(*) FROM products").fetchone()[0]
            photos = db.execute("SELECT count(DISTINCT product_id) FROM photos").fetchone()[0]
            return {"version": VERSION, "revision": self.meta(db,"revision"), "stats": {"total": total, "factories": len(factories),
                    "collections": len(collections_list), "with_photos": photos,
                    "last_import": imports[0]["created_at"] if imports else None},
                    "settings": self.meta(db, "settings"), "factories": factories,
                    "collections": collections_list, "imports": imports, "profiles": profiles,
                    "folder": self.meta(db, "folder")}

    def list_products(self, params):
        with self.lock, self.connection() as db:
            settings = self.meta(db, "settings")
            sql, values = ["1=1"], []
            for term in search_normalize(params.get("q", "")).split():
                sql.append("instr(search_text,?)>0")
                values.append(term)
            for field in ("factory", "collection"):
                if params.get(field):
                    sql.append(field + "=?")
                    values.append(params[field])
            if params.get("has_photo") in ("1", "true", "yes"):
                sql.append("EXISTS(SELECT 1 FROM photos WHERE photos.product_id=products.id)")
            if params.get("has_photo") in ("0", "false", "no"):
                sql.append("NOT EXISTS(SELECT 1 FROM photos WHERE photos.product_id=products.id)")
            db.create_function("sale_price", 1, lambda x: sale_price(x, settings))
            for param, comparator in (("min", ">="), ("max", "<=")):
                if params.get(param) not in (None, ""):
                    try:
                        price = float(params[param])
                        if not math.isfinite(price) or price < 0:
                            raise ValueError()
                    except ValueError:
                        raise ApiError("Укажите корректную цену для фильтра.")
                    sql.append("sale_price(base_price)" + comparator + "?")
                    values.append(price)
            try:
                dimension_matcher = make_matcher(params)
            except ValueError as error:
                raise ApiError(str(error))
            dimension_filter = None
            if dimension_matcher and dimension_matcher.axes:
                dimension_filter = {"axes": list(dimension_matcher.axes), "considered": 0,
                                    "recognized": 0, "unrecognized": 0, "empty": 0, "examples": []}
                base_where = " AND ".join(sql)
                for size, count in db.execute("SELECT dimensions,count(*) FROM products WHERE " + base_where + " GROUP BY dimensions", values):
                    if not dimension_matcher.text_matches(size):
                        continue
                    dimension_filter["considered"] += count
                    if dimension_matcher.recognizes(size):
                        dimension_filter["recognized"] += count
                    else:
                        dimension_filter["unrecognized"] += count
                        if not size:
                            dimension_filter["empty"] += count
                        elif len(dimension_filter["examples"]) < 3:
                            dimension_filter["examples"].append(size)
            if dimension_matcher:
                db.create_function("dimension_matches", 1, dimension_matcher)
                sql.append("dimension_matches(dimensions)=1")
            where = " AND ".join(sql)
            sort = {"name": "name,id", "name_desc": "name DESC,id", "price_asc": "base_price,id",
                    "price_desc": "base_price DESC,id", "newest": "imported_at DESC,id",
                    "factory": "factory,name,id"}.get(params.get("sort"), "factory,collection,source_row,id")
            try:
                page_size = max(1, min(120, int(params.get("page_size", 36))))
                page = max(1, int(params.get("page", 1)))
            except (ValueError, TypeError):
                raise ApiError("Некорректный номер страницы.")
            total = db.execute("SELECT count(*) FROM products WHERE " + where, values).fetchone()[0]
            pages = max(1, math.ceil(total / page_size))
            page = min(page, pages)
            rows = db.execute("SELECT * FROM products WHERE " + where + " ORDER BY " + sort +
                              " LIMIT ? OFFSET ?", values + [page_size, (page-1)*page_size]).fetchall()
            return {"items": [self.serialize_product(db, r, settings) for r in rows],
                    "total": total, "page": page, "pages": pages, "dimension_filter": dimension_filter}

    def edit_product(self, product_id, fields):
        with self.lock, self.connection() as db:
            row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if row is None:
                raise ApiError("Товар не найден.", 404)
            overrides = json.loads(row["overrides"])
            current = dict(row)
            for key in TEXT_FIELDS:
                if key in fields:
                    value = str(fields[key] or "").strip()
                    if len(value) > 3000 or (key in ("name", "factory") and not value):
                        raise ApiError("Наименование и фабрика обязательны, длина поля — до 3000 символов.")
                    current[key] = value
                    overrides[key] = value
            search = search_normalize(" ".join(current[k] for k in TEXT_FIELDS))
            db.execute("UPDATE products SET " + ",".join(k+"=?" for k in TEXT_FIELDS) +
                       ",overrides=?,search_text=? WHERE id=?",
                       [current[k] for k in TEXT_FIELDS] + [compact(overrides), search, product_id])
            # Original identity remains an alias: manual corrections survive subsequent imports.
            new_key = product_key(current)
            existing = db.execute("SELECT product_id FROM aliases WHERE identity_key=?", (new_key,)).fetchone()
            if existing is None:
                db.execute("INSERT INTO aliases VALUES (?,?)", (new_key, product_id))
            self.bump(db)
        return self.product(product_id)

    def inspect(self, payload):
        session_id = uuid.uuid4().hex
        session_dir = self.incoming / session_id
        session_dir.mkdir()
        files = []
        supplied = payload.get("files", [])
        for entry in supplied:
            filename = clean_name(entry.get("name", ""))
            if Path(filename).suffix.lower() not in (".xls", ".xlsx"):
                raise ApiError("Поддерживаются только файлы .xls и .xlsx.")
            file_dir = session_dir / uuid.uuid4().hex
            file_dir.mkdir()
            path = file_dir / filename
            path.write_bytes(decode_file(entry, 80 * 1024 * 1024))
            files.append((path, filename))
        paths = list(payload.get("paths", []))
        if payload.get("folder"):
            folder = Path(payload["folder"]).expanduser().resolve()
            if not folder.is_dir():
                raise ApiError("Папка с прайсами не найдена.")
            paths += [str(p) for p in sorted(folder.iterdir())
                      if p.is_file() and p.suffix.lower() in (".xls", ".xlsx") and not p.name.startswith("~$")]
        for path_value in paths:
            path = Path(path_value).expanduser().resolve()
            if not path.is_file() or path.suffix.lower() not in (".xls", ".xlsx"):
                raise ApiError("Excel-файл не найден: " + str(path))
            if path.stat().st_size > 80 * 1024 * 1024:
                raise ApiError("Прайс превышает 80 МБ.")
            # Freeze a source snapshot, so the confirmed preview matches what is committed.
            file_dir = session_dir / uuid.uuid4().hex
            file_dir.mkdir()
            snapshot = file_dir / path.name
            shutil.copyfile(path, snapshot)
            files.append((snapshot, path.name))
        if not files:
            raise ApiError("Выберите хотя бы один Excel-файл.")
        if len(files) > 60:
            raise ApiError("За один импорт можно загрузить до 60 файлов.")
        configs, file_paths = [], {}
        watch_markers = {}
        for original in paths:
            original_path = Path(original).expanduser().resolve()
            stat = original_path.stat()
            watch_markers[str(original_path)] = [stat.st_mtime_ns, stat.st_size]
        with self.lock, self.connection() as db:
            for path, name in files:
                file_id = uuid.uuid4().hex
                try:
                    config = inspect_file(str(path), file_id)
                    config["name"] = name
                    # Factory inference must use the original source filename.
                    if not config.get("factory"):
                        try:
                            from backend.importer import infer_factory
                            config["factory"] = infer_factory(name)
                        except (ImportError, TypeError):
                            pass
                    if not config.get("factory"):
                        factory_patterns = [("регина", "Регина"), ("стенд", "Стендмебель"),
                            ("альбина", "Альбина"), ("росток", "Росток"), ("эста", "ЭСТА"),
                            ("sv-", "SV-Мебель"), ("nn-", "NN-Мебель"), ("алён", "Алёнушка"),
                            ("дсв", "ДСВ"), ("миф", "МИФ"), ("пирамида", "Пирамида")]
                        text = search_normalize(name)
                        config["factory"] = next((label for pattern, label in factory_patterns if pattern in text), "")
                    saved = db.execute("SELECT config FROM profiles WHERE family=?", (profile_family(name),)).fetchone()
                    if saved:
                        profile = json.loads(saved[0])
                        config["factory"] = profile.get("factory") or config.get("factory", "")
                        saved_sheets = {s["name"]: s for s in profile.get("sheets", [])}
                        for sheet in config["sheets"]:
                            if sheet["name"] in saved_sheets:
                                old = saved_sheets[sheet["name"]]
                                sheet["selected"] = old.get("selected", sheet.get("selected", True))
                                sheet["mapping"].update(old.get("mapping", {}))
                        config["profile_used"] = True
                    configs.append(config)
                    file_paths[file_id] = {"path": str(path), "name": name}
                except Exception as error:
                    raise ApiError("Не удалось прочитать «" + name + "»: " + str(error))
            self.sessions[session_id] = {"files": file_paths, "configs": configs, "created": time.time(), "watch_markers": watch_markers}
        return {"session_id": session_id, "files": configs}

    def get_session(self, session_id):
        session = self.sessions.get(session_id)
        if not session or time.time() - session["created"] > 12 * 3600:
            raise ApiError("Предпросмотр устарел. Загрузите прайсы заново.", 404)
        return session

    def preview(self, payload):
        session_id = payload.get("session_id")
        with self.lock:
            session = self.get_session(session_id)
            mode = payload.get("mode", "add")
            if mode not in ("add", "update"):
                raise ApiError("Неизвестный режим импорта.")
            configs = payload.get("files") or session["configs"]
            candidates, issues, warnings = [], [], []
            candidate_photos = collections.defaultdict(list)
            photo_assets = {}
            rejected_photos = set()
            for config in configs:
                source = session["files"].get(config.get("file_id"))
                if source is None:
                    raise ApiError("Файл не найден в текущем импорте.")
                if not str(config.get("factory", "")).strip():
                    issues.append({"name": source["name"], "status": "issue",
                                   "message": "Укажите фабрику для этого файла.", "source_file": source["name"]})
                    continue
                parsed = parse_file(source["path"], config)
                pictures, photo_warnings = product_pictures(source["path"], config, parsed.get("products", [])) if config.get("import_photos", True) else ({}, [])
                warnings.extend(photo_warnings)
                issue_rows = {(issue.get("source_sheet"),issue.get("source_row")) for issue in parsed.get("issues",[])}
                warnings.extend(parsed.get("warnings", []))
                for issue in parsed.get("issues", []):
                    issues.append({**issue, "status": "issue"})
                for item in parsed.get("products", []):
                    product = {key: item.get(key, "" if key in TEXT_FIELDS else None) for key in SOURCE_FIELDS}
                    product["source_file"] = source["name"]
                    product["name"] = str(product["name"] or "").strip()
                    product["factory"] = str(product["factory"] or config["factory"]).strip()
                    for key in TEXT_FIELDS:
                        product[key] = str(product.get(key) or "").strip()
                    try:
                        price = float(product["base_price"])
                        if not math.isfinite(price) or price <= 0:
                            raise ValueError()
                        product["base_price"] = price
                    except (TypeError, ValueError):
                        if (product.get("source_sheet"),product.get("source_row")) not in issue_rows:
                            issues.append({**product, "status": "issue", "message": "Нет корректной исходной цены."})
                        continue
                    if not product["name"]:
                        continue
                    candidates.append(product)
                    identity = product_key(product)
                    for picture in pictures.get((product["source_sheet"], product["source_row"]), []):
                        digest = picture["digest"]
                        if digest in rejected_photos:
                            continue
                        if digest not in photo_assets:
                            try:
                                data, extension = self.prepare_photo(picture["data"])
                                photo_assets[digest] = {"data": data, "extension": extension, "name": clean_name(picture["name"])}
                            except ApiError as error:
                                rejected_photos.add(digest)
                                warnings.append("Фото пропущено: " + str(error))
                                continue
                        if digest not in candidate_photos[identity]:
                            candidate_photos[identity].append(digest)

            by_key = collections.OrderedDict()
            for product in candidates:
                by_key.setdefault(product_key(product), []).append(product)
            actions, result_items = [], list(issues)
            photo_actions, photo_count, photo_products = [], 0, 0
            counts = {"new": 0, "updated": 0, "duplicates": 0, "issues": len(issues)}
            with self.connection() as db:
                settings = self.meta(db, "settings")
                for identity, batch in by_key.items():
                    first = batch[0]
                    distinct_prices = {round(float(p["base_price"]), 8) for p in batch}
                    distinct_names = {search_normalize(p["name"]) for p in batch}
                    if len(distinct_prices) > 1 or len(distinct_names) > 1:
                        counts["issues"] += len(batch)
                        for product in batch:
                            result_items.append({**product, "status": "issue",
                                "message": "Одна позиция с разными ценами или названиями в загружаемых файлах. Проверьте источник."})
                        continue
                    existing_id = db.execute(
                        "SELECT id FROM products WHERE identity_key=? UNION "
                        "SELECT product_id FROM aliases WHERE identity_key=? LIMIT 1",
                        (identity, identity)).fetchone()
                    existing = db.execute("SELECT * FROM products WHERE id=?", (existing_id[0],)).fetchone() if existing_id else None
                    target_id = existing["id"] if existing is not None else uuid.uuid4().hex
                    if existing is not None:
                        source_values = json.loads(existing["source_values"])
                        if first.get("sku") and search_normalize(source_values.get("name", "")) != search_normalize(first["name"]):
                            counts["issues"] += len(batch)
                            result_items.append({**first, "status": "issue",
                                "message": "Артикул найден, но название в прайсе изменилось. Требуется проверка."})
                            continue
                        changed = any(first.get(k) != source_values.get(k) for k in SOURCE_FIELDS
                                      if k not in ("source_file", "source_sheet", "source_row"))
                        if mode == "update" and changed:
                            status = "updated"
                            counts["updated"] += 1
                            actions.append({"status": status, "id": existing["id"], "key": identity, "product": first})
                        else:
                            status = "duplicate"
                            counts["duplicates"] += 1
                    else:
                        status = "new"
                        counts["new"] += 1
                        actions.append({"status": status, "id": target_id, "key": identity, "product": first})
                    photo_keys = candidate_photos.get(identity, [])
                    known = set(self.meta(db, "excel_photos:" + target_id) or [])
                    pending = [digest for digest in photo_keys if digest not in known][:20]
                    existing_hashes = self.photo_content_hashes(db, target_id)
                    additions = len({hashlib.sha256(photo_assets[digest]["data"]).hexdigest()
                                     for digest in pending} - existing_hashes)
                    if len(photo_keys) > 20:
                        warnings.append("У позиции больше 20 фото: за один импорт добавятся первые 20 новых.")
                    if pending:
                        photo_actions.append({"id": target_id, "digests": pending})
                        photo_count += additions
                        photo_products += bool(additions)
                    photo_preview = {"excel_photo_url": "/api/import/" + session_id + "/photos/" + photo_keys[0],
                                     "excel_photos_count": len(photo_keys)} if photo_keys else {}
                    result_items.append({**first, **photo_preview, "status": status, "sale_price": sale_price(first["base_price"], settings),
                                         "message": {"new": "Новая позиция", "updated": "Обновление данных из прайса",
                                                     "duplicate": "Уже есть в каталоге"}[status]})
                    if len(batch) > 1:
                        counts["duplicates"] += len(batch)-1
                        for duplicate in batch[1:]:
                            result_items.append({**duplicate, "status": "duplicate",
                                                 "message": "Повтор строки в загруженных прайсах."})
                session.update({"actions": actions, "mode": mode, "counts": counts,
                                "configs": configs, "details": result_items,
                                "revision": self.meta(db, "revision"), "photo_actions": photo_actions,
                                "photo_assets": photo_assets, "photos_count": photo_count})
            return {"session_id": session_id, "mode": mode, "counts": counts,
                    "items": self.preview_sample(result_items), "warnings": warnings,
                    "total_rows": len(result_items), "preview_limit": 300,
                    "photos": {"new": photo_count, "products": photo_products}}

    @staticmethod
    def preview_sample(items):
        groups = {status: [item for item in items if item["status"] == status]
                  for status in ("issue","new","updated","duplicate")}
        selected = []
        for index in range(300):
            nonempty = [group for group in groups.values() if group]
            if not nonempty:
                break
            item = nonempty[index % len(nonempty)].pop(0)
            selected.append({**item, "color": derive_color(item.get("name", ""),
                collection=item.get("collection", ""), variant=item.get("variant", ""), existing=item.get("color", ""))})
        return selected

    def report(self, identifier, session=False):
        with self.lock:
            if session:
                entries = [item for item in self.get_session(identifier).get("details", []) if item["status"] == "issue"]
            else:
                with self.connection() as db:
                    row = db.execute("SELECT details FROM imports WHERE id=?", (identifier,)).fetchone()
                    if row is None:
                        raise ApiError("Загрузка не найдена.",404)
                    entries = json.loads(row[0])
            fields = ("status","message","factory","collection","name","sku","color","dimensions","base_price",
                      "source_file","source_sheet","source_row")
            labels = ("Статус","Причина","Фабрика","Коллекция","Товар","Артикул","Цвет","Размеры","Цена из прайса",
                      "Файл","Лист","Строка")
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer,delimiter=";")
            writer.writerow(labels)
            for item in entries:
                values = []
                for field in fields:
                    value = item.get(field,"")
                    if isinstance(value,str) and value.lstrip().startswith(("=","+","-","@")):
                        value = "'" + value
                    values.append(value)
                writer.writerow(values)
            return buffer.getvalue().encode("utf-8-sig")

    def photo_content_hashes(self, db, product_id):
        hashes = set()
        for row in db.execute("SELECT filename FROM photos WHERE product_id=?", (product_id,)):
            path = self.photos_dir / row["filename"]
            if path.is_file():
                hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
        return hashes

    def attach_excel_photos(self, db, action, assets, written):
        product_id = action["id"]
        key = "excel_photos:" + product_id
        recorded = set(self.meta(db, key) or [])
        existing = self.photo_content_hashes(db, product_id)
        has_photo = db.execute("SELECT 1 FROM photos WHERE product_id=? LIMIT 1", (product_id,)).fetchone() is not None
        added = 0
        for digest in action["digests"][:20]:
            if digest in recorded:
                continue
            asset = assets[digest]
            content_hash = hashlib.sha256(asset["data"]).hexdigest()
            recorded.add(digest)
            if content_hash in existing:
                continue
            image_id = uuid.uuid4().hex
            filename = image_id + asset["extension"]
            path = self.photos_dir / filename
            written.append(path)
            path.write_bytes(asset["data"])
            db.execute("INSERT INTO photos VALUES (?,?,?,?,?,?)",
                (image_id, product_id, filename, asset["name"], int(not has_photo), now()))
            has_photo = True
            existing.add(content_hash)
            added += 1
        self.set_meta(db, key, sorted(recorded))
        return added

    def commit(self, session_id):
        written = []
        with self.lock:
            try:
                with self.connection() as db:
                    session = self.get_session(session_id)
                    if session.get("committed"):
                        return session["committed"]
                    if "actions" not in session:
                        raise ApiError("Сначала откройте предварительный просмотр.")
                    if session["revision"] != self.meta(db, "revision"):
                        raise ApiError("Каталог изменился. Обновите предварительный просмотр перед сохранением.", 409)
                    imported_at = now()
                    for action in session["actions"]:
                        source = action["product"]
                        if action["status"] == "new":
                            values = {key: source.get(key, "") for key in TEXT_FIELDS}
                            search = search_normalize(" ".join(values.values()))
                            columns = ["id", "identity_key"] + list(TEXT_FIELDS) + [
                                "base_price", "source_file", "source_sheet", "source_row", "imported_at", "source_values", "overrides", "search_text"]
                            row = [action["id"], action["key"]] + list(values.values()) + [
                                source["base_price"], source["source_file"], source["source_sheet"],
                                source["source_row"], imported_at, compact(source), "{}", search]
                            db.execute("INSERT INTO products (" + ",".join(columns) + ") VALUES (" +
                                       ",".join("?" for _ in columns) + ")", row)
                            db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (action["key"], action["id"]))
                        else:
                            current = db.execute("SELECT overrides FROM products WHERE id=?", (action["id"],)).fetchone()
                            overrides = json.loads(current["overrides"])
                            display = {key: overrides.get(key, source.get(key, "")) for key in TEXT_FIELDS}
                            search = search_normalize(" ".join(display.values()))
                            db.execute("UPDATE products SET " + ",".join(key+"=?" for key in TEXT_FIELDS) +
                                ",base_price=?,source_file=?,source_sheet=?,source_row=?,imported_at=?,source_values=?,search_text=? WHERE id=?",
                                list(display.values()) + [source["base_price"], source["source_file"], source["source_sheet"],
                                source["source_row"], imported_at, compact(source), search, action["id"]])
                    for config in session["configs"]:
                        name = session["files"][config["file_id"]]["name"]
                        db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?)",
                            (profile_family(name), name, config.get("factory", ""), compact(config), imported_at))
                    counts = session["counts"]
                    import_id = uuid.uuid4().hex
                    names = [source["name"] for source in session["files"].values()]
                    details = [item for item in session["details"] if item["status"] == "issue"]
                    db.execute("INSERT INTO imports VALUES (?,?,?,?,?,?,?,?,?)",
                               (import_id, imported_at, session["mode"], compact(names), counts["new"],
                                counts["updated"], counts["duplicates"], counts["issues"], compact(details)))
                    photos_added = sum(self.attach_excel_photos(db, action, session.get("photo_assets", {}), written)
                                       for action in session.get("photo_actions", []))
                    self.bump(db)
                    result = {"added": counts["new"], "updated": counts["updated"], "duplicates": counts["duplicates"],
                              "issues": counts["issues"], "import_id": import_id, "photos_added": photos_added}
                    folder = self.meta(db, "folder") or {}
                    if folder.get("path"):
                        markers = self.meta(db, "folder_hashes") or {}
                        for original, marker in session.get("watch_markers", {}).items():
                            if Path(original).parent == Path(folder["path"]):
                                markers[original] = marker
                        self.set_meta(db, "folder_hashes", markers)
                    if folder.get("pending_session") == session_id:
                        folder.update({"pending_session": None, "status": "watching" if folder.get("watch") else "idle", "error": None})
                        self.set_meta(db, "folder", folder)

            except Exception:
                for path in written:
                    path.unlink(missing_ok=True)
                raise
            session["committed"] = result
            return result

    def settings(self, payload):
        with self.lock, self.connection() as db:
            settings = self.meta(db, "settings")
            for key in ("markup_percent", "fixed_add"):
                if key in payload:
                    try:
                        value = float(payload[key])
                    except (TypeError, ValueError):
                        raise ApiError("Наценка и прибавка должны быть числами.")
                    if not math.isfinite(value) or value < 0 or value > 1000000 or (key == "markup_percent" and value > 1000):
                        raise ApiError("Укажите неотрицательные значения: наценка до 1000%, прибавка до 1 000 000 ₽.")
                    settings[key] = value
            if "rounding" in payload:
                if payload["rounding"] not in ("none", "ruble", "ten", "hundred"):
                    raise ApiError("Неизвестное правило округления.")
                settings["rounding"] = payload["rounding"]
            self.set_meta(db, "settings", settings)
            self.bump(db)
            return settings

    def prepare_photo(self, data):
        if not data or len(data) > 15*1024*1024:
            raise ApiError('Фото пустое или превышает 15 МБ.')
        if Image is not None:
            try:
                with Image.open(io.BytesIO(data)) as image:
                    if image.width * image.height > 60_000_000:
                        raise ApiError("Слишком большое разрешение фотографии.")
                    image.load()
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((2000, 2000))
                    if image.mode in ("RGBA","LA") or "transparency" in image.info:
                        image = image.convert("RGBA")
                        background = Image.new("RGB",image.size,"white")
                        background.paste(image,mask=image.getchannel("A"))
                        image = background
                    else:
                        image = image.convert("RGB")
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=88, optimize=True)
                    data = buffer.getvalue()
                extension = ".jpg"
            except ApiError:
                raise
            except Exception:
                raise ApiError("Фотография не распознана. Используйте JPEG, PNG или WebP.")
        else:
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                extension = ".png"
            elif data.startswith(b"\xff\xd8\xff"):
                extension = ".jpg"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                extension = ".webp"
            else:
                raise ApiError("Используйте фотографию JPEG, PNG или WebP.")
        return data, extension

    def add_photos(self, product_id, payload):
        # Validate every supplied image before changing the product.
        self.product(product_id)
        images = []
        for entry in payload.get("files", []):
            data = decode_file(entry, 15*1024*1024)
            data, extension = self.prepare_photo(data)
            image_id = uuid.uuid4().hex
            images.append((image_id, image_id+extension, clean_name(entry.get("name", "Фото")), data))
        if not images:
            raise ApiError("Выберите фотографии.")
        if len(images) > 20:
            raise ApiError("Добавляйте не больше 20 фотографий за один раз.")
        written = []
        try:
            with self.lock, self.connection() as db:
                if db.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone() is None:
                    raise ApiError("Товар не найден.", 404)
                has_primary = db.execute("SELECT id FROM photos WHERE product_id=? LIMIT 1", (product_id,)).fetchone()
                for index, (image_id, filename, original, data) in enumerate(images):
                    path = self.photos_dir / filename
                    path.write_bytes(data)
                    written.append(path)
                    db.execute("INSERT INTO photos VALUES (?,?,?,?,?,?)",
                        (image_id, product_id, filename, original, int(not has_primary and index == 0), now()))
                self.bump(db)
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return self.product(product_id)

    def primary_photo(self, photo_id):
        with self.lock, self.connection() as db:
            row = db.execute("SELECT product_id FROM photos WHERE id=?", (photo_id,)).fetchone()
            if row is None:
                raise ApiError("Фото не найдено.", 404)
            db.execute("UPDATE photos SET is_primary=0 WHERE product_id=?", (row["product_id"],))
            db.execute("UPDATE photos SET is_primary=1 WHERE id=?", (photo_id,))
            self.bump(db)
        return {"ok": True}

    def delete_photo(self, photo_id):
        with self.lock, self.connection() as db:
            row = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
            if row is None:
                raise ApiError("Фото не найдено.", 404)
            db.execute("DELETE FROM photos WHERE id=?", (photo_id,))
            if row["is_primary"]:
                other = db.execute("SELECT id FROM photos WHERE product_id=? ORDER BY created_at LIMIT 1",
                                   (row["product_id"],)).fetchone()
                if other:
                    db.execute("UPDATE photos SET is_primary=1 WHERE id=?", (other["id"],))
            self.bump(db)
        # Preserve the file for existing snapshots; active exports include only database-referenced files.
        return {"ok": True}

    def backup(self):
        with self.lock, self.connection() as db:
            snapshot = self.data_dir / ("backup-" + uuid.uuid4().hex + ".sqlite3")
            memory = sqlite3.connect(snapshot)
            try:
                db.backup(memory)
                memory.execute("PRAGMA journal_mode=DELETE")
                memory.close()
                data = snapshot.read_bytes()
            finally:
                try:
                    memory.close()
                finally:
                    snapshot.unlink(missing_ok=True)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", compact({"app": "furniture-catalog", "version": 1,
                                                          "created_at": now()}))
                archive.writestr("catalog.sqlite3", data)
                for row in db.execute("SELECT filename FROM photos"):
                    photo = self.photos_dir / row["filename"]
                    if not photo.is_file():
                        raise ApiError("Резервная копия не создана: отсутствует файл фотографии.")
                    archive.write(photo, "photos/" + row["filename"])
            return buffer.getvalue()

    def restore(self, payload):
        data = decode_file(payload)
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
            members = archive.infolist()
            if len(members) > 50000 or sum(m.file_size for m in members) > 2*1024*1024*1024:
                raise ApiError("Архив превышает допустимый размер.")
            seen = set()
            for member in members:
                path = PurePosixPath(member.filename)
                if (member.filename in seen or "\\" in member.filename or path.is_absolute() or
                        ".." in path.parts or ":" in member.filename or
                        (member.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ApiError("Некорректные пути внутри архива.")
                seen.add(member.filename)
                if member.is_dir():
                    continue
                if member.filename not in ("manifest.json", "catalog.sqlite3") and not (
                        len(path.parts) == 2 and path.parts[0] == "photos" and
                        re.fullmatch(r"[a-f0-9]{32}\.(jpg|png|webp)", path.parts[1])):
                    raise ApiError("В архиве обнаружены неподдерживаемые файлы.")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("app") != "furniture-catalog" or manifest.get("version") != 1:
                raise ApiError("Это не резервная копия каталога мебели.")
            database = archive.read("catalog.sqlite3")
            with sqlite3.connect(":memory:") as validation:
                validation.deserialize(database)
                if validation.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ApiError("База данных в архиве повреждена.")
                required = {"products", "photos", "profiles", "imports", "metadata", "aliases"}
                actual = {r[0] for r in validation.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not required.issubset(actual):
                    raise ApiError("Структура базы в архиве не подходит.")
                for table, columns in {
                    "products": {"id","identity_key",*TEXT_FIELDS,"base_price","source_file","source_sheet","source_row","imported_at","source_values","overrides","search_text"},
                    "photos": {"id","product_id","filename","original_name","is_primary","created_at"},
                    "metadata": {"key","value"}, "aliases": {"identity_key","product_id"},
                    "profiles": {"family","name","factory","config","updated_at"},
                    "imports": {"id","created_at","mode","files","added","updated","duplicates","issues","details"}
                }.items():
                    actual_columns = {r[1] for r in validation.execute("PRAGMA table_info("+table+")")}
                    if not columns.issubset(actual_columns):
                        raise ApiError("Структура таблиц в архиве не подходит.")
                if validation.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ApiError("Связи товаров и фотографий в архиве повреждены.")
                for filename, in validation.execute("SELECT filename FROM photos"):
                    if not re.fullmatch(r"[a-f0-9]{32}\.(jpg|png|webp)", filename) or "photos/"+filename not in seen:
                        raise ApiError("В архиве отсутствует фотография товара.")
                value = validation.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()
                restored_settings = json.loads(value[0]) if value else None
                if not isinstance(restored_settings, dict) or not set(DEFAULTS).issubset(restored_settings):
                    raise ApiError("В архиве отсутствуют настройки каталога.")
                for key, upper in (("markup_percent",1000), ("fixed_add",1000000)):
                    price_setting = restored_settings[key]
                    if not isinstance(price_setting,(int,float)) or not math.isfinite(price_setting) or not 0 <= price_setting <= upper:
                        raise ApiError("Настройки цены в архиве некорректны.")
                if restored_settings["rounding"] not in ("none","ruble","ten","hundred"):
                    raise ApiError("Правило округления в архиве некорректно.")
                if validation.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()[0]:
                    raise ApiError("Структура базы в архиве не подходит.")
        except ApiError:
            raise
        except Exception:
            raise ApiError("Не удалось прочитать резервную копию. Выберите ZIP-архив этого приложения.")
        with self.lock:
            # Save the current database and photos before replacing it.
            backups = self.data_dir / "backups"
            backups.mkdir(exist_ok=True)
            safety = backups / ("before-restore-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6] + ".zip")
            safety.write_bytes(self.backup())
            staged = self.data_dir / ("restore-" + uuid.uuid4().hex + ".sqlite3")
            staged.write_bytes(database)
            for member in members:
                if member.filename.startswith("photos/") and not member.is_dir():
                    target = self.photos_dir / PurePosixPath(member.filename).name
                    target.write_bytes(archive.read(member))
            with self.connection() as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            for suffix in ("-wal", "-shm"):
                Path(str(self.db_path)+suffix).unlink(missing_ok=True)
            os.replace(staged, self.db_path)
            self.sessions.clear()
            self.avito.import_sessions.clear()
            with self.connection() as db:
                folder = self.meta(db, "folder") or {}
                folder.update({"watch": False, "status": "idle", "pending_session": None, "error": None})
                self.set_meta(db, "folder", folder)
                self.bump(db)
        return {"ok": True, "safety_backup": str(safety)}

    def folder(self, payload):
        with self.lock, self.connection() as db:
            value = self.meta(db, "folder")
            if "path" in payload:
                path = str(payload["path"] or "").strip()
                if path and not Path(path).expanduser().is_dir():
                    raise ApiError("Папка не найдена. Укажите путь к папке с прайсами.")
                changed = path != value.get("path")
                value["path"] = str(Path(path).expanduser().resolve()) if path else ""
                if changed:
                    self.set_meta(db, "folder_hashes", {})
                    value["pending_session"] = None
            value["mode"] = payload.get("mode", value.get("mode", "add"))
            if value["mode"] not in ("add", "update"):
                raise ApiError("Неизвестный режим обновления папки.")
            value["watch"] = bool(payload.get("watch", value.get("watch"))) and bool(value["path"])
            value["error"] = None
            value["status"] = "watching" if value["watch"] else "idle"
            self.set_meta(db, "folder", value)
            return value

    def scan_folder(self):
        with self.lock, self.connection() as db:
            folder = self.meta(db, "folder")
            if not folder.get("path"):
                raise ApiError("Сначала выберите папку с прайсами.")
        result = self.inspect({"folder": folder["path"]})
        with self.lock, self.connection() as db:
            folder = self.meta(db, "folder")
            folder.update({"last_scan": now(), "status": "review", "pending_session": result["session_id"], "error": None})
            self.set_meta(db, "folder", folder)
        return result

    def watch_loop(self):
        while not self.stop_event.wait(30):
            try:
                with self.lock, self.connection() as db:
                    folder = self.meta(db, "folder")
                    seen = self.meta(db, "folder_hashes") or {}
                if not folder.get("watch") or not folder.get("path") or folder.get("status") == "review":
                    continue
                paths, hashes = [], {}
                for path in sorted(Path(folder["path"]).iterdir()):
                    if path.is_file() and path.suffix.lower() in (".xls", ".xlsx") and not path.name.startswith("~$"):
                        stat = path.stat()
                        marker = [stat.st_mtime_ns, stat.st_size]
                        hashes[str(path)] = marker
                        if seen.get(str(path)) != marker:
                            paths.append(str(path))
                if not paths:
                    continue
                result = self.inspect({"paths": paths})
                preview = self.preview({"session_id": result["session_id"], "mode": folder.get("mode", "add")})
                if preview["counts"]["issues"] or not all(config.get("profile_used") for config in result["files"]):
                    folder.update({"status": "review", "pending_session": result["session_id"],
                                   "last_scan": now(), "error": "Проверьте новые прайсы и настройки исходной цены перед импортом."})
                else:
                    self.commit(result["session_id"])
                    folder.update({"status": "watching", "last_scan": now(), "pending_session": None, "error": None})
                    with self.lock, self.connection() as db:
                        self.set_meta(db, "folder_hashes", hashes)
                with self.lock, self.connection() as db:
                    self.set_meta(db, "folder", folder)
            except Exception as error:
                with self.lock, self.connection() as db:
                    folder = self.meta(db, "folder") or {}
                    folder.update({"error": str(error), "last_scan": now()})
                    self.set_meta(db, "folder", folder)

class Handler(BaseHTTPRequestHandler):
    server_version = "FurnitureCatalog/" + VERSION

    @property
    def catalog(self):
        return self.server.catalog

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/") and args and str(args[1] if len(args)>1 else "") not in ("200", "201"):
            print(fmt % args, flush=True)

    def valid_local(self):
        host = self.headers.get("Host", "").lower()
        valid = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in valid:
            raise ApiError("Доступ разрешён только с этого компьютера.", 403)
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://"+value for value in valid}:
            raise ApiError("Запрос с другого сайта заблокирован.", 403)

    def send_bytes(self, data, content_type, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, data, status=200):
        self.send_bytes(compact(data).encode("utf-8"), "application/json; charset=utf-8", status)

    def body(self):
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ApiError("Ожидается JSON-запрос.", 415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > MAX_BODY:
                raise ApiError("Запрос слишком большой или пустой.", 413)
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except ApiError:
            raise
        except Exception:
            raise ApiError("Не удалось прочитать запрос.")

    def update_state(self):
        source = updater.DEFAULT_SOURCE
        try:
            source = updater.configured_source(ROOT)
        except (ValueError, OSError):
            pass
        result = {"version": VERSION, "source": source, **self.server.update_info}
        status_file = ROOT / ".runtime" / "update-status.json"
        try:
            if status_file.exists() and result.get("status") not in ("checking", "preparing", "installing"):
                result["last_result"] = json.loads(status_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
        return result

    def begin_update(self, payload, install=False):
        try:
            source = updater.validate_source(payload.get("source") or self.update_state()["source"])
        except ValueError as error:
            raise ApiError(str(error))
        if not self.server.update_lock.acquire(blocking=False):
            raise ApiError("Обновление уже выполняется. Дождитесь завершения.", 409)
        try:
            # Installation source belongs to this app, never to a product or shared database.
            if not self.server.allow_updates:
                raise ApiError("В тестовом каталоге установка обновлений отключена.", 403)
            (ROOT / "update-source.json").write_text(compact({"source": source}), encoding="utf-8")
            self.server.update_info = {"status": "preparing" if install else "checking",
                                       "message": "Загружаем и проверяем обновление…" if install else "Проверяем новую версию…"}
            def work():
                try:
                    manifest = updater.release(source, VERSION)
                    self.server.update_info = {"status": "available" if manifest["available"] else "current",
                        "latest_version": manifest["version"], "available": manifest["available"],
                        "message": ("Доступна версия " + manifest["version"]) if manifest["available"] else "Установлена актуальная версия.",
                        "notes": str(manifest.get("notes", ""))[:3000]}
                    if not install or not manifest["available"]:
                        return
                    self.server.update_info["status"] = "preparing"
                    staging = updater.prepare(ROOT, manifest)
                    arguments = [sys.executable, "-E", "-s", "-X", "utf8", str(staging / "worker.py"),
                        "--app-root", str(ROOT), "--worker", str(staging), "--parent-pid", str(os.getpid()),
                        "--data-dir", str(self.catalog.data_dir), "--port", str(self.server.server_port)]
                    with self.catalog.lock:
                        self.server.update_info = {"status": "installing", "message": "Устанавливаем обновление. Каталог перезапустится…"}
                        (ROOT / ".runtime" / "update-status.json").write_text(compact(self.server.update_info), encoding="utf-8")
                        with (ROOT / ".runtime" / "update-worker.log").open("ab") as log:
                            subprocess.Popen(arguments, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.server.shutdown()
                except Exception as error:
                    self.server.update_info = {"status": "error", "message": str(error)}
                finally:
                    self.server.update_lock.release()
            threading.Thread(target=work, name="Application update", daemon=True).start()
            return {"message": "Проверяем и устанавливаем обновление…" if install else "Проверяем обновление…"}
        except Exception:
            self.server.update_lock.release()
            raise

    def handle_avito(self, path, params):
        service = self.catalog.avito
        if self.command in ("GET", "HEAD"):
            if path == "/api/avito/state":
                return service.state()
            if path == "/api/avito/listings":
                return service.listings(params)
            if path == "/api/avito/journal":
                return service.journal()
            if re.fullmatch(r"/api/avito/listings/[a-f0-9]{32}", path):
                return service.detail(path.rsplit("/", 1)[-1], params)
            if re.fullmatch(r"/api/avito/plans/[a-f0-9]{32}", path):
                return service.plan(path.rsplit("/", 1)[-1])
        if self.command == "POST":
            if path == "/api/avito/send":
                raise ApiError("Отправка запрещена: официальный транспорт не реализован.", 403)
            payload = self.body()
            if not isinstance(payload, dict):
                raise ApiError("Ожидается объект JSON.")
            if path == "/api/avito/setup":
                return service.setup()
            if path == "/api/avito/listings":
                return service.save_listing(payload)
            if path == "/api/avito/import/inspect":
                return service.inspect_export(decode_file(payload, 30*1024*1024), clean_name(payload.get("name", "")),
                                              payload.get("mode", "add"), payload.get("observed_at"))
            if path == "/api/avito/import/commit":
                return service.commit_export(payload)
            if path == "/api/avito/demo":
                source = json.loads((ROOT / "static" / "avito-demo.json").read_text(encoding="utf-8"))
                items = [service.save_listing(item, demo=True) for item in source["records"]]
                return {"items": items, "is_demo": True}
            if path == "/api/avito/decisions":
                return service.decide(payload)
            if path == "/api/avito/prepare":
                return service.prepare(payload)
            if re.fullmatch(r"/api/avito/plans/[a-f0-9]{32}/cancel", path):
                return service.cancel(path.split("/")[4])
        raise ApiError("Операция Авито не найдена.", 404)

    def handle_request(self):
        try:
            self.valid_local()
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/avito/"):
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                return self.send_json(self.handle_avito(path, params))
            if self.command in ("GET", "HEAD"):
                if path == "/api/update":
                    return self.send_json(self.update_state())
                if path == "/api/state":
                    return self.send_json(self.catalog.state())
                if re.fullmatch(r"/api/imports/[a-f0-9]{32}/report",path):
                    return self.send_bytes(self.catalog.report(path.split("/")[3]), "text/csv; charset=utf-8", filename="import-issues.csv")
                if re.fullmatch(r"/api/import/report/[a-f0-9]{32}",path):
                    return self.send_bytes(self.catalog.report(path.rsplit("/",1)[-1],session=True), "text/csv; charset=utf-8", filename="import-preview.csv")
                if re.fullmatch(r"/api/import/session/[a-f0-9]{32}", path):
                    with self.catalog.lock:
                        session_id = path.rsplit("/",1)[-1]
                        session = self.catalog.get_session(session_id)
                        return self.send_json({"session_id": session_id, "files": session["configs"]})
                if path == "/api/products":
                    params = {k: v[0] for k,v in parse_qs(parsed.query).items()}
                    return self.send_json(self.catalog.list_products(params))
                if re.fullmatch(r"/api/products/[a-f0-9]{32}", path):
                    return self.send_json(self.catalog.product(path.rsplit("/", 1)[-1]))
                if re.fullmatch(r"/api/import/[a-f0-9]{32}/photos/[a-f0-9]{64}", path):
                    with self.catalog.lock:
                        asset = self.catalog.get_session(path.split("/")[3]).get("photo_assets", {}).get(path.rsplit("/", 1)[-1])
                        if asset is None:
                            raise ApiError("Фото предпросмотра не найдено.", 404)
                        return self.send_bytes(asset["data"], mimetypes.guess_type("image" + asset["extension"])[0] or "application/octet-stream")
                if path == "/api/backup":
                    filename = "catalog-" + datetime.now().strftime("%Y-%m-%d-%H%M") + ".zip"
                    return self.send_bytes(self.catalog.backup(), "application/zip", filename=filename)
                if path.startswith("/photos/"):
                    filename = path[len("/photos/"):]
                    if not re.fullmatch(r"[a-f0-9]{32}\.(jpg|png|webp)", filename):
                        raise ApiError("Файл не найден.", 404)
                    file = self.catalog.photos_dir / filename
                else:
                    relative = "index.html" if path == "/" else path.lstrip("/")
                    if relative.startswith("static/"):
                        relative = relative[len("static/"):]
                    file = (ROOT / "static" / relative).resolve()
                    if not file.is_relative_to((ROOT/"static").resolve()):
                        raise ApiError("Файл не найден.", 404)
                if not file.is_file():
                    raise ApiError("Страница не найдена.", 404)
                return self.send_bytes(file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")
            if self.command == "POST":
                payload = self.body()
                if path == "/api/import/inspect":
                    return self.send_json(self.catalog.inspect(payload))
                if path == "/api/import/preview":
                    return self.send_json(self.catalog.preview(payload))
                if path == "/api/import/commit":
                    return self.send_json(self.catalog.commit(payload.get("session_id")))
                if path == "/api/update/check":
                    return self.send_json(self.begin_update(payload))
                if path == "/api/update/install":
                    return self.send_json(self.begin_update(payload, install=True))
                if path == "/api/settings":
                    return self.send_json(self.catalog.settings(payload))
                if path == "/api/restore":
                    return self.send_json(self.catalog.restore(payload))
                if path == "/api/folder":
                    return self.send_json(self.catalog.folder(payload))
                if path == "/api/folder/scan":
                    return self.send_json(self.catalog.scan_folder())
                if re.fullmatch(r"/api/products/[a-f0-9]{32}/photos", path):
                    return self.send_json(self.catalog.add_photos(path.split("/")[3], payload))
            if self.command == "PATCH":
                payload = self.body()
                if re.fullmatch(r"/api/products/[a-f0-9]{32}", path):
                    return self.send_json(self.catalog.edit_product(path.rsplit("/",1)[-1], payload))
                if re.fullmatch(r"/api/photos/[a-f0-9]{32}", path) and payload.get("primary"):
                    return self.send_json(self.catalog.primary_photo(path.rsplit("/",1)[-1]))
            if self.command == "DELETE" and re.fullmatch(r"/api/photos/[a-f0-9]{32}", path):
                return self.send_json(self.catalog.delete_photo(path.rsplit("/",1)[-1]))
            raise ApiError("Метод или страница не найдены.", 404)
        except ApiError as error:
            self.send_json({"error": str(error)}, error.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            traceback.print_exc()
            self.send_json({"error": "Не удалось выполнить операцию. " + str(error)}, 500)

    do_GET = do_HEAD = do_POST = do_PATCH = do_DELETE = handle_request

def main():
    parser = argparse.ArgumentParser(description="Локальный каталог мебели")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        # Windows permits multiple listeners when SO_REUSEADDR is enabled.
        # One port must serve one catalog instance and its import sessions.
        class CatalogServer(ThreadingHTTPServer):
            allow_reuse_address = False
        server = CatalogServer(("127.0.0.1", args.port), Handler)
    except OSError:
        if args.port == 8765:
            try:
                import urllib.request
                value = json.loads(urllib.request.urlopen("http://127.0.0.1:8765/api/state", timeout=2).read())
                if "version" in value and "stats" in value:
                    if not args.no_browser:
                        webbrowser.open("http://127.0.0.1:8765/")
                    print("Каталог уже запущен: http://127.0.0.1:8765/", flush=True)
                    return
            except Exception:
                pass
        raise
    server.update_lock = threading.Lock()
    server.update_info = {"status": "idle", "message": "Проверка запускается по вашему нажатию."}
    server.allow_updates = Path(args.data_dir).resolve() == (ROOT / "data").resolve()
    server.catalog = Catalog(args.data_dir)
    url = f"http://127.0.0.1:{server.server_port}/"
    print("Local URL: " + url, flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.catalog.stop_event.set()
        server.server_close()

if __name__ == "__main__":
    main()
