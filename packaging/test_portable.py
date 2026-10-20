"""Extract a portable release, verify its files, and check its private runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--extract-to", type=Path, required=True)
    args = parser.parse_args()
    destination = args.extract_to.resolve()
    if destination.exists():
        raise FileExistsError("Use a new extraction directory")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(args.archive) as bundle:
        if bundle.testzip():
            raise RuntimeError("ZIP CRC verification failed")
        for name in bundle.namelist():
            if not (destination / name).resolve().is_relative_to(destination):
                raise ValueError(f"Archive path leaves its root: {name}")
        bundle.extractall(destination)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    assert len(roots) == 1
    root = roots[0]
    manifest = json.loads((root / "release-manifest.json").read_text())
    for name, record in manifest["files"].items():
        path = root / name
        assert path.stat().st_size == record["bytes"], name
        with path.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == record["sha256"], name
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert actual == set(manifest["files"]) | {"release-manifest.json"}
    assert {"app/cdp_relay.py", "app/relay_windows.py"}.issubset(actual)
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in actual)
    assert not any(Path(name).name in ("config.json", "state.json", "credentials.json") for name in actual)
    env = dict(os.environ, PYTHONHOME=str(destination / "bad-home"), PYTHONPATH=str(destination / "bad-path"))
    env["PATH"] = str(Path(os.environ["WINDIR"]) / "System32")
    subprocess.run([str(root / "YesDev.exe"), "--check-runtime"], env=env, check=True, timeout=30)
    check = json.loads((root / "runtime-check.json").read_text())
    assert check["passed"] and check["isolated"], check
    assert check["dependencies"]["websockets"] == "15.0.1", check
    print(json.dumps({"passed": True, "package": str(root), "files_verified": len(manifest["files"]),
                      "commit": manifest["commit"], "python": check["python"], "dependencies": check["dependencies"]}, indent=2))


if __name__ == "__main__":
    main()
