import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from launcher.github_release import release_context, publish
from launcher.source import prepare_source

class GitHubPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="viano-github-")
        self.root=Path(self.tmp.name)/"project"
        self.root.mkdir()
        for name,content in {
            "server.py":'VERSION = "1.2.0"\n',"Update.cmd":"@echo off",
            "static/app.js":"// fixture","static/index.html":"<html></html>",
            "backend/importer.py":"# fixture","launcher/update.py":"# fixture"
        }.items():
            target=self.root/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(content,encoding="utf-8")
    def tearDown(self): self.tmp.cleanup()
    def test_clean_source_excludes_personal_files_and_private_documentation(self):
        for name in ("data/catalog.sqlite3","data/photos/manual.jpg","exports/full.zip",
                     ".runtime/process.log",".qa/fixture.xlsx","tests/private.xlsx",
                     "update-source.json","backend/__pycache__/private.pyc"):
            target=self.root/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text("PERSONAL-DATA",encoding="utf-8")
        (self.root/"README.md").write_text(
            "Public description\n## Стартовый каталог\nPERSONAL-DATA\n\nПоиск по размерам: general help",encoding="utf-8")
        (self.root/"PROJECT_PROMPT.md").write_text("Project path "+str(self.root),encoding="utf-8")
        (self.root/"tests/test_example.py").write_text("# test",encoding="utf-8")
        target=Path(self.tmp.name)/"clean-source"
        names=prepare_source(self.root,target)
        self.assertIn("tests/test_example.py",names)
        self.assertFalse(any(name.startswith(("data/","exports/",".runtime/",".qa/")) for name in names))
        self.assertNotIn("update-source.json",names)
        for file in target.rglob("*"):
            if file.is_file(): self.assertNotIn(b"PERSONAL-DATA",file.read_bytes(),str(file))
        self.assertNotIn(str(self.root),(target/"PROJECT_PROMPT.md").read_text(encoding="utf-8"))
        self.assertIn("Поиск по размерам",(target/"README.md").read_text(encoding="utf-8"))
    def test_document_paths_ignore_windows_letter_case_and_separators(self):
        description="Paths: " + str(self.root).upper() + " and " + str(self.root).replace("\\", "/")
        (self.root/"PROJECT_PROMPT.md").write_text(description,encoding="utf-8")
        target=Path(self.tmp.name)/"different-path-spelling"
        prepare_source(self.root,target)
        self.assertEqual((target/"PROJECT_PROMPT.md").read_text(encoding="utf-8"),"Paths: папка проекта and папка проекта")
    def test_dependencies_are_included_in_clean_source(self):
        dependency=self.root/"vendor/xlrd/__init__.py"
        dependency.parent.mkdir(parents=True)
        dependency.write_text("# dependency",encoding="utf-8")
        target=Path(self.tmp.name)/"with-dependencies"
        names=prepare_source(self.root,target)
        self.assertIn("vendor/xlrd/__init__.py",names)
        self.assertEqual((target/"vendor/xlrd/__init__.py").read_text(encoding="utf-8"),"# dependency")
    def test_unreadable_sources_fail_before_export(self):
        def denied(directory, onerror, followlinks):
            onerror(PermissionError(13,"denied",str(directory)))
            return iter(())
        target=Path(self.tmp.name)/"failed-export"
        with patch("launcher.release.os.walk",side_effect=denied):
            with self.assertRaises(OSError): prepare_source(self.root,target)
        self.assertFalse(target.exists())
    def test_tag_must_match_version_and_urls_are_bound_to_release(self):
        context=release_context(self.root,"owner/viano-mebel","v1.2.0","tag")
        self.assertEqual(context["tag"],"v1.2.0")
        self.assertIn("/releases/download/v1.2.0/",context["package_url"])
        self.assertTrue(context["source_url"].endswith("/releases/latest/download/latest.json"))
        with self.assertRaises(ValueError): release_context(self.root,"owner/viano-mebel","v1.1.0","tag")
        with self.assertRaises(ValueError): release_context(self.root,"https://github.com/owner/repo")
    def test_already_published_version_is_never_replaced(self):
        with patch.dict(os.environ,{"GH_TOKEN":"test-only-token"}),patch("launcher.github_release.shutil.which",return_value="gh"),patch("launcher.github_release.gh",return_value=subprocess.CompletedProcess([],0,'{"isDraft":false}',"")) as command:
            with self.assertRaises(ValueError): publish(self.root,release_context(self.root,"owner/repo"))
        self.assertEqual(command.call_count,1)
        self.assertFalse((self.root/"exports").exists())
    def test_release_remains_draft_when_asset_upload_fails(self):
        calls=[]
        def command(arguments,repository,check=True):
            calls.append(arguments)
            if arguments[1]=="view": return subprocess.CompletedProcess([],1,"","not found")
            if arguments[1]=="upload": raise RuntimeError("simulated upload failure")
            return subprocess.CompletedProcess([],0,"","")
        with patch.dict(os.environ,{"GH_TOKEN":"test-only-token"}),patch("launcher.github_release.shutil.which",return_value="gh"),patch("launcher.github_release.gh",side_effect=command):
            with self.assertRaises(RuntimeError): publish(self.root,release_context(self.root,"owner/repo"))
        self.assertIn("--draft",calls[1])
        self.assertFalse(any(arguments[1]=="edit" for arguments in calls))
    def test_both_code_assets_are_uploaded_before_publication(self):
        calls=[]
        def command(arguments,repository,check=True):
            calls.append(arguments)
            return subprocess.CompletedProcess([],1 if arguments[1]=="view" else 0,"","")
        with patch.dict(os.environ,{"GH_TOKEN":"test-only-token"}),patch("launcher.github_release.shutil.which",return_value="gh"),patch("launcher.github_release.gh",side_effect=command):
            source=publish(self.root,release_context(self.root,"owner/repo"))
        self.assertEqual([arguments[1] for arguments in calls],["view","create","upload","edit"])
        self.assertIn("--draft=false",calls[-1])
        self.assertIn("latest.json"," ".join(calls[-2]))
        self.assertTrue(source.startswith("https://github.com/owner/repo/"))
        manifest=json.loads((self.root/"exports/github-release/latest.json").read_text(encoding="utf-8"))
        self.assertIn("/releases/download/v1.2.0/",manifest["url"])

if __name__=="__main__": unittest.main()
