"""A stand-in for Chrome's self-activation, for measuring the focus guard.

One small window. After --delay seconds it does, --count times and --interval
seconds apart, exactly what Chrome's widget does when it shows the consent
sheet: [NSApp activateIgnoringOtherApps:YES] then [window makeKeyAndOrderFront:].
Each activation is logged to the millisecond, with the app that was frontmost
just before it, so the guard's restore can be checked against it.

No browser, no prompt, no CDP: this measures the restore path and nothing else.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSMakeRect,
    NSWindow,
    NSWindowStyleMaskTitled,
    NSWorkspace,
)
from Foundation import NSObject, NSTimer


def _ms() -> str:
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"


class Flasher(NSObject):
    window = None
    log_path = None
    remaining = 0

    def write_(self, message) -> None:
        line = f"{_ms()} {message}"
        print(line, flush=True)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def flash_(self, _timer) -> None:
        if self.remaining <= 0:
            self.write_("done")
            NSApplication.sharedApplication().terminate_(None)
            return
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        before = f"{front.localizedName()} pid={front.processIdentifier()}" if front is not None else "?"
        self.remaining -= 1
        self.write_(f"ACTIVATE #{self.remaining + 1} before={before} mono={time.monotonic():.3f}")
        app = NSApplication.sharedApplication()
        # Regular only from here: an app launched by the active app is handed
        # focus at launch, which would make it frontmost before it ever asked.
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        app.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="focus flasher")
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--log", required=True)
    opts = ap.parse_args(argv)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(80, 80, 360, 120), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
    window.setTitle_("Yes, Dev focus flasher")
    window.setReleasedWhenClosed_(False)
    window.orderFront_(None)   # on screen, but this alone does not activate the app

    f = Flasher.alloc().init()
    f.window = window
    f.log_path = opts.log
    f.remaining = opts.count
    f.write_(f"flasher started pid={__import__('os').getpid()} count={opts.count} interval={opts.interval}s delay={opts.delay}s")
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        opts.delay, f, "flash:", None, False)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        opts.interval, f, "flash:", None, True).setFireDate_(
        __import__("Foundation").NSDate.dateWithTimeIntervalSinceNow_(opts.delay + opts.interval))
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
