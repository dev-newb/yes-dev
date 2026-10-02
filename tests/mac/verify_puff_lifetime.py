"""Verify visible native puff objects survive frame-pool drains and close on EOF.

Runs the production serve loop with ten real notifications and records only
their native image/window lifetime. Use a new result directory outside Git.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def child(out):
    os.environ["YESDEV_DATA_DIR"] = str(out / "app-data")
    import puffs_mac as puffs

    stats = {"created": 0, "destroyed": 0, "live_frame_checks": 0,
             "visible_frame_checks": 0, "image_checks": 0, "errors": []}

    class CheckedPuff(puffs._Puff):
        def __init__(self):
            super().__init__()
            stats["created"] += 1

        def step(self, now):
            try:
                # Every later frame follows the preceding frame's pool drain.
                # A dangling native view/image/window would fail here or crash.
                stats["live_frame_checks"] += 1
                if not self.win.isVisible():
                    raise AssertionError("live notification became invisible")
                stats["visible_frame_checks"] += 1
                view = self.win.contentView()
                if view is None or view.image() is None or not view.image().isValid():
                    raise AssertionError("live notification lost its native image")
                stats["image_checks"] += 1
            except Exception as exc:
                stats["errors"].append(repr(exc))
                raise
            return super().step(now)

        def destroy(self):
            win = self.win
            super().destroy()
            if win is not None:
                stats["destroyed"] += 1
                if win.isVisible():
                    stats["errors"].append("destroyed notification is still visible")

    puffs._Puff = CheckedPuff
    puffs.serve()
    stats["passed"] = (stats["created"] == stats["destroyed"] == 10
                       and stats["live_frame_checks"] > 100
                       and not stats["errors"])
    (out / "result.json").write_text(json.dumps(stats, indent=2) + "\n")
    print(json.dumps(stats, indent=2))
    return 0 if stats["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if args.child:
        return child(args.out)
    args.out.mkdir(parents=True, exist_ok=False)
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                             "--out", str(args.out), "--child"],
                            input="1\n3\n6\n", text=True, capture_output=True, timeout=20)
    (args.out / "stdout.log").write_text(result.stdout)
    (args.out / "stderr.log").write_text(result.stderr)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
