"""Folder watcher checks: one deterministic scan, no polling sleeps."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import Catalog
from test_server import MAPPING, make_xlsx


class OneScan:
    def __init__(self):
        self.done = False

    def wait(self, _timeout):
        if self.done:
            return True
        self.done = True
        return False

    def set(self):
        self.done = True


class FolderWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="catalog-watch-qa-")
        self.root = Path(self.tmp.name)
        self.prices = self.root / "prices"
        self.prices.mkdir()
        self.path = self.prices / "Альбина 01.10.26.xlsx"
        self.catalog = Catalog(self.root / "database")
        self.catalog.stop_event.set()
        self.catalog.watcher.join(timeout=2)

    def tearDown(self):
        self.catalog.stop_event.set()
        self.catalog.watcher.join(timeout=2)
        self.tmp.cleanup()

    def scan_tick(self):
        self.catalog.stop_event = OneScan()
        self.catalog.watch_loop()

    def save_profile(self):
        inspected = self.catalog.inspect({"paths": [str(self.path)]})
        for config in inspected["files"]:
            config["factory"] = "Альбина"
            for sheet in config["sheets"]:
                sheet["selected"] = True
                sheet["mapping"] = MAPPING.copy()
        self.catalog.preview({"session_id": inspected["session_id"], "mode": "add", "files": inspected["files"]})
        self.catalog.commit(inspected["session_id"])

    def test_unknown_profile_goes_to_review_without_importing(self):
        make_xlsx(self.path, [["A1", "Комод", "Лира", "Белый", "", 10000]])
        self.catalog.folder({"path": str(self.prices), "watch": True})
        self.scan_tick()
        state = self.catalog.state()
        self.assertEqual(state["stats"]["total"], 0)
        self.assertEqual(state["folder"]["status"], "review")
        self.assertTrue(state["folder"]["pending_session"])

    def test_saved_profile_updates_once_and_preserves_other_products(self):
        row = ["A1", "Комод", "Лира", "Белый", "", 10000]
        make_xlsx(self.path, [row])
        self.save_profile()
        original_id = self.catalog.list_products({})["items"][0]["id"]
        row[-1] = 12000
        make_xlsx(self.path, [row, ["A2", "Шкаф", "Лира", "Дуб", "", 20000]])
        self.catalog.folder({"path": str(self.prices), "watch": True, "mode": "update"})
        self.scan_tick()
        result = self.catalog.list_products({})
        self.assertEqual(result["total"], 2)
        original = next(item for item in result["items"] if item["sku"] == "A1")
        self.assertEqual(original["id"], original_id)
        self.assertEqual(original["base_price"], 12000)
        self.assertEqual(original["sale_price"], 15400)
        state = self.catalog.state()
        self.assertEqual(state["folder"]["status"], "watching")
        self.assertIsNone(state["folder"]["pending_session"])
        import_count = len(state["imports"])
        self.scan_tick()
        self.assertEqual(len(self.catalog.state()["imports"]), import_count)
        row[-1] = 13000
        make_xlsx(self.path, [row])
        self.scan_tick()
        result = self.catalog.list_products({})
        self.assertEqual(result["total"], 2)
        original = next(item for item in result["items"] if item["sku"] == "A1")
        self.assertEqual(original["sale_price"], 16600)


if __name__ == "__main__":
    unittest.main()
