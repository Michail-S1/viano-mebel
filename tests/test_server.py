"""Integration checks for the local catalog, using disposable data only.

Run: python -m unittest discover -s tests -v
The OOXML fixtures are hand-written ZIP/XML and do not use authoring libraries.
"""
from __future__ import annotations

import base64
import copy
import csv
import io
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
HEADERS = ["Артикул", "Наименование", "Коллекция", "Цвет", "Размеры", "Цена"]
MAPPING = dict(zip(["sku", "name", "collection", "color", "dimensions", "price"], "ABCDEF"))


def make_xlsx(path: Path, rows: list[list], sheet: str = "Прайс") -> None:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    xml_rows = []
    for row_no, row in enumerate([HEADERS] + rows, 1):
        cells = []
        for col_no, value in enumerate(row):
            ref = f"{chr(65 + col_no)}{row_no}"
            if value is None:
                continue
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                text = escape(str(value))
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
        xml_rows.append(f'<row r="{row_no}">{"".join(cells)}</row>')
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{escape(sheet)}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{ns}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>',
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


class CatalogAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="catalog-qa-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "database"
        self.counter = 0
        self.proc = None
        self.start_server()

    def tearDown(self):
        self.stop_server()
        self.tmp.cleanup()

    def start_server(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = (self.root / "server.log").open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py"), "--data-dir", str(self.data),
             "--port", str(self.port), "--no-browser"],
            cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            try:
                self.request("GET", "/api/state")
                return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.1)
        self.stop_server()
        self.fail("Server did not become ready: " + (self.root / "server.log").read_text(encoding="utf-8", errors="replace"))

    def stop_server(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if hasattr(self, "log") and not self.log.closed:
            self.log.close()

    def request(self, method: str, path: str, payload=None, headers=None, raw=False):
        supplied_headers = dict(headers or {})
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            supplied_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=supplied_headers, method=method)
        with urllib.request.urlopen(request, timeout=10) as response:
            result = response.read()
            return result if raw else json.loads(result.decode("utf-8"))

    def expect_http_error(self, method, path, payload=None, headers=None, statuses=(400, 403, 409, 422)):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(method, path, payload, headers)
        self.assertIn(caught.exception.code, statuses)
        return caught.exception

    def workbook(self, rows, name=None):
        self.counter += 1
        path = self.root / (name or f"Прайс {self.counter}.xlsx")
        make_xlsx(path, rows)
        return path

    def preview(self, rows, mode="add", name=None, factory="Фабрика Тест"):
        path = self.workbook(rows, name)
        inspected = self.request("POST", "/api/import/inspect", {"paths": [str(path)]})
        files = copy.deepcopy(inspected["files"])
        for config in files:
            config["factory"] = factory
            for sheet in config["sheets"]:
                sheet["selected"] = True
                sheet["mapping"] = MAPPING.copy()
        result = self.request("POST", "/api/import/preview", {
            "session_id": inspected["session_id"], "mode": mode, "files": files,
        })
        return inspected["session_id"], result

    def commit(self, rows, mode="add", name=None, factory="Фабрика Тест"):
        session, preview = self.preview(rows, mode, name, factory)
        result = self.request("POST", "/api/import/commit", {"session_id": session})
        return preview, result

    def products(self, query=""):
        result = self.request("GET", "/api/products?page_size=120" + query)
        return result["items"]

    def photo(self, product_id, name="photo.png"):
        return self.request("POST", f"/api/products/{product_id}/photos", {
            "files": [{"name": name, "content": base64.b64encode(PNG).decode("ascii")}],
        })

    def test_second_listener_cannot_take_over_a_running_catalog(self):
        second_data=self.root/"second-data"
        result=subprocess.run([sys.executable,"-X","utf8",str(ROOT/"server.py"),
            "--port",str(self.port),"--data-dir",str(second_data),"--no-browser"],
            cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(second_data.exists())
        self.assertEqual(self.request("GET","/api/state")["stats"]["total"],0)

    def test_repeat_import_and_add_preserve_original_price(self):
        rows = [["A1", "Комод", "Лира", "Белый", "800×400×900 мм", 10000]]
        self.commit(rows)
        original = self.products()[0]
        self.assertEqual(original["base_price"], 10000)
        self.assertEqual(original["sale_price"], 13000)
        self.commit(rows)
        self.commit([["A1", "Комод", "Лира", "Белый", "800×400×900 мм", 15000]])
        items = self.products()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], original["id"])
        self.assertEqual(items[0]["base_price"], 10000)
        self.assertEqual(items[0]["sale_price"], 13000)

    def test_update_does_not_compound_markup(self):
        row = ["A1", "Комод", "Лира", "Белый", "800×400×900 мм", 10000]
        self.commit([row])
        row[-1] = 12000
        self.commit([row], "update")
        self.commit([row], "update")
        item = self.products()[0]
        self.assertEqual(item["base_price"], 12000)
        self.assertEqual(item["sale_price"], 15400)

    def test_color_and_size_variants_are_distinct(self):
        self.commit([
            ["A1", "Комод", "Лира", "Белый", "800×400×900 мм", 10000],
            ["A1", "Комод", "Лира", "Дуб", "800×400×900 мм", 11000],
            ["A1", "Комод", "Лира", "Белый", "1000×400×900 мм", 12000],
        ])
        items = self.products()
        self.assertEqual(len(items), 3)
        self.assertEqual(len({item["id"] for item in items}), 3)

    def test_new_collection_and_factory_can_reuse_sku(self):
        row = ["B600", "Шкаф навесной", "Альфа", "Белый", "600×300×700 мм", 10000]
        self.commit([row])
        row[2] = "Бета"
        self.commit([row])
        self.commit([row], factory="Другая фабрика")
        items = self.products()
        self.assertEqual(len(items), 3)
        self.assertEqual(len({item["id"] for item in items}), 3)

    def test_invalid_image_upload_does_not_create_photo(self):
        self.commit([["A1", "Комод", "Лира", "Белый", "", 10000]])
        item = self.products()[0]
        self.expect_http_error("POST", f'/api/products/{item["id"]}/photos', {
            "files": [{"name": "photo.png", "content": base64.b64encode(b"not an image").decode("ascii")}],
        })
        self.assertEqual(self.products()[0]["photos"], [])

    def test_conflicting_duplicate_prices_are_not_silently_chosen(self):
        rows = [
            ["A1", "Комод", "Лира", "Белый", "800×400×900 мм", 10000],
            ["A1", "Комод", "Лира", "Белый", "800×400×900 мм", 12000],
        ]
        session, result = self.preview(rows)
        self.assertTrue(result["counts"]["issues"], result)
        self.request("POST", "/api/import/commit", {"session_id": session})
        self.assertEqual(len(self.products()), 0, "Conflicting rows must be excluded until resolved")

    def test_conflicting_names_for_same_sku_in_batch_are_reviewed(self):
        session, preview = self.preview([
            ["A1", "Комод", "Лира", "Белый", "800×400×900 мм", 10000],
            ["A1", "Шкаф", "Лира", "Белый", "800×400×900 мм", 10000],
        ])
        self.assertGreater(preview["counts"]["issues"], 0)
        self.request("POST", "/api/import/commit", {"session_id": session})
        self.assertEqual(self.products(), [])

    def test_pending_folder_scan_commits_and_clears_review_state(self):
        self.workbook([["A1", "Комод", "Лира", "Белый", "", 10000]])
        self.request("POST", "/api/folder", {"path": str(self.root), "watch": False, "mode": "add"})
        scanned = self.request("POST", "/api/folder/scan", {})
        folder = self.request("GET", "/api/state")["folder"]
        self.assertEqual(folder["status"], "review")
        self.assertEqual(folder["pending_session"], scanned["session_id"])
        self.assertEqual(self.products(), [])
        session = self.request("GET", "/api/import/session/" + scanned["session_id"])
        for config in session["files"]:
            config["factory"] = "Фабрика Тест"
            for sheet in config["sheets"]:
                sheet["selected"] = True
                sheet["mapping"] = MAPPING.copy()
        self.request("POST", "/api/import/preview", {"session_id": scanned["session_id"], "mode": "add", "files": session["files"]})
        self.request("POST", "/api/import/commit", {"session_id": scanned["session_id"]})
        self.assertEqual(len(self.products()), 1)
        folder = self.request("GET", "/api/state")["folder"]
        self.assertIsNone(folder["pending_session"])
        self.assertEqual(folder["status"], "idle")

    def test_commit_is_idempotent_and_stale_preview_is_rejected(self):
        session, _ = self.preview([["A1", "Комод", "Лира", "Белый", "", 10000]])
        first = self.request("POST", "/api/import/commit", {"session_id": session})
        second = self.request("POST", "/api/import/commit", {"session_id": session})
        self.assertEqual(first, second)
        self.assertEqual(len(self.products()), 1)
        stale_session, _ = self.preview([["A2", "Шкаф", "Лира", "Дуб", "", 20000]])
        self.request("POST", "/api/settings", {"markup_percent": 30})
        self.expect_http_error("POST", "/api/import/commit", {"session_id": stale_session}, statuses=(409,))
        self.assertEqual(len(self.products()), 1)

    def test_static_styles_and_root_are_served(self):
        page = self.request("GET", "/", raw=True)
        self.assertIn(b'lang="ru"', page)
        stylesheet = self.request("GET", "/static/styles.css", raw=True)
        self.assertIn(b".app-shell", stylesheet)
        self.expect_http_error("GET", "/static/../server.py", statuses=(404,))

    def test_missing_prices_are_issues_not_1000_rubles(self):
        session, result = self.preview([
            ["A1", "Комод", "Лира", "Белый", "800×400×900 мм", None],
            ["A2", "Шкаф", "Лира", "Дуб", "", "по запросу"],
        ])
        self.assertEqual(result["counts"]["issues"], 2, "Each invalid price row should be counted once")
        self.request("POST", "/api/import/commit", {"session_id": session})
        for product in self.products():
            self.assertIsNone(product["base_price"])
            self.assertIsNone(product["sale_price"])

    def test_renamed_sku_is_reviewed_and_not_duplicated(self):
        row = ["A1", "Комод", "Лира", "Белый", "800×400×900 мм", 10000]
        self.commit([row])
        row[1] = "Новый комод"
        session, preview = self.preview([row], "update")
        self.assertTrue(preview["counts"]["issues"], preview)
        self.request("POST", "/api/import/commit", {"session_id": session})
        self.assertEqual(len(self.products()), 1)
        self.assertEqual(self.products()[0]["name"], "Комод")

    def test_manual_dimensions_photo_identity_survive_update_and_restart(self):
        row = ["", "Комод", "Лира", "Белый", "800×400×900 мм", 10000]
        self.commit([row])
        original = self.products()[0]
        self.request("PATCH", f'/api/products/{original["id"]}', {"dimensions": "805×405×905 мм"})
        self.photo(original["id"])
        row[-1] = 12500
        self.commit([row], "update")
        items = self.products()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], original["id"])
        self.assertEqual(items[0]["dimensions"], "805×405×905 мм")
        self.assertEqual(items[0]["base_price"], 12500)
        self.assertEqual(items[0]["sale_price"], 16000)
        self.assertEqual(len(items[0]["photos"]), 1)
        photo_url = items[0]["photos"][0]["url"]
        served_photo = self.request("GET", photo_url, raw=True)
        self.assertTrue(served_photo.startswith((b"\xff\xd8\xff", b"\x89PNG")))
        self.stop_server()
        self.start_server()
        items = self.products()
        self.assertEqual(items[0]["id"], original["id"])
        self.assertEqual(items[0]["dimensions"], "805×405×905 мм")
        self.assertEqual(len(items[0]["photos"]), 1)
        self.assertEqual(self.request("GET", items[0]["photos"][0]["url"], raw=True), served_photo)

    def test_photo_primary_and_delete(self):
        self.commit([["A1", "Комод", "Лира", "Белый", "", 10000]])
        product = self.products()[0]
        self.photo(product["id"], "one.png")
        self.photo(product["id"], "two.png")
        photos = self.products()[0]["photos"]
        self.assertEqual(len(photos), 2)
        target_id = photos[1]["id"]
        self.request("PATCH", f'/api/photos/{target_id}', {"primary": True})
        photos = self.products()[0]["photos"]
        self.assertEqual(sum(bool(photo["is_primary"]) for photo in photos), 1)
        self.assertTrue(next(photo for photo in photos if photo["id"] == target_id)["is_primary"])
        self.request("DELETE", f'/api/photos/{target_id}')
        remaining = self.products()[0]["photos"]
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0]["is_primary"])

    def test_backup_restore_preserves_database_and_photos(self):
        self.commit([["A1", "Комод", "Лира", "Белый", "", 10000]])
        product_id = self.products()[0]["id"]
        self.photo(product_id)
        served_photo = self.request("GET", self.products()[0]["photos"][0]["url"], raw=True)
        archive = self.request("GET", "/api/backup", raw=True)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            self.assertTrue(zipped.namelist())
        self.commit([["A2", "Шкаф", "Лира", "Дуб", "", 20000]])
        self.assertEqual(len(self.products()), 2)
        self.request("POST", "/api/restore", {"name": "backup.zip", "content": base64.b64encode(archive).decode("ascii")})
        products = self.products()
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["id"], product_id)
        self.assertEqual(self.request("GET", products[0]["photos"][0]["url"], raw=True), served_photo)

    def test_restore_rejects_traversal_and_invalid_archives_without_damage(self):
        self.commit([["A1", "Комод", "Лира", "Белый", "", 10000]])
        before = self.products()
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as zipped:
            zipped.writestr("../escaped.txt", "must not escape")
        for content in [bad.getvalue(), b"not a zip"]:
            self.expect_http_error("POST", "/api/restore", {
                "name": "bad.zip", "content": base64.b64encode(content).decode("ascii"),
            })
            self.assertEqual(self.products(), before)
            self.assertFalse((self.root / "escaped.txt").exists())

    def test_settings_validation_and_recalculation(self):
        self.commit([["A1", "Комод", "Лира", "Белый", "", 10000]])
        for payload in [{"markup_percent": -10}, {"fixed_add": -1}, {"rounding": "invalid"}, {"markup_percent": "nan"}]:
            self.expect_http_error("POST", "/api/settings", payload)
        self.request("POST", "/api/settings", {"markup_percent": 25, "fixed_add": 500, "rounding": "hundred"})
        self.assertEqual(self.products()[0]["sale_price"], 13000)
        self.assertEqual(self.products()[0]["base_price"], 10000)

    def test_search_casefold_pagination_and_no_deletion_on_partial_price_list(self):
        self.commit([
            ["A1", "Комод Лира", "Лира", "Белый", "", 10000],
            ["A2", "Шкаф Лира", "Лира", "Дуб", "", 20000],
        ])
        result = self.request("GET", "/api/products?q=" + urllib.parse.quote("КОМОД"))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["sku"], "A1")
        result = self.request("GET", "/api/products?page_size=1&page=1")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["total"], 2)
        self.commit([["A1", "Комод Лира", "Лира", "Белый", "", 15000]], "update")
        self.assertEqual(len(self.products()), 2)

    def test_short_designations_match_whole_words_case_insensitively(self):
        names = ["КР", "КР-01", "(кР)", "КРП", "АКР", "КР01", "Кровать",
                 "Тумба прикроватная", "КП-554", "КПТ-215", "ШК-5", "Шкаф", "МСП-1"]
        self.commit([[f"A{i}", name, "Лира", "", "", 1000] for i,name in enumerate(names)])
        for query, expected in [("кр", {"КР", "КР-01", "(кР)"}),
                                ("КР", {"КР", "КР-01", "(кР)"}),
                                ("кР", {"КР", "КР-01", "(кР)"}),
                                ("кп", {"КП-554"}), ("Шк", {"ШК-5"}),
                                ("мсп", {"МСП-1"}), ("мс", set())]:
            with self.subTest(query=query):
                result = self.request("GET", "/api/products?q=" + urllib.parse.quote(query))
                self.assertEqual({p["name"] for p in result["items"]}, expected)
                self.assertEqual(result["total"], len(expected))
        # Longer terms retain substring matching.
        result = self.request("GET", "/api/products?q=" + urllib.parse.quote("кроват"))
        self.assertEqual({p["name"] for p in result["items"]}, {"Кровать", "Тумба прикроватная"})

    def test_short_designations_work_in_mixed_queries_filters_pages_and_edits(self):
        self.commit([
            ["A1", "КР-01", "Лира", "Белый", "800×400×900 мм", 1000],
            ["A2", "КР-02", "Лира", "Дуб", "800×400×900 мм", 2000],
            ["A3", "КРП", "Лира", "Белый", "800×400×900 мм", 1000],
            ["A4", "Кровать", "Лира", "Белый", "800×400×900 мм", 1000],
            ["КР", "Пенал", "Другая", "", "", 1000],
        ])
        self.commit([["B1", "КР-03", "Лира", "", "800×400×900 мм", 1500]], factory="Другая фабрика")
        params = {"q":"кр лиРА", "factory":"Фабрика Тест", "dimensions":"800",
                  "min":"2100", "max":"3500", "page_size":1, "sort":"price_asc"}
        first = self.request("GET", "/api/products?" + urllib.parse.urlencode(params))
        self.assertEqual((first["total"], first["pages"]), (2,2))
        self.assertEqual(first["items"][0]["name"], "КР-01")
        params["page"] = 2
        second = self.request("GET", "/api/products?" + urllib.parse.urlencode(params))
        self.assertEqual(second["items"][0]["name"], "КР-02")
        identifier = first["items"][0]["id"]
        self.request("PATCH", "/api/products/" + identifier, {"name":"ШК-01"})
        remaining = self.request("GET", "/api/products?" + urllib.parse.urlencode(params))
        self.assertEqual(remaining["total"],1)
        # A separately stored SKU is also searched by whole word.
        result = self.request("GET", "/api/products?" + urllib.parse.urlencode({"q":"кр пенал"}))
        self.assertEqual([p["sku"] for p in result["items"]], ["КР"])

    def test_derived_color_keeps_raw_identity_photos_after_import_restart_restore(self):
        rows = [
            ["BLUE", "Фасад Блю", "Лира", "", "", 10000],
            ["VISION", "Фасад Вижн", "Лира", "", "", 11000],
            ["ROSE", "Кухня Роза 2м", "Роза", "", "", 12000],
        ]
        session, preview = self.preview(rows)
        shown = {item.get("sku"): item.get("color") for item in preview["items"] if item["status"] == "new"}
        self.assertEqual(shown, {"BLUE": "Блю", "VISION": "Вижн", "ROSE": ""})
        self.request("POST", "/api/import/commit", {"session_id": session})
        products = {item["sku"]: item for item in self.products()}
        original_ids = {sku: item["id"] for sku, item in products.items()}
        self.assertEqual(products["BLUE"]["color"], "Блю")
        self.assertEqual(products["VISION"]["color"], "Вижн")
        self.assertEqual(products["ROSE"]["color"], "")
        self.photo(original_ids["BLUE"])
        original_photo = next(item for item in self.products() if item["sku"] == "BLUE")["photos"][0]
        photo_bytes = self.request("GET", original_photo["url"], raw=True)
        self.commit(rows)
        rows[0][-1] = 12000
        self.commit(rows, "update")
        products = {item["sku"]: item for item in self.products()}
        self.assertEqual({sku: item["id"] for sku, item in products.items()}, original_ids)
        self.assertEqual(products["BLUE"]["sale_price"], 15400)
        self.assertEqual(products["BLUE"]["color"], "Блю")
        self.assertEqual(products["BLUE"]["photos"][0]["id"], original_photo["id"])
        database = sqlite3.connect(self.data / "catalog.sqlite3")
        try:
            raw = list(database.execute("SELECT color,source_values FROM products"))
        finally:
            database.close()
        self.assertTrue(all(color == "" and json.loads(source)["color"] == "" for color, source in raw),
                        "Derived display colors must not become source fields or identity inputs")
        self.stop_server()
        self.start_server()
        products = {item["sku"]: item for item in self.products()}
        self.assertEqual(products["BLUE"]["color"], "Блю")
        self.assertEqual(products["VISION"]["color"], "Вижн")
        self.assertEqual({sku: item["id"] for sku, item in products.items()}, original_ids)
        archive = self.request("GET", "/api/backup", raw=True)
        self.commit([["EXTRA", "Новая тумба", "Лира", "", "", 15000]])
        self.request("POST", "/api/restore", {"name": "colors.zip", "content": base64.b64encode(archive).decode("ascii")})
        products = {item["sku"]: item for item in self.products()}
        self.assertEqual({sku: item["id"] for sku, item in products.items()}, original_ids)
        self.assertEqual(products["BLUE"]["color"], "Блю")
        self.assertEqual(products["VISION"]["color"], "Вижн")
        self.assertEqual(products["ROSE"]["color"], "")
        self.assertEqual(products["BLUE"]["photos"][0]["id"], original_photo["id"])
        self.assertEqual(self.request("GET", products["BLUE"]["photos"][0]["url"], raw=True), photo_bytes)

    def test_explicit_and_manual_blank_colors_survive_derived_display_updates(self):
        rows = [
            ["EXPLICIT", "Кухня (Белый, Графит)", "Лира", "Gray", "", 10000],
            ["MANUAL", "Фасад Блю", "Лира", "", "", 11000],
        ]
        self.commit(rows)
        products = {item["sku"]: item for item in self.products()}
        original_ids = {sku: item["id"] for sku, item in products.items()}
        self.assertEqual(products["EXPLICIT"]["color"], "Gray")
        self.assertEqual(products["MANUAL"]["color"], "Блю")
        self.request("PATCH", f'/api/products/{original_ids["MANUAL"]}', {"color": "Gray"})
        self.photo(original_ids["MANUAL"])
        rows[1][-1] = 12000
        self.commit(rows, "update")
        products = {item["sku"]: item for item in self.products()}
        self.assertEqual(products["MANUAL"]["color"], "Gray")
        self.assertEqual(products["EXPLICIT"]["color"], "Gray")
        self.assertEqual({sku: item["id"] for sku, item in products.items()}, original_ids)
        self.request("PATCH", f'/api/products/{original_ids["MANUAL"]}', {"color": ""})
        rows[0][-1] = 11500
        rows[1][-1] = 14000
        self.commit(rows, "update")
        self.commit(rows)
        products = {item["sku"]: item for item in self.products()}
        self.assertEqual(products["MANUAL"]["color"], "")
        self.assertIn("color", products["MANUAL"]["overrides"])
        self.assertEqual(products["MANUAL"]["overrides"]["color"], "")
        self.assertEqual(products["MANUAL"]["sale_price"], 17800)
        self.assertEqual(products["EXPLICIT"]["color"], "Gray")
        self.assertEqual(len(products["MANUAL"]["photos"]), 1)
        self.assertEqual({sku: item["id"] for sku, item in products.items()}, original_ids)
        self.stop_server()
        self.start_server()
        products = {item["sku"]: item for item in self.products()}
        self.assertEqual(products["MANUAL"]["color"], "")
        self.assertEqual(products["EXPLICIT"]["color"], "Gray")
        self.assertEqual(len(products["MANUAL"]["photos"]), 1)

    def csv_report(self, path):
        payload = self.request("GET", path, raw=True)
        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"), "Excel-compatible UTF-8 BOM expected")
        return list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig")), delimiter=";"))

    def test_session_problem_csv_excludes_valid_rows_and_escapes_formulas(self):
        self.commit([["A1", "Комод", "Лира", "Белый", "", 10000]])
        session, preview = self.preview([
            ["A1", "Комод", "Лира", "Белый", "", 10000],
            ["A2", "Шкаф", "Лира", "Дуб", "", 20000],
            ["A3", "Комод специальный", "Лира", "Белый", "", 10000],
            ["A3", "Комод специальный", "Лира", "Белый", "", 12000],
        ], name="=price.xlsx")
        self.assertEqual(preview["counts"], {"new": 1, "updated": 0, "duplicates": 1, "issues": 2})
        rows = self.csv_report("/api/import/report/" + session)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["Статус"] for row in rows}, {"issue"})
        self.assertEqual({row["Товар"] for row in rows}, {"Комод специальный"})
        self.assertEqual({row["Файл"] for row in rows}, {"'=price.xlsx"})
        self.assertEqual({row["Лист"] for row in rows}, {"Прайс"})
        self.assertEqual({row["Строка"] for row in rows}, {"4", "5"})

    def test_history_problem_csv_preserves_more_than_500_issues(self):
        count = 520
        session, preview = self.preview([
            [f"A{index}", f"Комод {index}", "Лира", "Белый", "", None]
            for index in range(count)
        ])
        self.assertEqual(preview["counts"]["issues"], count)
        self.assertLessEqual(len(preview["items"]), 300)
        committed = self.request("POST", "/api/import/commit", {"session_id": session})
        state = self.request("GET", "/api/state")
        entry = next(item for item in state["imports"] if item["id"] == committed["import_id"])
        self.assertEqual(entry["issues"], count)
        self.assertEqual(entry["details_count"], count)
        self.assertEqual(len(entry["details"]), 50)
        rows = self.csv_report("/api/imports/" + committed["import_id"] + "/report")
        self.assertEqual(len(rows), count)
        self.assertEqual({row["Статус"] for row in rows}, {"issue"})
        self.assertEqual({int(row["Строка"]) for row in rows}, set(range(2, count + 2)))
        self.assertEqual(self.products(), [])

    def test_state_revision_tracks_product_settings_and_photo_mutations(self):
        revision = self.request("GET", "/api/state")["revision"]
        self.assertIsInstance(revision, int)
        self.commit([["A1", "Комод", "Лира", "Белый", "", 10000]])
        item = self.products()[0]
        current = self.request("GET", "/api/state")["revision"]
        self.assertGreater(current, revision)
        revision = current
        self.request("PATCH", f'/api/products/{item["id"]}', {"dimensions": "800×400×900 мм"})
        current = self.request("GET", "/api/state")["revision"]
        self.assertGreater(current, revision)
        revision = current
        self.request("POST", "/api/settings", {"markup_percent": 30})
        current = self.request("GET", "/api/state")["revision"]
        self.assertGreater(current, revision)
        revision = current
        self.photo(item["id"], "one.png")
        current = self.request("GET", "/api/state")["revision"]
        self.assertGreater(current, revision)
        self.photo(item["id"], "two.png")
        target_id = self.products()[0]["photos"][1]["id"]
        revision = self.request("GET", "/api/state")["revision"]
        self.request("PATCH", f'/api/photos/{target_id}', {"primary": True})
        current = self.request("GET", "/api/state")["revision"]
        self.assertGreater(current, revision)
        revision = current
        self.request("DELETE", f'/api/photos/{target_id}')
        current = self.request("GET", "/api/state")["revision"]
        self.assertGreater(current, revision)

    def test_cross_origin_and_wrong_host_requests_are_rejected(self):
        before = self.request("GET", "/api/state")["settings"]
        self.expect_http_error("POST", "/api/settings", {"markup_percent": 50}, {"Origin": "https://foreign.example"}, (403,))
        self.expect_http_error("POST", "/api/settings", {"markup_percent": 50}, {"Host": "foreign.example"}, (400, 403))
        self.assertEqual(self.request("GET", "/api/state")["settings"], before)

    def test_dimension_filters_combine_with_factory_price_and_manual_edits(self):
        from urllib.parse import urlencode
        self.commit([
            ["A1","Шкаф А","Тест","Белый","800×2200×520",10000],
            ["A2","Шкаф Б","Тест","Белый","1800х2200х520",12000],
            ["A3","Тумба","Тест","Белый","Ш: 80 см; В: 90 см; Г: 52 см",5000],
            ["A4","Без размеров","Тест","Белый","",6000],
        ])
        def filtered(**params): return self.products("&"+urlencode(params))
        self.assertEqual([p["sku"] for p in filtered(dimensions="800 * 2200 * 520")],["A1"])
        self.assertEqual(len(filtered(width_min="800",width_max="800")),1)
        result=filtered(width_min="800",width_max="800",dimension_order="whd",dimension_unit="mm")
        self.assertEqual({p["sku"] for p in result},{"A1","A3"})
        self.assertEqual([p["sku"] for p in filtered(width_max="900",dimension_order="whd",dimension_unit="mm",min="9000")],["A1"])
        self.assertEqual(len(filtered(dimensions="800",factory="Другая")),0)
        target=next(p for p in result if p["sku"]=="A1")
        self.request("PATCH",f'/api/products/{target["id"]}',{"dimensions":"900×2200×520"})
        self.assertEqual(len(filtered(dimensions="800")),0)
        self.assertEqual(filtered(dimensions="900")[0]["id"],target["id"])
        self.expect_http_error("GET","/api/products?width_min=900&width_max=800",statuses=(400,))

    def test_width_range_reports_uninterpreted_sizes_and_inclusive_boundary(self):
        from urllib.parse import urlencode
        self.commit([
            ["A1","Шкаф 1900","Тест","Белый","1900×2200×500",10000],
            ["A2","Шкаф 1901","Тест","Белый","1901×2200×500",10000],
            ["A3","Шкаф 1902","Тест","Белый","1902×2200×500",10000],
            ["A4","Стеллаж","Тест","Белый","800×1900×360",5000],
            ["A5","Без размеров","Тест","Белый","",5000],
        ])
        def result(**params): return self.request("GET","/api/products?"+urlencode(params))
        missing=result(width_max="1901")
        self.assertEqual(missing["total"],0)
        self.assertEqual(missing["dimension_filter"]["recognized"],0)
        self.assertEqual(missing["dimension_filter"]["unrecognized"],5)
        self.assertEqual(missing["dimension_filter"]["empty"],1)
        configured=result(width_max="1901",dimension_order="whd",dimension_unit="mm")
        self.assertEqual({p["sku"] for p in configured["items"]},{"A1","A2","A4"})
        self.assertEqual(configured["dimension_filter"]["recognized"],4)
        self.assertEqual(configured["dimension_filter"]["unrecognized"],1)
        exact=result(dimensions="1900")
        self.assertEqual({p["sku"] for p in exact["items"]},{"A1","A4"})
        self.assertIsNone(exact["dimension_filter"])
        combined=result(dimensions="1900",width_max="1901")
        self.assertEqual(combined["dimension_filter"]["unrecognized"],2)
        empty=result(width_max="1901",factory="Нет такой фабрики")
        self.assertEqual(empty["dimension_filter"]["considered"],0)
    def test_updates_cannot_modify_application_from_an_isolated_database(self):
        self.assertEqual(self.request("GET","/api/update")["version"],self.request("GET","/api/state")["version"])
        self.expect_http_error("POST","/api/update/check",{"source":"https://example.com/latest.json"},statuses=(403,))
        self.expect_http_error("POST","/api/update/install",{"source":"https://example.com/latest.json"},statuses=(403,))
        self.expect_http_error("POST","/api/update/check",{"source":"http://example.com/latest.json"},statuses=(400,))
        self.assertEqual(self.request("GET","/api/state")["stats"]["total"],0)


if __name__ == "__main__":
    unittest.main()
