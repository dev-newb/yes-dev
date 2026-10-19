"""Build a portable ZIP with a private, isolated Windows CPython runtime."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import zipfile


APP_FILES = ("yes_dev.pyw", "windows_runtime.py", "watcher_supervisor.py", "watcher.ps1", "puffs.py", "burst_dialog.py")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command, **kwargs):
    return subprocess.check_output(command, text=True, encoding="utf-8", **kwargs).strip()


def build(version, output, allow_dirty=False):
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise RuntimeError("Build with a full 64-bit Windows CPython installation")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Use a numeric major.minor.patch version")
    repo = Path(__file__).resolve().parents[1]
    status = run(["git", "status", "--porcelain"], cwd=repo)
    if status and not allow_dirty:
        raise RuntimeError("Build from a clean commit; use --allow-dirty only for local QA")
    commit = run(["git", "rev-parse", "HEAD"], cwd=repo)
    base = Path(sys.base_prefix)
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    for required in (base / "pythonw.exe", base / "Lib", base / "DLLs", base / "tcl", compiler):
        if not required.exists():
            raise FileNotFoundError(required)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"Use a new output directory: {output}")
    output.mkdir(parents=True)
    package = output / f"YesDev-{version}-windows-x64"
    app = package / "app"
    runtime = package / "runtime"
    app.mkdir(parents=True)
    runtime.mkdir()
    for name in APP_FILES:
        shutil.copy2(repo / name, app / name)
    shutil.copy2(repo / "packaging/verify_runtime.py", app / "verify_runtime.py")
    for path in sorted(base.iterdir()):
        if path.is_file() and (path.name in ("python.exe", "pythonw.exe", "LICENSE.txt") or path.suffix.lower() == ".dll"):
            shutil.copy2(path, runtime / path.name)
    for directory in ("Lib", "DLLs", "tcl"):
        ignored = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "site-packages", "test", "tests", "ensurepip", "idlelib", "turtledemo")
        shutil.copytree(base / directory, runtime / directory, ignore=ignored)
    major, minor = sys.version_info[:2]
    pth = runtime / f"python{major}{minor}._pth"
    pth.write_text(".\nLib\nDLLs\nLib\\site-packages\n..\\app\nimport site\n", encoding="utf-8")
    dependencies = runtime / "Lib/site-packages"
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile", "--no-user",
                    "--only-binary=:all:", "--target", str(dependencies), "-r", str(repo / "packaging/windows-requirements.txt")], check=True)
    license_dir = package / "licenses"
    license_dir.mkdir()
    shutil.copy2(repo / "LICENSE", license_dir / "YesDev-MIT.txt")
    shutil.copy2(base / "LICENSE.txt", license_dir / "Python-LICENSE.txt")
    for distribution in dependencies.glob("*.dist-info"):
        target = license_dir / distribution.name
        target.mkdir()
        for path in distribution.rglob("*"):
            if path.is_file() and ("license" in path.name.lower() or "copying" in path.name.lower()):
                destination = target / path.relative_to(distribution)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
    shutil.copy2(repo / "LICENSE", package / "LICENSE.txt")
    shutil.copy2(repo / "packaging/PORTABLE-README.txt", package / "README.txt")
    shutil.copy2(repo / "packaging/THIRD-PARTY.txt", package / "THIRD-PARTY.txt")
    source = output / "Launcher.cs"
    source.write_text((repo / "packaging/Launcher.cs").read_text().replace("__VERSION__", version), encoding="utf-8")
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/r:System.Windows.Forms.dll",
                    "/out:" + str(package / "YesDev.exe"), str(source)], check=True)
    # The bundled interpreter must resolve all imports inside the extracted ZIP.
    env = dict(os.environ)
    env["PATH"] = str(Path(os.environ["WINDIR"]) / "System32")
    env["PYTHONHOME"] = str(output / "nonexistent-python-home")
    env["PYTHONPATH"] = str(output / "nonexistent-python-path")
    subprocess.run([str(package / "YesDev.exe"), "--check-runtime"], env=env, check=True, timeout=30)
    check = json.loads((package / "runtime-check.json").read_text())
    if not check["passed"] or not check["isolated"]:
        raise RuntimeError(check)
    versions = check["dependencies"]
    (output / "build-runtime-check.json").write_text(json.dumps(check, indent=2), encoding="utf-8")
    (package / "runtime-check.json").unlink()
    manifest = {"version": version, "commit": commit, "dirty_build": bool(status), "platform": "windows-x64",
                "built_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
                "dependencies": versions, "files": {}}
    for path in sorted(package.rglob("*")):
        if path.is_file():
            manifest["files"][path.relative_to(package).as_posix()] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    (package / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    archive = output / (package.name + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(output))
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip():
            raise RuntimeError("ZIP CRC verification failed")
    (output / "SHA256SUMS.txt").write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    print(json.dumps({"zip": str(archive), "sha256": sha256(archive), "bytes": archive.stat().st_size,
                      "commit": commit, "files": len(manifest["files"]), "runtime_check": check["passed"]}, indent=2))
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    build(args.version, args.output, args.allow_dirty)
