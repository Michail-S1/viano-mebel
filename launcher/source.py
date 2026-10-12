"""Prepare a clean GitHub checkout with an explicit file list, without user data."""
import argparse
import shutil
import re
from pathlib import Path
try:
    from .update import ROOT_FILES, CODE_DIRS, allowed_name
    from .release import walk_files
except ImportError:
    from update import ROOT_FILES, CODE_DIRS, allowed_name
    from release import walk_files

WORKFLOWS=("ci.yml","release.yml")
def source_files(root):
    root=Path(root).resolve()
    for name in sorted(ROOT_FILES|{".gitignore",".gitattributes","GITHUB.md"}):
        path=root/name
        if path.is_file(): yield path,name
    for directory in sorted(CODE_DIRS):
        for path in walk_files(root/directory):
            name=path.relative_to(root).as_posix()
            if path.is_file() and allowed_name(name) and not name.startswith("vendor/bin/"): yield path,name
    for path in walk_files(root/"tests"):
        name=path.relative_to(root).as_posix()
        if path.is_file() and "__pycache__" not in path.parts and path.suffix in (".py",".ps1"):
            yield path,name
    for filename in WORKFLOWS:
        path=root/".github/workflows"/filename
        if path.is_file(): yield path,".github/workflows/"+filename

def prepare_source(root,destination):
    root_aliases = {str(Path(root).absolute()), str(Path(root).resolve())}
    root,destination=Path(root).resolve(),Path(destination).resolve()
    if destination==root or destination.exists():
        raise ValueError("Укажите новую пустую папку для исходного кода.")
    inputs=list(source_files(root))
    for path,name in inputs:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Обнаружена ссылка за пределы проекта: "+name)
    destination.mkdir(parents=True)
    for path,name in inputs:
        target=destination/name
        target.parent.mkdir(parents=True,exist_ok=True)
        if path.suffix==".md":
            text=path.read_text(encoding="utf-8-sig")
            for alias in sorted(root_aliases, key=len, reverse=True):
                for spelling in (alias, alias.replace("\\", "/")):
                    text=re.sub(re.escape(spelling), lambda match: "папка проекта", text, flags=re.IGNORECASE)
            if name=="README.md":
                if "## Стартовый каталог" in text:
                    before,after=text.split("## Стартовый каталог",1)
                    general=after.partition("Поиск по размерам:")[2]
                    text=before+("\nПоиск по размерам:"+general if general else "")
                text=text.replace("На этом компьютере уже найден подходящий Python Codex. ", "")
            target.write_text(text,encoding="utf-8")
        else: shutil.copy2(path,target)
    return [name for path,name in inputs]

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--app-root",required=True)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    names=prepare_source(args.app_root,args.output)
    print(f"Подготовлено {len(names)} файлов программы без личных данных.")
