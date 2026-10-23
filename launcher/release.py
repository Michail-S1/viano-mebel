"""Build a small release containing code only, never a user's catalog."""
import argparse
import hashlib
import json
import os
import zipfile
from pathlib import Path
try:
    from .update import APP, ROOT_FILES, CODE_DIRS, allowed_name, source_version
except ImportError:
    from update import APP, ROOT_FILES, CODE_DIRS, allowed_name, source_version

def walk_files(directory):
    """List source files strictly: permission errors must never create incomplete packages."""
    def fail(error):
        if isinstance(error, FileNotFoundError) and Path(error.filename) == Path(directory):
            return
        raise OSError("Не удалось прочитать файлы программы: " + str(error.filename)) from error
    for parent, directories, files in os.walk(directory, onerror=fail, followlinks=False):
        for name in directories:
            if (Path(parent)/name).is_symlink():
                raise ValueError("Обнаружена ссылка в файлах программы.")
        directories[:] = sorted(name for name in directories if name != "__pycache__")
        for name in sorted(files):
            yield Path(parent)/name

def build_release(root, output, url=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    version = source_version((root / "server.py").read_bytes())
    output.mkdir(parents=True, exist_ok=True)
    target = output / ("Viano-update-" + version + ".zip")
    if target.exists():
        raise FileExistsError("Пакет этой версии уже существует.")
    inputs = {}
    for name in sorted(ROOT_FILES):
        path = root / name
        if path.is_file(): inputs[name] = path
    for directory in sorted(CODE_DIRS):
        for path in walk_files(root / directory):
            name = path.relative_to(root).as_posix()
            if path.is_file() and allowed_name(name):
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise ValueError("Обнаружена внешняя ссылка.")
                inputs[name] = path
    for required in ("server.py", "Update.cmd", "launcher/update.py", "static/app.js", "backend/importer.py"):
        if required not in inputs:
            raise ValueError("Не хватает файла " + required)
    temporary = target.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, path in inputs.items(): archive.write(path, name)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip(): raise ValueError("Не пройдена проверка пакета.")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    manifest = {"app": APP, "version": version, "url": url or target.name,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "notes": "Импорт всех листов многофабричных книг и выгрузок остатков, автоматические столбцы и фабрика БТС для многострочных заголовков 1С. Новая панель «Подборка комплекта»: количества, удаление, очистка с отменой и текущие итоги. Числовые названия модулей получают контекст или выводятся на проверку."}
    (output / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return target

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--output")
    parser.add_argument("--url")
    args = parser.parse_args()
    result = build_release(args.app_root, args.output or (Path(args.app_root) / "exports/update"), args.url)
    print("Пакет обновления: " + str(result))
    print("Описание версии: " + str(result.parent / "latest.json"))
