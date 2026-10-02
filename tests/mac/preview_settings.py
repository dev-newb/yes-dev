"""Exercise the real settings window with isolated storage and no helpers."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from AppKit import NSApplication, NSApplicationActivationPolicyRegular
from Foundation import NSDate, NSRunLoop
from settings_mac import SettingsWindow
from settings_model import DEFAULTS, normalize, save_atomic


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    class Preview:
        cfg = normalize(json.loads(args.output.read_text())) if args.output.exists() else {
            **DEFAULTS, "enabled": False, "relay_profile": "/tmp"}
        def apply_settings(self, values, login):
            save_atomic(args.output, values)
            self.cfg = dict(values)
        def relay_status_text(self):
            return "Preview — no approval engine or relay is running"
        def on_grant_accessibility(self, _):
            pass  # Preview never changes system permissions.
    application = NSApplication.sharedApplication()
    application.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    application.finishLaunching()
    window = SettingsWindow(Preview())
    window.show()
    while window.window.isVisible():
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.05))


if __name__ == "__main__":
    main()
