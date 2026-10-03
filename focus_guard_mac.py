"""Yes, Dev - macOS focus guard: hand focus back when Chrome takes it for a prompt.

Chrome activates its own window before it builds the remote-debugging consent
sheet (DevToolsConnectionDialog calls browser->GetWindow()->Activate()), and
macOS lets it: the app asking for activation has the last word unless the user
is mid-input. Nothing outside Chrome can veto that - window flags do not, and
the only things that would are a modified Chrome or a SIP-disabled Dock. What
can be done is to notice the activation the instant it happens and hand focus
straight back, while the engine approves the sheet in the background. That is a
blink, not prevention, and nothing here pretends otherwise.

Its own process, like the clouds: the tray supervises it and never touches the
accessibility APIs itself. It needs an AppKit run loop for the activation
notifications, and the Accessibility grant for the sheet check and the restore.

A restore fires only when all of these hold:

  - a watched browser just became the active app;
  - the app before it was some other regular app - not the browser, not us;
  - no mouse or keyboard input in the last QUIET_S seconds: a user who clicked
    Chrome or Cmd-Tabbed to it produced input, Chrome activating itself did not;
  - the browser is showing the consent sheet, checked a few times over a short
    window because the activation is delivered a beat before the sheet reaches
    the accessibility tree;
  - no new mouse or keyboard input arrived while that sheet check was pending.

One attempt per activation, and every step is timestamped to the millisecond so
the blink can be measured against WindowServer's own record rather than guessed.

Restore goes through Accessibility: setting AXFrontmost on the previous app asks
that app, in its own process, to activate itself, which macOS grants the way it
granted Chrome. NSRunningApplication's activate is the fallback.

Test hooks: --watch-pid watches an arbitrary process instead of the browsers, and
--no-require-sheet skips the sheet check, so the restore path can be measured
against a stand-in app that steals focus the way Chrome does, without a prompt.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from platform_mac import DATA_DIR, ensure_data_dir, is_trusted  # noqa: E402

try:
    import watcher_mac as wm   # the one definition of what the consent sheet looks like
    from AppKit import (
        NSApplication,
        NSApplicationActivateIgnoringOtherApps,
        NSApplicationActivationPolicyAccessory,
        NSApplicationActivationPolicyRegular,
        NSRunningApplication,
        NSWorkspace,
        NSWorkspaceApplicationKey,
        NSWorkspaceDidActivateApplicationNotification,
    )
    from ApplicationServices import AXUIElementCreateApplication, AXUIElementSetAttributeValue
    from Foundation import NSObject, NSTimer
    from Quartz import CGEventSourceSecondsSinceLastEventType, kCGEventSourceStateCombinedSessionState
except ImportError:
    sys.exit(
        "Yes, Dev focus guard needs pyobjc:\n"
        "    pip3 install -r requirements.txt"
    )

GUARD_LOG = DATA_DIR / "focus-guard.log"

QUIET_S = 0.5          # no user input for this long = the activation was not the user's doing
SHEET_TRIES = 6        # the sheet can lag the activation notification by a few frames
SHEET_RETRY_S = 0.03
VERIFY_AFTER_S = 0.15  # read the frontmost app back this long after the restore
kCGAnyInputEventType = 0xFFFFFFFF   # (CGEventType)~0, which pyobjc does not export


def _ms() -> str:
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"


class Guard:
    """Observer, decision and restore. Plain Python; _Bridge below is the
    NSObject that AppKit talks to, because every method on an NSObject
    subclass becomes a selector and pyobjc polices their arity."""

    def __init__(self, opts) -> None:
        self.opts = opts
        self.log_path = Path(opts.log_path)
        self.watch_pids = set(opts.watch_pid or [])
        self.bundles = set(wm.CHROME_BUNDLES) | (set(wm.EDGE_BUNDLES) if opts.include_edge else set())
        # The supervisor's pid when it passed one; see watcher_mac.Engine.
        self.parent_pid = getattr(opts, "parent_pid", 0) or os.getppid()
        self.restores = 0
        self.skips = 0
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        self.current = self._describe(front) if front is not None else None
        self.bridge = _Bridge.alloc().init()
        self.bridge.guard = self

    # -------- logging: same shape as the engine's log --------

    def log(self, message: str, level: str = "INFO") -> None:
        line = f"{_ms()} [{level}] {message}"
        print(line, flush=True)
        try:
            ensure_data_dir()
            if self.log_path.exists() and self.log_path.stat().st_size > 1_048_576:
                self.log_path.replace(self.log_path.with_name(self.log_path.name + ".1"))
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    # -------- what just became active --------

    @staticmethod
    def _describe(app) -> dict:
        return {
            "pid": int(app.processIdentifier()),
            "bundle": str(app.bundleIdentifier() or ""),
            "name": str(app.localizedName() or ""),
            "policy": int(app.activationPolicy()),
            "at": time.monotonic(),
        }

    def _watched(self, info: dict) -> bool:
        if self.watch_pids:
            return info["pid"] in self.watch_pids
        return info["bundle"] in self.bundles

    def on_activated(self, note) -> None:
        t_notified = time.monotonic()
        app = note.userInfo().get(NSWorkspaceApplicationKey)
        if app is None:
            return
        info = self._describe(app)
        prev, self.current = self.current, info
        if not self._watched(info):
            return

        who = f"{info['name']} pid={info['pid']}"
        if prev is None or prev["pid"] == info["pid"]:
            return self._skip(f"{who} activated, nothing else was in front")
        if self._watched(prev):
            return self._skip(f"{who} activated from another watched app")
        if prev["pid"] in (os.getpid(), self.parent_pid):
            return self._skip(f"{who} activated from Yes, Dev itself")
        if prev["policy"] != NSApplicationActivationPolicyRegular:
            return self._skip(f"{who} activated from a non-regular app ({prev['name']})")

        quiet = CGEventSourceSecondsSinceLastEventType(
            kCGEventSourceStateCombinedSessionState, kCGAnyInputEventType)
        if quiet < self.opts.quiet_s:
            return self._skip(f"{who} activated {quiet * 1000:.0f}ms after user input - "
                              f"treating as the user's own switch")

        checks = 0
        if self.opts.require_sheet:
            seen = False
            for checks in range(1, SHEET_TRIES + 1):
                if self._sheet_visible(info["pid"]):
                    seen = True
                    break
                time.sleep(SHEET_RETRY_S)
            if not seen:
                return self._skip(f"{who} activated with no consent sheet after "
                                  f"{checks} checks - leaving it")

        # Chrome's AX reads can block through the sheet animation. The quiet
        # value above is then stale: a click during that wait belongs to the
        # user even if the browser was already frontmost and sends no new
        # activation notification. Compare with the whole pending interval,
        # not QUIET_S, so a long AX stall cannot age that click out again.
        quiet_before_restore = CGEventSourceSecondsSinceLastEventType(
            kCGEventSourceStateCombinedSessionState, kCGAnyInputEventType)
        t_restore = time.monotonic()
        if quiet_before_restore <= t_restore - t_notified:
            return self._skip(
                f"{who} received user input while consent check was pending "
                f"({quiet_before_restore * 1000:.0f}ms ago) - leaving focus alone")

        how = self._restore(prev)
        self.restores += 1
        self.log(
            f"RESTORE {prev['name']} pid={prev['pid']} taken_by={who} "
            f"notified_at=+{(t_notified - info['at']) * 1000:.0f}ms "
            f"decided_in={(t_restore - t_notified) * 1000:.0f}ms "
            f"quiet_at_activation={quiet:.2f}s quiet_before_restore={quiet_before_restore:.2f}s "
            f"sheet_checks={checks} via={how}",
            "ACTION",
        )
        # Read the result back once the window server has had a moment.
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            VERIFY_AFTER_S, self.bridge, "verify:", {"want": prev["pid"], "since": t_restore}, False)

    def _skip(self, why: str) -> None:
        self.skips += 1
        self.log(f"skip: {why}", "AUDIT")

    # -------- the sheet check, borrowed from the engine --------

    def _sheet_visible(self, pid: int) -> bool:
        try:
            app = AXUIElementCreateApplication(pid)
            for window in wm._attr(app, "AXWindows") or []:
                candidates = (wm._attr(window, "AXSheets") or []) + (wm._attr(window, "AXChildren") or [])
                for c in candidates:
                    is_dialog = wm._is_dialog_role(c)
                    if not is_dialog:
                        continue
                    label = wm._element_label(c)
                    has_heading = wm._has_dialog_heading(c) if label == "" else False
                    if wm._is_consent_host(label, has_heading, True) and wm._is_visible(c):
                        return True
        except Exception as exc:
            self.log(f"sheet check raised {exc!r}", "AUDIT")
        return False

    # -------- the restore --------

    def _restore(self, prev: dict) -> str:
        try:
            err = AXUIElementSetAttributeValue(AXUIElementCreateApplication(prev["pid"]), "AXFrontmost", True)
            if err == 0:
                return "AXFrontmost"
            self.log(f"  AXFrontmost err={err}, falling back", "AUDIT")
        except Exception as exc:
            self.log(f"  AXFrontmost raised {exc!r}, falling back", "AUDIT")
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(prev["pid"])
        if app is None:
            return "gone"
        ok = app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        return "activate" if ok else "activate-refused"

    def verify(self, want: int, since: float) -> None:
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        got = int(front.processIdentifier()) if front is not None else -1
        name = str(front.localizedName()) if front is not None else "?"
        level = "INFO" if got == want else "WARN"
        self.log(f"  after {(time.monotonic() - since) * 1000:.0f}ms frontmost={name} pid={got} "
                 f"{'restored' if got == want else 'NOT restored'}", level)

    # -------- housekeeping --------

    def tick(self) -> None:
        if self.opts.exit_with_parent and os.getppid() != self.parent_pid:
            self.log("parent gone - exiting")
            os._exit(0)


class _Bridge(NSObject):
    """The selectors AppKit calls. Each forwards to the Guard it belongs to."""

    guard = None

    def appActivated_(self, note) -> None:
        self.guard.on_activated(note)

    def verify_(self, timer) -> None:
        info = timer.userInfo()
        self.guard.verify(int(info["want"]), float(info["since"]))

    def tick_(self, _timer) -> None:
        self.guard.tick()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Yes, Dev macOS focus guard")
    ap.add_argument("--log-path", default=str(GUARD_LOG))
    ap.add_argument("--include-edge", action="store_true")
    ap.add_argument("--quiet-s", type=float, default=QUIET_S,
                    help="seconds without user input before an activation counts as the browser's own")
    ap.add_argument("--exit-with-parent", action="store_true",
                    help="stop when the launching process goes away")
    ap.add_argument("--parent-pid", type=int, default=0,
                    help="the supervisor's pid, passed by the supervisor itself. Without it the "
                         "parent is read at startup, which is too late if the parent has "
                         "already exited: the helper then records launchd and never stops")
    ap.add_argument("--watch-pid", type=int, action="append",
                    help="(testing) watch this process instead of the browsers; repeatable")
    ap.add_argument("--no-require-sheet", dest="require_sheet", action="store_false",
                    help="(testing) restore without checking for a consent sheet")
    opts = ap.parse_args(argv)

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    guard = Guard(opts)
    if not is_trusted():
        guard.log("NOT trusted for Accessibility - the sheet check and the restore will fail "
                  "until it is granted", "ERROR")
    NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
        guard.bridge, "appActivated:", NSWorkspaceDidActivateApplicationNotification, None)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0, guard.bridge, "tick:", None, True)
    watching = (f"pids {sorted(guard.watch_pids)}" if guard.watch_pids
                else f"{len(guard.bundles)} browser bundle ids")
    guard.log(f"focus guard started (watching {watching}, quiet={opts.quiet_s}s, "
              f"require_sheet={opts.require_sheet}, pid={os.getpid()})")
    NSApplication.sharedApplication().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
