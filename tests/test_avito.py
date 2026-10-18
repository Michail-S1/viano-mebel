"""Stage A regression tests: disposable catalogs, no Avito account or network."""
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import test_server as fixtures
from server import ApiError, Catalog
from backend.avito import money_cents, product_snapshot
from backend import avito_migrations


class LocalAvito(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="avito-qa-")
        self.root = Path(self.tmp.name)
        self.catalog = Catalog(self.root / "database")
        self.catalog.stop_event.set()
        self.catalog.watcher.join(timeout=2)
        self.region = self.add_product("Регион 058")
        self.other = self.add_product("Другая фабрика")
        self.avito = self.catalog.avito

    def tearDown(self):
        self.catalog.stop_event.set()
        self.catalog.watcher.join(timeout=2)
        self.tmp.cleanup()

    def add_product(self, factory, price=10005, color="Белый", size="1000×1000"):
        path = self.root / (factory + color + ".xlsx")
        fixtures.make_xlsx(path, [["ТБ-02", "Тумба 1000×1000", "Лира", color, size, price]])
        inspection = self.catalog.inspect({"paths": [str(path)]})
        for config in inspection["files"]:
            config["factory"] = factory
            for sheet in config["sheets"]:
                sheet["selected"] = True
                sheet["mapping"] = fixtures.MAPPING.copy()
        self.catalog.preview({"session_id": inspection["session_id"], "files": inspection["files"], "mode": "update"})
        self.catalog.commit(inspection["session_id"])
        return self.catalog.list_products({"factory": factory})["items"][0]

    def listing(self, factory="Регион 058", **changes):
        payload = {"avito_id": "123456789", "title": "Тумба 1000×1000", "description": "",
                   "category": "Мебель", "status": "active", "price": 15000,
                   "price_source": "Ручное наблюдение для теста", "observed_at": "2026-10-16T12:00:00+03:00",
                   "attributes": {"factory": factory, "collection": "Лира", "sku": "ТБ-02", "color": "Белый", "dimensions": "1000×1000"}}
        payload.update(changes)
        return self.avito.save_listing(payload)

    def decision_payload(self, listing, product, action="confirm"):
        detail = self.avito.detail(listing["id"], {})
        return {"listing_id": listing["id"], "listing_version": detail["listing"]["version"],
                "link_version": detail["link_version"], "product_id": product["id"],
                "product_snapshot": product_snapshot(product), "action": action,
                "reason": "Проверены фабрика, артикул, размеры и цвет"}

    def confirm(self, listing, product):
        self.avito.decide(self.decision_payload(listing, product))

    def test_migration_is_explicit_backed_up_and_idempotent(self):
        self.assertFalse(self.avito.state()["installed"])
        with self.catalog.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'avito_%'").fetchone()[0], 0)
        self.catalog.add_photos(self.region["id"], {"files": [{"name": "test.png", "content": fixtures.base64.b64encode(fixtures.PNG).decode()}]})
        before = self.catalog.list_products({})
        result = self.avito.setup()
        with zipfile.ZipFile(result["backup"]) as archive:
            self.assertTrue(any(name.startswith("photos/") for name in archive.namelist()))
            data = archive.read("catalog.sqlite3")
        with closing(sqlite3.connect(":memory:")) as db:
            db.deserialize(data)
            self.assertEqual(db.execute("SELECT count(*) FROM products").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'avito_%'").fetchone()[0], 0)
        self.assertEqual(before, self.catalog.list_products({}))
        self.assertTrue(self.avito.setup()["already_installed"])
        self.assertEqual(len(list((self.catalog.data_dir / "backups").glob("before-avito-*.zip"))), 1)

    def test_backup_failure_does_not_create_tables(self):
        with patch.object(self.catalog, "backup", side_effect=OSError("backup failed")):
            with self.assertRaises(OSError):
                self.avito.setup()
        self.assertFalse(self.avito.state()["installed"])

    def test_failed_migration_rolls_back_schema_and_keeps_backup(self):
        with patch.object(avito_migrations, "DDL", (*avito_migrations.DDL[:1], "INVALID SQL")):
            with self.assertRaises(sqlite3.Error):
                self.avito.setup()
        self.assertFalse(self.avito.state()["installed"])
        with self.catalog.connection() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='avito_listings'").fetchone())
        self.assertEqual(len(list((self.catalog.data_dir / "backups").glob("before-avito-*.zip"))), 1)

    def test_identical_name_sku_collection_cannot_link_wrong_factory(self):
        self.avito.setup()
        listing = self.listing()
        detail = self.avito.detail(listing["id"], {})
        self.assertIsNone(detail["confirmed"])
        self.assertEqual(detail["category"], "likely")
        wrong = next(c for c in detail["candidates"] if c["product"]["id"] == self.other["id"])
        self.assertTrue(wrong["check"]["conflicts"])
        with self.assertRaises(ApiError):
            self.confirm(listing, self.other)
        self.confirm(listing, self.region)
        self.assertEqual(self.avito.detail(listing["id"], {})["category"], "confirmed")

    def test_missing_factory_is_ambiguous_and_never_auto_confirms(self):
        self.avito.setup()
        listing = self.listing(attributes={"sku": "ТБ-02", "collection": "Лира"})
        detail = self.avito.detail(listing["id"], {})
        self.assertEqual(detail["category"], "multiple")
        self.assertIsNone(detail["confirmed"])
        payload = self.decision_payload(listing, self.region)
        payload["reason"] = ""
        with self.assertRaises(ApiError):
            self.avito.decide(payload)

    def test_factory_in_description_blocks_wrong_manufacturer(self):
        self.avito.setup()
        listing = self.listing(attributes={}, description="Производитель Регион 058")
        with self.assertRaises(ApiError):
            self.confirm(listing, self.other)

    def test_candidates_filter_wrong_type_and_collection_before_top_ten(self):
        self.avito.setup()
        self.catalog.edit_product(self.region["id"], {"name": "НКМ Версаль-5 Кровать", "collection": "Спальня Версаль-5"})
        self.catalog.edit_product(self.other["id"], {"name": "Комод Версаль-5", "collection": "Спальня Версаль-5"})
        listing = self.listing(title="Кровать двуспальная", description='Новая коллекция &#34;Версаль-5&#34; от фабрики.', attributes={})
        detail = self.avito.detail(listing["id"], {})
        self.assertEqual([c["product"]["id"] for c in detail["candidates"]], [self.region["id"]])
        self.assertEqual(detail["match_context"]["item_type"]["kind"], "bed")
        self.assertEqual(detail["excluded"]["type"], 1)
        self.assertIsNone(detail["confirmed"])
        # Manual text search cannot bypass type compatibility.
        self.assertEqual(self.avito.detail(listing["id"], {"q": "Комод"})["candidates"], [])
        with self.assertRaises(ApiError):
            self.confirm(listing, self.catalog.product(self.other["id"]))
        self.catalog.edit_product(self.other["id"], {"name": "Кровать", "collection": "Амалия"})
        detail = self.avito.detail(listing["id"], {})
        self.assertEqual(detail["excluded"]["collection"], 1)
        self.assertEqual([c["product"]["id"] for c in detail["candidates"]], [self.region["id"]])
        self.catalog.edit_product(self.other["id"], {"name": "Секция №42", "collection": "Спальня Версаль-5"})
        detail = self.avito.detail(listing["id"], {})
        self.assertEqual([c["product"]["id"] for c in detail["candidates"]], [self.region["id"]])
        self.assertEqual(len(self.avito.detail(listing["id"], {"q": "Секция"})["candidates"]), 1)

    def test_color_size_discrepancy_requires_manual_warning_review(self):
        self.avito.setup()
        listing = self.listing(attributes={"factory": "Регион 058", "color": "Чёрный", "dimensions": "1800×1000"})
        with self.assertRaises(ApiError):
            self.confirm(listing, self.region)
        payload = self.decision_payload(listing, self.region)
        detail = self.avito.detail(listing['id'], {'factory': 'Регион 058'})
        check = next(c['check'] for c in detail['candidates'] if c['product']['id'] == self.region['id'])
        self.assertFalse(check['conflicts'])
        self.assertTrue(any('Цвет' in warning for warning in check['warnings']))
        payload['acknowledged_warnings'] = check['warnings']
        payload['reason'] = 'Вручную проверен товар; цвет и размеры старого объявления ошибочны'
        self.avito.decide(payload)
        self.assertFalse(self.avito.detail(listing['id'], {})['confirmed']['check']['conflicts'])

    def test_reject_unlink_relink_history_and_concurrent_decisions(self):
        self.avito.setup()
        listing = self.listing(attributes={})
        old_payload = self.decision_payload(listing, self.region)
        self.confirm(listing, self.region)
        with self.assertRaises(ApiError):
            self.avito.decide(old_payload)
        self.avito.decide(self.decision_payload(listing, self.region, "unlink"))
        self.confirm(listing, self.other)
        detail = self.avito.detail(listing["id"], {})
        self.assertEqual(detail["confirmed"]["product"]["id"], self.other["id"])
        self.assertEqual([d["action"] for d in detail["history"]], ["confirm", "unlink", "confirm"])
        self.avito.decide(self.decision_payload(listing, self.region, "reject"))
        self.assertNotIn(self.region["id"], [c["product"]["id"] for c in self.avito.detail(listing["id"], {})["candidates"]])

    def test_price_uses_catalog_rounding_and_one_factory_only(self):
        self.avito.setup()
        self.catalog.settings({"markup_percent": 20, "fixed_add": 1000, "rounding": "ten"})
        region = self.catalog.product(self.region["id"])
        a = self.listing()
        b = self.listing(factory="Другая фабрика", avito_id="987654321")
        self.confirm(a, region)
        self.confirm(b, self.other)
        plan = self.avito.prepare({"factory": "Регион 058"})
        self.assertEqual(len(plan["items"]), 1)
        item = plan["items"][0]
        self.assertEqual(item["product_id"], region["id"])
        self.assertEqual(item["target_cents"], money_cents(region["sale_price"]))
        self.assertEqual(item["target_cents"], 1301000)
        self.assertEqual(item["previous_cents"], 1500000)
        self.assertEqual(item["state"], "blocked_transport")
        self.assertFalse(plan["transport_enabled"])
        self.assertEqual(self.avito.prepare({"factory": "Регион 058"})["id"], plan["id"])

    def test_settings_import_edits_and_restore_invalidate_plans(self):
        self.avito.setup()
        listing = self.listing()
        self.confirm(listing, self.region)
        old = self.avito.prepare({"factory": "Регион 058"})
        self.catalog.settings({"rounding": "ten"})
        self.assertEqual(self.avito.plan(old["id"])["state"], "stale")
        new = self.avito.prepare({"factory": "Регион 058"})
        self.add_product("Регион 058", price=20000)
        self.assertEqual(self.avito.plan(new["id"])["state"], "stale")
        fresh = self.avito.prepare({"factory": "Регион 058"})
        self.catalog.edit_product(self.region["id"], {"factory": "Другая фабрика"})
        self.assertEqual(self.avito.plan(fresh["id"])["state"], "stale")
        self.assertEqual(self.avito.detail(listing["id"], {})["category"], "conflict")
        self.assertEqual(self.avito.prepare({"factory": "Другая фабрика"})["items"][0]["state"], "conflict")

    def test_unknown_price_and_changed_observation_are_not_ready(self):
        self.avito.setup()
        listing = self.listing(price=None, observed_at=None)
        self.confirm(listing, self.region)
        self.assertEqual(self.avito.prepare({"factory": "Регион 058"})["items"][0]["state"], "price_unknown")
        self.listing(price=16000)
        self.assertEqual(self.avito.prepare({"factory": "Регион 058"})["items"][0]["state"], "price_review")
        observed = [json.loads(e["details"]) for e in self.avito.journal()["items"] if e["kind"] == "listing_observed"]
        self.assertEqual(observed[0]["price_cents"], 1600000)

    def test_invalid_catalog_price_is_unknown_not_zero(self):
        self.avito.setup()
        listing = self.listing()
        self.confirm(listing, self.region)
        with self.catalog.connection() as db:
            db.execute("UPDATE products SET base_price=NULL WHERE id=?", (self.region["id"],))
            self.catalog.bump(db)
        item = self.avito.prepare({"factory": "Регион 058"})["items"][0]
        self.assertEqual(item["state"], "conflict")
        self.assertIsNone(item["target_cents"])

    def test_restart_cancel_and_backup_restore_preserve_local_state(self):
        self.avito.setup()
        listing = self.listing()
        self.confirm(listing, self.region)
        plan = self.avito.prepare({"factory": "Регион 058"})
        backup = self.catalog.backup()
        pending_import = self.avito.inspect_export(self.export(), "sample.xlsx")
        self.catalog.stop_event.set()
        self.catalog = Catalog(self.root / "database")
        self.catalog.stop_event.set()
        self.catalog.watcher.join(timeout=2)
        self.avito = self.catalog.avito
        self.assertEqual(self.avito.detail(listing["id"], {})["category"], "confirmed")
        self.assertEqual(self.avito.plan(plan["id"])["state"], "prepared")
        self.avito.cancel(plan["id"])
        self.assertEqual(self.avito.plan(plan["id"])["state"], "cancelled")
        self.catalog.restore({"content": fixtures.base64.b64encode(backup).decode()})
        self.assertEqual(self.avito.plan(plan["id"])["state"], "stale")
        self.assertFalse(self.avito.import_sessions)

    def test_input_validation_and_demo_namespace(self):
        self.avito.setup()
        for value in (float("nan"), -1, True, "12.345"):
            with self.assertRaises(ApiError):
                self.listing(price=value)
        with self.assertRaises(ApiError):
            self.listing(url="https://attacker.example/avito.ru")
        with self.assertRaises(ApiError):
            self.listing(observed_at="2026-10-16T12:00:00")
        with self.assertRaises(ApiError):
            self.avito.save_listing({"avito_id": "DEMO-1"})
        payload = {"avito_id": "DEMO-1", "title": "Тумба", "price_source": "ТЕСТ", "attributes": {}}
        item = self.avito.save_listing(payload, demo=True)
        self.assertTrue(item["is_demo"])
        self.confirm(item, self.region)
        plan = self.avito.prepare({"factory": "Регион 058"})
        self.assertTrue(plan["is_demo"])
        self.assertIn("ТЕСТОВОЕ", plan["items"][0]["reasons"][0])

    def export(self, rows=None):
        headers = ["Уникальный идентификатор объявления", "Номер объявления на Авито", "Название объявления",
                   "Описание объявления", "Категория", "Поместится ли товар в одну коробку?", "Цена",
                   "Ссылки на фото", "Ширина", "Высота", "Глубина", "AvitoStatus", "Номер телефона"]
        rows = rows or [["external-key", "123456789", "Тумба 1000×1000", "<p>Фабрика не указана</p>",
                         "Мебель и интерьер", "Да", 15000, "http://avito.ru/image-1 | http://avito.ru/image-2", 100, 100, 50, "Активно", "PRIVATE-PHONE"],
                        ["external-job", "987654321", "Вакансия", "Описание", "Вакансии", "", 10000]]
        path = self.root / "export.xlsx"
        fixtures.make_xlsx(path, [headers, ["Необязательно"]*len(headers), ["Заполнить в профиле"]*len(headers), *rows])
        return path.read_bytes()

    def test_export_preview_id_mapping_categories_and_safe_reimport(self):
        self.avito.setup()
        source = self.export()
        preview = self.avito.inspect_export(source, "sample.xlsx")
        self.assertEqual(preview["total"], 2)
        self.assertEqual(preview["issues_count"], 0)
        self.assertEqual(preview["sample"][0]["avito_id"], "123456789")
        self.assertEqual(preview["sample"][0]["price"], 15000)
        self.assertEqual(self.avito.state()["listings"], 0)
        request = {"session_id": preview["session_id"], "categories": ["Мебель и интерьер"]}
        with self.assertRaises(ApiError):
            self.avito.commit_export({**request, "categories": ["Вакансии"]})
        result = self.avito.commit_export(request)
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.avito.commit_export(request), result)
        row = self.avito.listings({})["items"][0]
        self.assertEqual(row["source_details"]["export_id"], "external-key")
        self.assertEqual(len(row["source_details"]["image_urls"]), 2)
        self.assertNotIn("Номер телефона", row["source_details"]["characteristics"])
        self.assertIsNone(row["observed_at"])
        self.assertNotIn("dimensions", row["attributes"])
        self.assertEqual(row["source_details"]["characteristics"]["Ширина"], "100")
        self.assertEqual(row["description"], "<p>Фабрика не указана</p>")
        second = self.avito.inspect_export(source, "sample.xlsx")
        self.assertEqual(self.avito.commit_export({"session_id": second["session_id"], "categories": ["Мебель и интерьер"]})["skipped"], 1)
        self.assertEqual(self.avito.state()["listings"], 1)

    def test_export_conflicting_duplicates_and_missing_avito_id(self):
        self.avito.setup()
        rows = [["external-1", "123456789", "Тумба", "", "Мебель и интерьер", "Да", 10000],
                ["external-1", "123456789", "Тумба", "", "Мебель и интерьер", "Да", 20000],
                ["source-id-is-not-avito-id", "", "Другая тумба", "", "Мебель и интерьер", "Да", 5000]]
        preview = self.avito.inspect_export(self.export(rows), "sample.xlsx")
        self.assertEqual(preview["issues_count"], 2)
        result = self.avito.commit_export({"session_id": preview["session_id"], "categories": ["Мебель и интерьер"]})
        self.assertEqual(result["added"], 0)
        self.assertEqual(self.avito.state()["listings"], 0)

    def test_export_never_overwrites_newer_manual_price_and_stale_preview(self):
        self.avito.setup()
        listing = self.listing()
        source = self.export()
        preview = self.avito.inspect_export(source, "sample.xlsx", mode="update")
        self.assertEqual(preview["sample"][0]["import_state"], "conflict")
        self.avito.commit_export({"session_id": preview["session_id"], "categories": ["Мебель и интерьер"]})
        self.assertEqual(self.avito.listings({})["items"][0]["price_source"], listing["price_source"])
        preview = self.avito.inspect_export(source, "sample.xlsx", mode="update", observed_at="2026-10-17T00:00:00+03:00")
        self.listing(price=16000)
        with self.assertRaises(ApiError):
            self.avito.commit_export({"session_id": preview["session_id"], "categories": ["Мебель и интерьер"]})

    def test_export_commit_rolls_back_all_rows_on_late_validation_error(self):
        self.avito.setup()
        rows = [["one", "123456789", "Тумба", "", "Мебель и интерьер", "Да", 10000],
                ["two", "987654321", "Тумба", "x"*30001, "Мебель и интерьер", "Да", 10000]]
        preview = self.avito.inspect_export(self.export(rows), "sample.xlsx")
        with self.assertRaises(ApiError):
            self.avito.commit_export({"session_id": preview["session_id"], "categories": ["Мебель и интерьер"]})
        self.assertEqual(self.avito.state()["listings"], 0)

    def test_v1_to_v2_migration_has_separate_backup_and_preserves_catalog(self):
        with patch.object(avito_migrations, "VERSION", 1):
            self.avito.setup()
        with self.catalog.connection() as db:
            self.assertNotIn("source_details", [r[1] for r in db.execute("PRAGMA table_info(avito_listings)")])
        self.avito.setup()
        self.assertTrue(self.avito.state()["installed"])
        with self.catalog.connection() as db:
            self.assertIn("source_details", [r[1] for r in db.execute("PRAGMA table_info(avito_listings)")])
        self.assertEqual(self.catalog.list_products({})["total"], 2)
        self.assertEqual(len(list((self.catalog.data_dir / "backups").glob("before-avito-*.zip"))), 2)


class AvitoHTTP(unittest.TestCase):
    def setUp(self):
        self.client = fixtures.CatalogAPI(methodName="runTest")
        self.client.setUp()

    def tearDown(self):
        self.client.tearDown()

    def test_end_to_end_demo_and_transport_block(self):
        c = self.client
        self.assertFalse(c.request("GET", "/api/avito/state")["installed"])
        c.request("POST", "/api/avito/setup", {})
        c.commit([["ТБ-02", "Тумба 1000×1000", "Лира", "Белый", "1000×1000", 10000]], factory="Регион 058")
        result = c.request("POST", "/api/avito/demo", {})
        self.assertTrue(all(i["is_demo"] for i in result["items"]))
        rows = c.request("GET", "/api/avito/listings?factory=" + fixtures.urllib.parse.quote("Регион 058"))["items"]
        self.assertEqual(len(rows), 2)  # An explicitly different factory is no longer included.
        listing = next(r for r in rows if r["avito_id"] == "DEMO-058-1")
        detail = c.request("GET", "/api/avito/listings/" + listing["id"])
        product = detail["candidates"][0]["product"]
        c.request("POST", "/api/avito/decisions", {"listing_id": listing["id"], "listing_version": listing["version"],
                  "product_id": product["id"], "product_snapshot": product_snapshot(product), "action": "confirm", "reason": "Проверка теста"})
        plan = c.request("POST", "/api/avito/prepare", {"factory": "Регион 058"})
        self.assertEqual(plan["items"][0]["state"], "unchanged")
        self.assertTrue(c.request("GET", "/api/avito/journal")["items"])
        c.request("POST", "/api/avito/plans/" + plan["id"] + "/cancel", {})
        c.expect_http_error("POST", "/api/avito/send", {}, statuses=(403,)).close()
        c.expect_http_error("POST", "/api/avito/setup", {}, headers={"Origin": "https://foreign.example"}, statuses=(403,)).close()
        self.assertIn(b"navAvito", c.request("GET", "/", raw=True))
        self.assertIn(b"AvitoUI", c.request("GET", "/static/avito.js", raw=True))
