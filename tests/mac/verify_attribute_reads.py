"""Compare native AX read values/errors on a disposable Chrome, without clicks.

Only attribute names, types, and equality results are saved; values such as
window titles are never recorded. Results must be outside Git.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-pid", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    command = shlex.split(subprocess.check_output(
        ["ps", "-p", str(args.browser_pid), "-o", "command="], text=True))
    # ps does not quote the spaces in the executable name. Check the complete
    # command prefix as well as the exact disposable-profile argument.
    command_text = subprocess.check_output(
        ["ps", "-p", str(args.browser_pid), "-o", "command="], text=True).strip()
    assert command_text.startswith("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome ")
    profile_arg = next(value for value in command if value.startswith("--user-data-dir="))
    profile = Path(profile_arg.split("=", 1)[1])
    assert profile.resolve().is_relative_to(Path("/tmp").resolve()) and profile.name.startswith("yesdev-")
    args.out.mkdir(parents=True, exist_ok=False)

    import objc
    from ApplicationServices import AXUIElementCreateApplication, AXUIElementCopyAttributeValue
    from CoreFoundation import CFEqual
    from watcher_mac import _copy_attribute

    app = AXUIElementCreateApplication(args.browser_pid)
    records = []

    def check(element, scope, name):
        with objc.autorelease_pool():
            old_error, old = AXUIElementCopyAttributeValue(element, name, None)
            new_error, new = _copy_attribute(element, name)
        # Compare after the temporary result array and the local pool are gone.
        # Native values used by the caller must still have valid ownership.
        equal = old_error == new_error
        if equal and old_error == 0:
            equal = old is new if old is None or new is None else bool(CFEqual(old, new))
        records.append({"scope": scope, "attribute": name, "error": new_error,
                        "type": type(new).__name__, "equal_after_pool_drain": equal})
        return new

    for name in ("AXRole", "AXHidden", "AXYesDevUnsupportedAttribute"):
        check(app, "application", name)
    windows = check(app, "application", "AXWindows")
    assert windows, "Disposable Chrome must have a window"
    for index, window in enumerate(windows):
        for name in ("AXRole", "AXSubrole", "AXTitle", "AXPosition", "AXSize",
                     "AXMinimized", "AXFocused", "AXMain", "AXChildren", "AXSheets",
                     "AXYesDevUnsupportedAttribute"):
            check(window, f"window-{index}", name)
    result = {"checks": records, "passed": all(row["equal_after_pool_drain"] for row in records)}
    (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
