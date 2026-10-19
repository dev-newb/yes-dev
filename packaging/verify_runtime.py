"""Write a portable runtime check without starting the tray or approving prompts."""
import ctypes
import importlib.util
import json
from pathlib import Path
import ssl
import sys
import traceback


def main():
    root = Path(__file__).resolve().parents[1]
    report = {"passed": False, "python": sys.version, "executable": sys.executable}
    try:
        import pystray
        from PIL import Image, ImageTk
        import tkinter
        import puffs
        import windows_runtime
        import watcher_supervisor
        from importlib.metadata import version
        runtime = root / "runtime"
        for path in sys.path:
            assert Path(path).resolve().is_relative_to(root), path
        assert Path(sys.executable).resolve().is_relative_to(runtime)
        image = Image.new("RGBA", (16, 16), (0, 100, 200, 255))
        assert image.getpixel((0, 0)) == (0, 100, 200, 255)
        tcl = tkinter.Tcl()
        report["tcl"] = tcl.call("info", "patchlevel")
        for name in ("yes_dev.pyw", "watcher_supervisor.py", "windows_runtime.py", "puffs.py", "burst_dialog.py"):
            compile((root / "app" / name).read_text(encoding="utf-8-sig"), name, "exec")
        report.update(passed=True, dependencies={name: version(name) for name in ("pystray", "pillow", "six")},
                      sys_path=sys.path, isolated=bool(sys.flags.isolated), openssl=ssl.OPENSSL_VERSION)
    except BaseException:
        report["error"] = traceback.format_exc()
    (root / "runtime-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
