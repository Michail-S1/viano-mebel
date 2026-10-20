"""Supplier format updates must preserve catalog identities and user work."""
import sqlite3
import tempfile
from contextlib import closing
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from server import Catalog, TEXT_FIELDS, compact
from backend.catalog_identity import normalize_product, semantic_key, header_context, repair_plan
from backend.importer import product_key, parse_file, inspect_file
from test_importer import fixture

OLD_HEADERS = ["Наименование готового модуля", "", "", "Ширина (мм)",
               "Наименование корпуса", "Наименование фасада", "Корпус", "Фасад", "Цена продукции (руб.)"]
NEW_HEADERS = ["НАИМЕНОВАНИЕ МОДУЛЯ", "РАЗМЕР", "НАИМЕНОВАНИЕ КОРПУСА", "НАИМЕНОВАНИЕ ФАСАДА",
               "ЦЕНА КОРПУС", "ЦЕНА ФАСАД", "ЦЕНА МОДУЛЯ"]
CONTEXT = {"version": 1, "kind": "dsv-module", "collection": "Виано"}


def product(**changes):
    result = dict(name="Верхний шкаф Виано АНП 400", factory="ДСВ", collection="Виано",
                  sku="", color="", dimensions="Ш: 400", variant="", base_price=1000,
                  source_file="old.xlsx", source_sheet="Виано", source_row=2)
    result.update(changes)
    return result


class SupplierIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="identity-qa-")
        self.root = Path(self.tmp.name)
        self.catalog = Catalog(self.root / "data")

    def tearDown(self):
        self.catalog.stop_event.set()
        self.tmp.cleanup()

    def seed(self, identifier, source, stamp="2026-10-01", overrides=None):
        display = {k: (overrides or {}).get(k, source[k]) for k in TEXT_FIELDS}
        columns = ["id", "identity_key", *TEXT_FIELDS, "base_price", "source_file", "source_sheet",
                   "source_row", "imported_at", "source_values", "overrides", "search_text"]
        values = [identifier, product_key(source), *display.values(), source["base_price"],
                  source["source_file"], source["source_sheet"], source["source_row"], stamp,
                  compact(source), compact(overrides or {}), " ".join(display.values())]
        with self.catalog.connection() as db:
            db.execute("INSERT INTO products (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", values)
            db.execute("INSERT INTO aliases VALUES (?,?)", (product_key(source), identifier))
            db.execute("INSERT OR IGNORE INTO imports VALUES (?,?,?,?,?,?,?,?,?)",
                       (stamp, stamp, "update", "[]", 0, 0, 0, 0, "[]"))

    def profile(self, name="old.xlsx"):
        config = {"sheets": [{"name": "Виано", "headers": [
            {"column": chr(65+i), "label": text} for i, text in enumerate(OLD_HEADERS)],
            "mapping": {"name": "A", "price": "I", "width": "D"}}]}
        with self.catalog.connection() as db:
            db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?)", (name, name, "ДСВ", compact(config), "old"))

    def preview(self, source, mode="update"):
        sid = "test-session"
        cfg = {"file_id": "file", "factory": source["factory"], "import_photos": False, "sheets": []}
        self.catalog.sessions[sid] = {"created": __import__("time").time(),
            "files": {"file": {"name": source["source_file"], "path": self.root/"unused.xlsx"}}, "configs": [cfg]}
        with patch("server.parse_file", return_value={"products": [source], "issues": []}):
            result = self.catalog.preview({"session_id": sid, "mode": mode})
        return sid, result

    def test_old_and_new_supplier_files_have_same_key(self):
        outputs = []
        for name, headers, row in [
            ("old.xlsx", OLD_HEADERS, {"A": product()["name"], "D": 400, "I": 1000}),
            ("new.xlsx", NEW_HEADERS, {"A": product()["name"], "B": 400, "G": 1250}),
        ]:
            path = self.root/name
            fixture(path, {"Виано": {"rows": [dict(zip("ABCDEFGHI", headers)), row]}})
            config = inspect_file(str(path), "file")
            config["factory"] = "ДСВ"
            outputs.append(parse_file(str(path), config)["products"][0])
        self.assertEqual(outputs[0]["dimensions"], "Ш: 400")
        self.assertEqual(outputs[1]["dimensions"], "Ш: 400")
        self.assertEqual(product_key(outputs[0]), product_key(outputs[1]))
        self.assertEqual(outputs[1]["base_price"], 1250)

    def test_palette_expansion_is_not_a_new_finish(self):
        ctx = {"kind": "sv-colours", "version": 1}
        palettes = ["Пыльная роза; Пепельный беж; Ментол; Айвори; Меланж, Графит",
                    "Пыльная роза; Пепельный беж; Ментол; Айвори; Меланж; Графит; Нео"]
        a,b = [normalize_product(product(factory="SV-Мебель", collection="ФАСАДЫ КВАДРО | " + x, color=x),ctx) for x in palettes]
        self.assertEqual(semantic_key(a), semantic_key(b))
        self.assertEqual(a["collection"], "ФАСАДЫ КВАДРО")
        self.assertNotEqual(product_key(normalize_product(product(factory="SV-Мебель", color="Белый"),ctx)),
                            product_key(normalize_product(product(factory="SV-Мебель", color="Графит"),ctx)))

    def test_distinct_sizes_colours_components_and_collections_survive(self):
        base = normalize_product(product(), CONTEXT)
        for change in [{"dimensions":"Ш: 402"}, {"color":"Белый"}, {"variant":"Корпус"},
                       {"identity_context":dict(CONTEXT, collection="Гарда") }]:
            other = normalize_product(dict(base, **change))
            self.assertNotEqual(product_key(base), product_key(other))
        headers = [{"column":chr(65+i), "label":text} for i,text in enumerate(NEW_HEADERS)]
        self.assertEqual(header_context("ДСВ", "Виано",headers,{"name":"A","price":"E"}), {})
        self.assertEqual(header_context("ДСВ", "Виано",headers,{"name":"A","price":"G","collection":"J"}), {})

    def test_update_matches_legacy_row_and_retains_alias_in_add_mode(self):
        self.seed("old-id", product())
        self.profile()
        updated = normalize_product(product(source_file="new.xlsx", dimensions="400", variant="Модуль", base_price=1400),CONTEXT)
        sid, result = self.preview(updated, "add")
        self.assertEqual(result["counts"]["new"], 0)
        self.catalog.commit(sid)
        # The old profile can disappear; the semantic alias still identifies the item.
        with self.catalog.connection() as db:
            db.execute("DELETE FROM profiles")
        sid, result = self.preview(updated)
        self.assertEqual(result["counts"]["updated"], 1)
        self.catalog.commit(sid)
        with self.catalog.connection() as db:
            row = db.execute("SELECT * FROM products").fetchone()
            self.assertEqual(row["id"], "old-id")
            self.assertEqual(row["base_price"], 1400)
            self.assertEqual(db.execute("SELECT count(*) FROM products").fetchone()[0], 1)
        _, result = self.preview(updated)
        self.assertEqual(result["counts"]["duplicates"], 1)

    def test_repair_backs_up_and_preserves_id_manual_edits_photos_and_references(self):
        old = product()
        new = product(source_file="new.xlsx", collection="Новая палитра", dimensions="400", variant="Модуль", base_price=1400,
                      identity_context=CONTEXT)
        self.seed("old-id", old, overrides={"name":"Моё название"})
        self.profile()
        self.seed("duplicate", new, "2026-10-20", overrides={"color":"Белый"})
        with self.catalog.connection() as db:
            for i, owner, content in [(1,"old-id",b"manual"),(2,"duplicate",b"manual"),(3,"duplicate",b"excel")]:
                filename = str(i)+".jpg"
                (self.catalog.photos_dir/filename).write_bytes(content)
                db.execute("INSERT INTO photos VALUES (?,?,?,?,?,?)", (str(i),owner,filename,filename,1 if i!=3 else 0,"old"))
            for table in ("avito_links","avito_plan_items","avito_decisions"):
                db.execute("CREATE TABLE " + table + " (product_id TEXT REFERENCES products(id))")
                db.execute("INSERT INTO " + table + " VALUES ('duplicate')")
        result = self.catalog.repair_catalog_identity(force=True)
        self.assertEqual(result["removed"], 1)
        with zipfile.ZipFile(result["backup"]) as archive:
            self.assertIn("photos/3.jpg", archive.namelist())
            backup = self.root/"before.sqlite3"
            backup.write_bytes(archive.read("catalog.sqlite3"))
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM products").fetchone()[0],2)
        with self.catalog.connection() as db:
            row = db.execute("SELECT * FROM products").fetchone()
            self.assertEqual((row["id"],row["name"],row["color"],row["base_price"]), ("old-id","Моё название","Белый",1400))
            self.assertEqual(db.execute("SELECT count(*) FROM photos").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT id FROM photos WHERE is_primary=1").fetchone()[0],"1")
            for table in ("avito_links","avito_plan_items","avito_decisions"):
                self.assertEqual(db.execute("SELECT product_id FROM " + table).fetchone()[0], "old-id")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(len(list(self.catalog.photos_dir.iterdir())),3)
        self.assertEqual(self.catalog.repair_catalog_identity(force=True)["removed"],0)

    def test_conflicting_manual_edits_and_same_file_rows_require_review(self):
        self.seed("old", product(identity_context=CONTEXT), overrides={"color":"Белый"})
        self.seed("new", product(source_file="new.xlsx", dimensions="400", variant="Модуль", identity_context=CONTEXT),
                  "2026-10-20", overrides={"color":"Графит"})
        with self.catalog.connection() as db:
            plan = repair_plan(db)
        self.assertEqual(len(plan["groups"]),0)
        self.assertEqual(len(plan["review"]),1)
        _, result = self.preview(normalize_product(product(),CONTEXT))
        self.assertEqual(result["counts"]["issues"],1)
        self.assertEqual(result["counts"]["new"],0)

    def test_exact_legacy_collision_in_another_collection_is_reviewed(self):
        raw = product(collection="Блю, Вижн", dimensions="400", variant="Модуль")
        self.seed("legacy", raw)
        candidate = normalize_product(dict(raw, source_sheet="Паола"), dict(CONTEXT, collection="Паола"))
        _, result = self.preview(candidate)
        self.assertEqual(result["counts"]["new"], 0)
        self.assertEqual(result["counts"]["issues"], 1)

    def test_add_only_repair_does_not_replace_original_price(self):
        self.seed("old", product(identity_context=CONTEXT))
        self.seed("new", product(source_file="new.xlsx", dimensions="400", variant="Модуль", base_price=2500,
                                 identity_context=CONTEXT), "2026-10-20")
        with self.catalog.connection() as db:
            db.execute("UPDATE imports SET mode='add' WHERE created_at='2026-10-20'")
        self.catalog.repair_catalog_identity(force=True)
        with self.catalog.connection() as db:
            self.assertEqual(db.execute("SELECT base_price FROM products").fetchone()[0],1000)

    def test_same_file_rows_are_not_merged_automatically(self):
        self.seed("old", product(identity_context=CONTEXT))
        self.seed("new", product(dimensions="400", variant="Модуль", identity_context=CONTEXT))
        with self.catalog.connection() as db:
            plan = repair_plan(db)
        self.assertEqual(len(plan["groups"]),0)
        self.assertEqual(plan["review"][0]["reason"],"Несколько строк в одном прайсе")

    def test_missing_photo_aborts_before_any_changes(self):
        self.seed("old", product(identity_context=CONTEXT))
        self.seed("new", product(source_file="new.xlsx", dimensions="400", variant="Модуль", identity_context=CONTEXT), "2026-10-20")
        with self.catalog.connection() as db:
            db.execute("INSERT INTO photos VALUES ('p','old','missing.jpg','manual',1,'old')")
        with self.assertRaises(Exception):
            self.catalog.repair_catalog_identity(force=True)
        with self.catalog.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM products").fetchone()[0],2)


if __name__ == "__main__":
    unittest.main()
