"""Code-only updates: validate a release, preserve data, retain a rollback copy."""
from __future__ import annotations
import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

APP = "viano-furniture-update"
DEFAULT_SOURCE = "https://github.com/Michail-S1/viano-mebel/releases/latest/download/latest.json"
ROOT_FILES = {"server.py", "Start.cmd", "Start.ps1", "Stop.ps1", "Update.cmd", "Update.ps1",
              "Create-Archive.cmd", "Create-Archive.ps1", "Create-Update.cmd", "Create-Update.ps1",
              "README.md", "requirements.txt", "PROJECT_PROMPT.md", "AGENTS.md", "UPDATES.md", "AUTONOMY.md"}
CODE_DIRS = {"backend", "static", "vendor", "launcher"}
MAX_ZIP = 25 * 1024 * 1024
MAX_EXPANDED = 80 * 1024 * 1024

def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Некорректная версия обновления.")
    return tuple(int(part) for part in value.split("."))

def allowed_name(name):
    if not isinstance(name, str) or "\\" in name or ":" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    if str(path) != name or any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in path.parts):
        return False
    if any(re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p, re.I) for p in path.parts):
        return False
    if "__pycache__" in path.parts or path.suffix.lower() in (".pyc", ".pyo") or any(p.startswith(".") for p in path.parts):
        return False
    return name in ROOT_FILES or (len(path.parts) > 1 and path.parts[0] in CODE_DIRS)

def configured_source(root):
    path = Path(root) / "update-source.json"
    if not path.exists():
        return DEFAULT_SOURCE
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("source", ""), str):
        raise ValueError("Некорректный файл источника обновлений.")
    return value.get("source") or DEFAULT_SOURCE

def validate_source(source):
    source = str(source or "").strip()
    if not source:
        raise ValueError("Укажите адрес источника обновлений или путь к latest.json.")
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme:
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
            # A Windows drive letter is a path, not a URI.
            if not (len(parsed.scheme) == 1 and source[1:3] in (":\\", ":/")):
                raise ValueError("Используйте HTTPS-адрес latest.json или полный путь к файлу.")
            return str(Path(source).resolve())
        return source
    return str(Path(source).resolve())

class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise ValueError("Источник перенаправил загрузку на небезопасный адрес.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def read_source(source, limit):
    if source.startswith("https://"):
        opener = urllib.request.build_opener(HTTPSRedirect())
        request = urllib.request.Request(source, headers={"User-Agent": "Viano-Furniture-Updater/1"})
        with opener.open(request, timeout=20) as response:
            data = response.read(limit + 1)
    else:
        with Path(source).open("rb") as stream:
            data = stream.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError("Файл обновления пустой или превышает допустимый размер.")
    return data

def release(source, current):
    source = validate_source(source)
    try:
        value = json.loads(read_source(source, 256 * 1024))
    except (OSError, urllib.error.URLError) as error:
        raise ValueError("Не удалось получить обновление. Проверьте адрес и подключение.") from error
    if not isinstance(value, dict) or value.get("app") != APP:
        raise ValueError("Источник не содержит обновление Виано Мебель.")
    version_tuple(value.get("version"))
    if not isinstance(value.get("sha256"), str) or not re.fullmatch("[a-f0-9]{64}", value["sha256"]):
        raise ValueError("В обновлении отсутствует контрольная сумма.")
    package = value.get("url", "")
    if not isinstance(package, str) or not package or len(package) > 2000:
        raise ValueError("В обновлении отсутствует адрес пакета.")
    if source.startswith("https://"):
        package = validate_source(urllib.parse.urljoin(source, package))
        if not package.startswith("https://"):
            raise ValueError("Некорректный адрес пакета.")
    else:
        parsed = urllib.parse.urlparse(package)
        if parsed.scheme == "https":
            package = validate_source(package)
        else:
            if parsed.scheme or "\\" in package or ":" in package or PurePosixPath(package).is_absolute() or ".." in PurePosixPath(package).parts:
                raise ValueError("Некорректный путь к пакету.")
            package = str((Path(source).parent / package).resolve())
    return {**value, "package": package, "source": source,
            "available": version_tuple(value["version"]) > version_tuple(current)}

def source_version(data):
    tree = ast.parse(data.decode("utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError("В пакете отсутствует версия программы.")

def unpack_package(data, manifest, staging):
    if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise ValueError("Контрольная сумма обновления не совпала. Установка отменена.")
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > 2000 or sum(info.file_size for info in archive.infolist()) > MAX_EXPANDED:
            raise ValueError("Пакет обновления слишком большой.")
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if info.orig_filename != name or not allowed_name(name) or name in files or ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("Пакет содержит запрещённый файл: " + name)
            content = archive.read(info)
            if name.endswith(".py"):
                compile(content, name, "exec")
            files[name] = content
    for required in ("server.py", "static/app.js", "static/index.html", "backend/importer.py", "launcher/update.py"):
        if required not in files:
            raise ValueError("В обновлении отсутствует " + required)
    if source_version(files["server.py"]) != manifest["version"]:
        raise ValueError("Версия пакета не соответствует описанию.")
    for name, content in files.items():
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return sorted(files)

def prepare(root, manifest):
    runtime = root / ".runtime"
    runtime.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="update-", dir=runtime))
    try:
        data = read_source(manifest["package"], MAX_ZIP)
        names = unpack_package(data, manifest, staging / "files")
        job = {key: manifest[key] for key in ("version", "sha256")}
        job["files"] = names
        (staging / "job.json").write_text(json.dumps(job), encoding="utf-8")
        # Keep an independent copy of the running updater while its source is replaced.
        shutil.copy2(Path(__file__), staging / "worker.py")
        return staging
    except Exception:
        shutil.rmtree(staging)
        raise

def install(root, staging):
    root, staging = root.resolve(), staging.resolve()
    if not staging.is_relative_to(root / ".runtime"):
        raise ValueError("Некорректная папка подготовленного обновления.")
    job = json.loads((staging / "job.json").read_text(encoding="utf-8"))
    names = job["files"]
    if not names or len(names) != len(set(names)) or not all(allowed_name(name) for name in names):
        raise ValueError("Некорректный список файлов обновления.")
    backup = root / ".runtime" / ("before-update-" + time.strftime("%Y%m%d-%H%M%S") + "-" + staging.name)
    backup.mkdir()
    old = {}
    for name in names:
        target = root / name
        if not target.resolve().is_relative_to(root) or target.is_symlink():
            raise ValueError("Файл программы связан с внешней папкой.")
        old[name] = target.exists()
        if target.exists():
            destination = backup / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, destination)
    changed = []
    try:
        for name in names:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            changed.append(name)
            # Replace a file atomically; no data/, photo or configuration is touched.
            temporary = target.with_name(target.name + ".update-tmp")
            shutil.copyfile(staging / "files" / name, temporary)
            os.replace(temporary, target)
    except Exception:
        for name in reversed(changed):
            target = root / name
            target.with_name(target.name + ".update-tmp").unlink(missing_ok=True)
            if old[name]:
                shutil.copy2(backup / name, target)
            else:
                target.unlink(missing_ok=True)
        raise
    (backup / "rollback.json").write_text(json.dumps({"files": names, "existed": old}), encoding="utf-8")
    return backup

def restore_code(root, backup):
    saved = json.loads((backup / "rollback.json").read_text(encoding="utf-8"))
    for name in saved["files"]:
        target = root / name
        if saved["existed"][name]:
            shutil.copy2(backup / name, target)
        else:
            target.unlink(missing_ok=True)

def start_server(root, data_dir, port, log):
    return subprocess.Popen([sys.executable, "-E", "-s", "-X", "utf8", str(root / "server.py"),
                             "--data-dir", str(data_dir), "--port", str(port), "--no-browser"],
                            cwd=root, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

def wait_ready(port, process, expected=None):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1) as response:
                state = json.load(response)
            if expected is None or state.get("version") == expected:
                return
        except Exception:
            pass
        if process.poll() is not None:
            break
        time.sleep(.25)
    raise ValueError("Каталог не запустился после обновления.")

def run_worker(root, staging, parent_pid, data_dir, port):
    status = root / ".runtime" / "update-status.json"
    def report(state, message, pid=None):
        status.write_text(json.dumps({"status": state, "message": message, "pid": pid}, ensure_ascii=False), encoding="utf-8")
    # Parent has already authorized this release and initiated graceful shutdown.
    time.sleep(.5)
    if os.name == "nt" and parent_pid:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, parent_pid)
        if handle:
            try:
                result = kernel.WaitForSingleObject(handle, 30000)
                if result != 0:
                    report("error", "Каталог занят. Закройте импорт и повторите обновление.")
                    return
            finally:
                kernel.CloseHandle(handle)
    backup = None
    process = None
    with (root / ".runtime" / "update-run.log").open("ab") as log:
        try:
            backup = install(root, staging)
            process = start_server(root, data_dir, port, log)
            expected = json.loads((staging / "job.json").read_text(encoding="utf-8"))["version"]
            wait_ready(port, process, expected)
            report("complete", "Обновление установлено. База и фотографии сохранены.", process.pid)
        except Exception as error:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            if backup:
                restore_code(root, backup)
            previous = start_server(root, data_dir, port, log)
            try: wait_ready(port, previous)
            except Exception: pass
            report("error", "Обновление отменено, предыдущий код восстановлен. " + str(error), previous.pid)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--worker")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--data-dir")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--source")
    args = parser.parse_args()
    root = Path(args.app_root).resolve()
    if args.worker:
        run_worker(root, Path(args.worker), args.parent_pid, Path(args.data_dir), args.port)
        return
    current = source_version((root / "server.py").read_bytes())
    source = args.source or configured_source(root)
    manifest = release(source, current)
    if not manifest["available"]:
        print("Установлена актуальная версия: " + current)
        return
    # Prefer the running UI so it stops its own process, instead of searching other Python processes.
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{args.port}/api/update/install",
                  data=json.dumps({"source": source}).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.load(response)
        print(result["message"])
    except urllib.error.HTTPError:
        raise ValueError("Обновите этот компьютер до версии с поддержкой Update.cmd.")
    except urllib.error.URLError:
        staging = prepare(root, manifest)
        run_worker(root, staging, 0, root / "data", args.port)
        status = json.loads((root / ".runtime" / "update-status.json").read_text(encoding="utf-8"))
        print(status["message"])
        if status["status"] != "complete":
            raise ValueError("Обновление не установлено.")

if __name__ == "__main__":
    main()
