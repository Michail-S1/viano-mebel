"""Portable archive checks, using disposable databases and Windows file locks."""
from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from launcher.package import CODE_DIRS, ROOT_FILES, build_archive

PHOTO = "1" * 32 + ".png"


@contextmanager
def exclusive_log(path):
    if os.name != "nt":
        with path.open("wb"):
            yield
        return
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateFileW(str(path), 0x40000000, 0, None, 4, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


class PortableArchive(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="viano-package-")
        self.root = Path(self.temporary.name) / "Виано Мебель с пробелами"
        self.root.mkdir()
        for name in ROOT_FILES:
            (self.root / name).write_text("fixture " + name, encoding="utf-8")
        for name in CODE_DIRS:
            (self.root / name).mkdir()
            (self.root / name / "source.txt").write_text("included", encoding="utf-8")
        for name in (".runtime/logs", ".qa", ".analysis", "exports", "data/photos",
                     "data/incoming", "data/backups", "backend/__pycache__"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.log = self.root / ".runtime/logs/locked.out.log"
        (self.root / ".qa/large-test.exe").write_bytes(b"excluded")
        (self.root / ".analysis/report.json").write_text("{}")
        (self.root / "exports/older.zip").write_bytes(b"excluded")
        (self.root / "backend/__pycache__/module.pyc").write_bytes(b"excluded")
        (self.root / "data/incoming/source.xlsx").write_bytes(b"preserved source")
        (self.root / "data/backups/older.zip").write_bytes(b"preserved backup")
        (self.root / "data/photos" / PHOTO).write_bytes(b"preserved photograph")
        (self.root / "data/photos/orphan.png").write_bytes(b"unreferenced")
        self.database = self.root / "data/catalog.sqlite3"
        self.db = sqlite3.connect(self.database)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA wal_autocheckpoint=0;
            CREATE TABLE products (id TEXT PRIMARY KEY, name TEXT, base_price REAL);
            CREATE TABLE photos (filename TEXT UNIQUE);
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.execute("INSERT INTO products VALUES ('first','Комод',10000)")
        self.db.execute("INSERT INTO photos VALUES (?)", (PHOTO,))
        self.db.execute("INSERT INTO metadata VALUES ('settings',?)",
                        (json.dumps({"markup_percent": 20, "fixed_add": 1000}),))
        self.db.commit()
        self.output = self.root / "exports/catalog.zip"

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def test_open_database_wal_photos_and_exclusively_locked_logs(self):
        # This committed row is still in the WAL; copying only .sqlite3 would lose it.
        self.db.execute("INSERT INTO products VALUES ('latest','Стол блю',9000)")
        self.db.commit()
        self.assertTrue(Path(str(self.database) + "-wal").is_file())
        with exclusive_log(self.log):
            if os.name == "nt":
                with self.assertRaises(PermissionError):
                    self.log.read_bytes()
            result = build_archive(self.root, self.output)
        self.assertEqual(result["products"], 2)
        self.assertEqual(result["photos"], 1)
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
            self.assertIn("IBD/Start.cmd", names)
            self.assertIn("IBD/launcher/source.txt", names)
            self.assertIn("IBD/data/incoming/source.xlsx", names)
            self.assertIn("IBD/data/backups/older.zip", names)
            self.assertEqual(archive.read("IBD/data/photos/" + PHOTO), b"preserved photograph")
            for name in names:
                self.assertNotIn(".runtime", name)
                self.assertNotIn(".qa/", name)
                self.assertNotIn(".analysis/", name)
                self.assertNotIn("exports/", name)
                self.assertNotIn("__pycache__", name)
                self.assertFalse(name.endswith(("-wal", "-shm", ".pyc", "orphan.png")))
            extracted = self.root.parent / "Обычная распаковка"
            archive.extractall(extracted)
        with closing(sqlite3.connect(extracted / "IBD/data/catalog.sqlite3")) as restored:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT name,base_price FROM products WHERE id='latest'").fetchone(),
                             ("Стол блю", 9000))
            self.assertEqual(json.loads(restored.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()[0])["fixed_add"], 1000)
        self.assertEqual(self.db.execute("SELECT count(*) FROM products").fetchone()[0], 2)
        self.assertFalse(list(self.output.parent.glob(".package-*")))

    def test_missing_photo_does_not_publish_partial_archive(self):
        (self.root / "data/photos" / PHOTO).unlink()
        with self.assertRaisesRegex(FileNotFoundError, "фотография"):
            build_archive(self.root, self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.output.parent.glob(".package-*")))

    def test_existing_archive_is_not_overwritten(self):
        self.output.write_bytes(b"previous archive")
        with self.assertRaises(FileExistsError):
            build_archive(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), b"previous archive")

    def test_output_inside_data_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exports"):
            build_archive(self.root, self.root / "data/backups/new.zip")
        self.assertFalse((self.root / "data/backups/new.zip").exists())

    def test_missing_application_file_does_not_publish_archive(self):
        (self.root / "Start.cmd").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "Start.cmd"):
            build_archive(self.root, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
