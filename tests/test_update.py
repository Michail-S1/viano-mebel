import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from launcher import update
from launcher.release import build_release

class CodeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="viano-update-")
        self.root=Path(self.temp.name)
        self.app=self.root/"Программа с пробелами"
        self.app.mkdir()
        (self.app/"server.py").write_text('VERSION = "1.0.0"\n',encoding="utf-8")
        (self.app/"data/photos").mkdir(parents=True)
        (self.app/"data/catalog.sqlite3").write_bytes(b"personal-database")
        (self.app/"data/photos/manual.jpg").write_bytes(b"manual-photo")
        (self.app/"update-source.json").write_text('{"source":"local"}',encoding="utf-8")
        self.release=self.root/"release"
        self.release.mkdir()
    def tearDown(self): self.temp.cleanup()
    def package(self, extra=None):
        files={"server.py":'VERSION = "2.0.0"\n',"static/app.js":"// updated",
               "static/index.html":"<html>updated</html>","backend/importer.py":"# updated",
               "launcher/update.py":"# updated",**(extra or {})}
        memory=io.BytesIO()
        with zipfile.ZipFile(memory,"w",zipfile.ZIP_DEFLATED) as z:
            for name,data in files.items():
                info=zipfile.ZipInfo(name)
                info.filename=name
                z.writestr(info,data)
        data=memory.getvalue()
        (self.release/"code.zip").write_bytes(data)
        manifest={"app":update.APP,"version":"2.0.0","url":"code.zip","sha256":hashlib.sha256(data).hexdigest()}
        (self.release/"latest.json").write_text(json.dumps(manifest),encoding="utf-8")
        return update.release(str(self.release/"latest.json"),"1.0.0")
    def assert_personal(self):
        self.assertEqual((self.app/"data/catalog.sqlite3").read_bytes(), b"personal-database")
        self.assertEqual((self.app/"data/photos/manual.jpg").read_bytes(), b"manual-photo")
        self.assertEqual((self.app/"update-source.json").read_text(encoding="utf-8"),'{"source":"local"}')
    def test_install_preserves_all_personal_data_and_saves_previous_code(self):
        staged=update.prepare(self.app,self.package())
        backup=update.install(self.app,staged)
        self.assertEqual(update.source_version((self.app/"server.py").read_bytes()),"2.0.0")
        self.assertEqual(update.source_version((backup/"server.py").read_bytes()),"1.0.0")
        self.assert_personal()
        update.restore_code(self.app,backup)
        self.assertEqual(update.source_version((self.app/"server.py").read_bytes()),"1.0.0")
        self.assertFalse((self.app/"static/app.js").exists())
        self.assert_personal()
    def test_file_write_failure_rolls_back_code(self):
        staged=update.prepare(self.app,self.package())
        replace=os.replace
        calls=0
        def fail(source,target):
            nonlocal calls
            calls+=1
            if calls==2: raise OSError("simulated write failure")
            return replace(source,target)
        with patch("launcher.update.os.replace",side_effect=fail):
            with self.assertRaises(OSError): update.install(self.app,staged)
        self.assertEqual(update.source_version((self.app/"server.py").read_bytes()),"1.0.0")
        self.assertFalse((self.app/"backend/importer.py").exists())
        self.assert_personal()
    def test_bad_hash_does_not_change_files(self):
        manifest=self.package()
        manifest["sha256"]="0"*64
        with self.assertRaisesRegex(ValueError,"сумма"): update.prepare(self.app,manifest)
        self.assert_personal()
        self.assertEqual(update.source_version((self.app/"server.py").read_bytes()),"1.0.0")
    def test_forbidden_paths_cannot_replace_user_files(self):
        for name in ("../server.py","data/catalog.sqlite3",".runtime/update-source.json",
                     "backend/../../data/catalog.sqlite3","C:/data/hack.py","static/CON.txt","static/a\\b.txt"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError): update.prepare(self.app,self.package({name:"bad"}))
                self.assert_personal()
    def test_bad_python_or_version_is_rejected_before_install(self):
        for value in ('VERSION = "3.0.0"\n','VERSION = "2.0.0"\nthis is broken !'):
            with self.subTest(value=value):
                with self.assertRaises((ValueError,SyntaxError)):
                    update.prepare(self.app,self.package({"server.py":value}))
                self.assert_personal()
    def test_current_version_does_not_offer_downgrade(self):
        self.package()
        for current in ("2.0.0","3.0.0"):
            self.assertFalse(update.release(str(self.release/"latest.json"),current)["available"])
    def test_clean_install_uses_github_without_writing_configuration(self):
        fresh=self.root/"fresh"
        fresh.mkdir()
        self.assertEqual(update.configured_source(fresh), update.DEFAULT_SOURCE)
        self.assertFalse((fresh/"update-source.json").exists())
    def test_custom_update_source_has_priority_over_default(self):
        custom="https://example.com/catalog/latest.json"
        (self.app/"update-source.json").write_text(json.dumps({"source":custom}),encoding="utf-8")
        self.assertEqual(update.configured_source(self.app), custom)
        self.assertEqual(json.loads((self.app/"update-source.json").read_text(encoding="utf-8"))["source"],custom)
    def test_source_requires_https_without_embedded_credentials(self):
        for source in ("http://example.com/latest.json","https://user:password@example.com/latest.json","ftp://example.com/latest.json"):
            with self.assertRaises(ValueError): update.validate_source(source)
    def test_release_contains_only_program(self):
        self.package()
        source=self.root/"source"
        source.mkdir()
        with zipfile.ZipFile(self.release/"code.zip") as z: z.extractall(source)
        (source/"Update.cmd").write_text("@echo off",encoding="utf-8")
        (source/"data").mkdir()
        (source/"data/catalog.sqlite3").write_bytes(b"must not be published")
        (source/"exports").mkdir()
        (source/"exports/full.zip").write_bytes(b"must not be published")
        target=build_release(source,self.root/"output")
        with zipfile.ZipFile(target) as z:
            self.assertIn("Update.cmd",z.namelist())
            self.assertFalse(any(name.startswith(("data/","exports/",".runtime/")) for name in z.namelist()))
        self.assertTrue((target.parent/"latest.json").is_file())

if __name__ == "__main__": unittest.main()
