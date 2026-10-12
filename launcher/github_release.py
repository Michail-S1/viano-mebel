"""GitHub Actions release: validate tag, build a code package, publish both files together."""
import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
try:
    from .release import build_release
    from .update import source_version
except ImportError:
    from release import build_release
    from update import source_version

def release_context(root, repository, ref_name="", ref_type="branch"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
        raise ValueError("Не указан корректный GitHub-репозиторий.")
    version=source_version((Path(root)/"server.py").read_bytes())
    tag="v"+version
    if ref_type=="tag" and ref_name!=tag:
        raise ValueError("Тег должен совпадать с VERSION в server.py: "+tag)
    package="Viano-update-"+version+".zip"
    return {"version":version,"tag":tag,"repository":repository,
            "package_url":f"https://github.com/{repository}/releases/download/{tag}/{package}",
            "source_url":f"https://github.com/{repository}/releases/latest/download/latest.json"}

def gh(arguments, repository, check=True):
    result=subprocess.run(["gh",*arguments,"--repo",repository],text=True,capture_output=True,check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "GitHub не принял выпуск.")
    return result

def publish(root, context):
    if not os.environ.get("GH_TOKEN"):
        raise ValueError("Запускайте публикацию в GitHub Actions с GH_TOKEN.")
    if not shutil.which("gh"):
        raise ValueError("GitHub CLI недоступен.")
    repository=context["repository"]
    existing=gh(["release","view",context["tag"],"--json","isDraft"],repository,check=False)
    if existing.returncode==0 and not json.loads(existing.stdout)["isDraft"]:
        raise ValueError("Эта версия уже опубликована. Увеличьте VERSION; опубликованные обновления не заменяются.")
    output=Path(root)/"exports/github-release"
    package=build_release(root,output,context["package_url"])
    if existing.returncode:
        arguments=["release","create",context["tag"],"--draft","--title","Виано Мебель "+context["version"],
                   "--notes","Обновление программы. Личные данные остаются на вашем компьютере."]
        if os.environ.get("GITHUB_SHA"): arguments.extend(["--target",os.environ["GITHUB_SHA"]])
        gh(arguments,repository)
    gh(["release","upload",context["tag"],str(package),str(package.parent/"latest.json"),"--clobber"],repository)
    gh(["release","edit",context["tag"],"--draft=false","--latest"],repository)
    return context["source_url"]

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--app-root",default=".")
    parser.add_argument("--check-only",action="store_true")
    args=parser.parse_args()
    context=release_context(args.app_root,os.environ.get("GITHUB_REPOSITORY"),
                            os.environ.get("GITHUB_REF_NAME",""),os.environ.get("GITHUB_REF_TYPE","branch"))
    if args.check_only: print(json.dumps(context,ensure_ascii=False))
    else: print("Источник обновлений: "+publish(Path(args.app_root).resolve(),context))
