"""Create a portable ZIP without touching live logs or copying a live SQLite file."""
from __future__ import annotations

import argparse
import re
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT_FILES = (
    "server.py", "Start.cmd", "Start.ps1", "Stop.ps1",
    "Create-Archive.cmd", "Create-Archive.ps1", "README.md", "requirements.txt",
    "Update.cmd", "Update.ps1", "Create-Update.cmd", "Create-Update.ps1",
)
CODE_DIRS = ("backend", "static", "vendor", "launcher")
OPTIONAL_FILES = ("AGENTS.md", "PROJECT_PROMPT.md", ".gitignore", "update-source.json", "UPDATES.md", "AUTONOMY.md")
PHOTO_NAME = re.compile(r"[a-f0-9]{32}\.(?:jpg|png|webp)")


def portable_files(root: Path):
    for name in ROOT_FILES:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"В папке приложения отсутствует {name}.")
        yield path, name
    for name in OPTIONAL_FILES:
        path = root / name
        if path.is_file():
            yield path, name
    for name in (*CODE_DIRS, "tests", "data/incoming", "data/backups"):
        directory = root / name
        if name in CODE_DIRS and not directory.is_dir():
            raise FileNotFoundError(f"В папке приложения отсутствует {name}.")
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo"):
                continue
            if not path.is_file():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError(f"В архивируемой папке обнаружена внешняя ссылка: {relative}.")
            yield path, relative.as_posix()


def snapshot_database(source_path: Path, snapshot_path: Path):
    if not source_path.is_file():
        raise FileNotFoundError("База не найдена. Сначала запустите каталог через Start.cmd.")
    deadline = time.monotonic() + 120

    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise TimeoutError("База занята длительной записью. Повторите создание архива после завершения импорта.")

    # mode=ro prevents creating or modifying the source database; backup includes WAL.
    with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
        with closing(sqlite3.connect(snapshot_path)) as snapshot:
            source.backup(snapshot, pages=1024, progress=progress)
            snapshot.execute("PRAGMA journal_mode=DELETE")
            if snapshot.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("Проверка резервной копии базы не пройдена.")
            count = snapshot.execute("SELECT count(*) FROM products").fetchone()[0]
            photos = [row[0] for row in snapshot.execute("SELECT filename FROM photos ORDER BY filename")]
    for filename in photos:
        if not isinstance(filename, str) or not PHOTO_NAME.fullmatch(filename):
            raise ValueError("В базе найдено некорректное имя фотографии.")
    return count, photos


def build_archive(app_root: Path, output: Path):
    root = app_root.resolve()
    output = output.resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("Архив должен иметь расширение .zip.")
    if output.exists():
        raise FileExistsError("Архив с таким именем уже существует. Укажите другое имя.")
    # Prevent an output or staging archive from becoming part of its own inputs.
    for name in (*CODE_DIRS, "tests", "data"):
        if output.is_relative_to(root / name):
            raise ValueError("Сохраняйте ZIP в папку exports или вне папок программы и данных.")
    inputs = list(portable_files(root))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".package-", dir=output.parent) as staging:
        staging = Path(staging)
        database = staging / "catalog.sqlite3"
        count, photos = snapshot_database(root / "data/catalog.sqlite3", database)
        staged_zip = staging / "catalog.zip"
        print(f"Сохраняем {count:,} товаров и {len(photos)} фотографий...", flush=True)
        with zipfile.ZipFile(staged_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path, name in inputs:
                archive.write(path, "IBD/" + name)
            archive.write(database, "IBD/data/catalog.sqlite3")
            archive.writestr("IBD/data/photos/", b"")
            for filename in photos:
                photo = root / "data/photos" / filename
                if not photo.is_file():
                    raise FileNotFoundError(f"Не найдена фотография {filename}. Архив не создан.")
                archive.write(photo, "IBD/data/photos/" + filename)
        with zipfile.ZipFile(staged_zip) as archive:
            corrupt = archive.testzip()
            if corrupt:
                raise ValueError(f"Проверка ZIP не пройдена: {corrupt}.")
        if output.exists():
            raise FileExistsError("Архив с таким именем уже существует. Укажите другое имя.")
        staged_zip.replace(output)
    return {"path": str(output), "products": count, "photos": len(photos), "bytes": output.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description="Переносимый архив Виано Мебель")
    parser.add_argument("--app-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.app_root / "exports" / (
        "IBD-" + datetime.now().strftime("%Y-%m-%d-%H%M%S-%f") + ".zip")
    try:
        result = build_archive(args.app_root, output)
        print(f"Готово: {result['path']}", flush=True)
        print("На другом компьютере распакуйте ZIP и запустите IBD/Start.cmd.", flush=True)
        return 0
    except (OSError, ValueError, sqlite3.Error, TimeoutError) as error:
        print(f"Не удалось создать архив: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
