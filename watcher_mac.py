"""Yes, Dev engine for macOS: auto-approves Chrome's "Allow remote debugging?"
consent dialog through the Accessibility API.

This is the macOS counterpart of watcher.ps1. The contract with the tray is the
only thing that must stay identical: append a line containing `[ACTION]` to the
log for each approval, in the existing format

    2026-08-27 16:11:28.644 [ACTION]   APPROVED via AXPress

and the counter, the clouds and the burst guard in the tray all work unchanged.
`[ACTION]` is only written after the sheet is gone - and "gone" is judged by the
identity of the AX refs that were pressed (a dismissed sheet's refs answer every
read with AXError -25202), never by whether something still sits at its
coordinates: Chrome queues these prompts when several CDP clients connect at
once and draws the next one exactly where the last one was. A sheet that
survives AXPress falls through to a CGEvent click at the Allow button's center.

The dialog's shape in the accessibility tree, confirmed against a live prompt on
Chrome 152, is an AXSheet on the browser window wrapping an alert:

    AXWindow  "<tab title> - Google Chrome"
      AXSheet  "Allow remote debugging?"
        AXGroup / AXSubrole=AXApplicationAlertDialog
          AXButton  "Turn off in settings" | "Cancel" | "Allow"

Two things that title alone will not tell you: the same title is also carried
by the Window menu's AXMenuItem and by an AXHeading inside the dialog, so the
role has to match too - though find_dialog_hosts() only ever reads a Chrome
process's AXWindows and their AXSheets, so neither is actually reachable; the
role check is defense in depth, not the only thing standing between it and a
false positive. That scope is also the perf fix: earlier builds walked every
AXChild up to twelve levels deep, which on a loaded page means the page's own
AX-exposed DOM, on every single poll (confirmed at ~150-160ms/pid via
--diagnostics). The dialog only ever lives two AX reads from the app root, so
that's all this looks at now. And Chrome sets no AXIdentifier here, so dedupe
keys on position and size. `docs/mac/ax_probe.py` re-dumps the tree if a
future Chrome moves it.

Runs standalone:

    python3 watcher_mac.py --observe        # log dialogs and buttons, never click
    python3 watcher_mac.py --once           # one sweep, then exit (prints findings)
    python3 watcher_mac.py                   # the real loop, clicking Allow
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from platform_mac import DATA_DIR, LOG_PATH, ensure_data_dir, is_trusted
except ImportError:  # allow running from another cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from platform_mac import DATA_DIR, LOG_PATH, ensure_data_dir, is_trusted

try:
    from ApplicationServices import (
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        AXUIElementPerformAction,
        AXValueGetValue,
        kAXErrorInvalidUIElement,
        kAXErrorSuccess,
        kAXValueCGPointType,
        kAXValueCGSizeType,
    )
    from AppKit import NSRunningApplication, NSWorkspace
except ImportError:
    sys.exit(
        "Yes, Dev macOS engine needs pyobjc:\n"
        "    pip3 install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices"
    )

try:
    from Quartz import (
        CGEventCreate,
        CGEventCreateMouseEvent,
        CGEventGetLocation,
        CGEventPost,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGEventMouseMoved,
        kCGHIDEventTap,
        kCGMouseButtonLeft,
    )
    _HAVE_QUARTZ = True
except ImportError:
    _HAVE_QUARTZ = False

# Chrome variants. Edge is Chromium too and shows the same dialog; the tray adds
# it to the bundle set when "Include Microsoft Edge" is on.
CHROME_BUNDLES = ("com.google.Chrome", "com.google.Chrome.beta",
                  "com.google.Chrome.canary", "com.google.Chrome.dev")
EDGE_BUNDLES = ("com.microsoft.edgemac", "com.microsoft.edgemac.beta")

# Match the dialog by title, the approve button by an anchored label. The anchor
# is not fussiness: web pages carry their own "Allow" buttons (permission chips,
# ad blockers) and a loose match clicks them; it also keeps us off "Turn off in
# settings", which disables the whole feature.
DIALOG_PATTERN = re.compile(r"^allow remote debugging\??$", re.I)
APPROVE_PATTERN = re.compile(r"^(allow|approve)$", re.I)

POLL_MS_DEFAULT = 250
DEDUPE_SECONDS = 2.0     # don't re-press one dialog mid-teardown ...
DEDUPE_MAX = 400         # ... but cap the memory so a long run can't grow forever
# ponytail: fixed sleep, not AX notification. Bump if Chrome teardown gets slower.
VERIFY_WAIT_S = 0.5


def _button_center(pos: tuple[float, float], size: tuple[float, float]) -> tuple[float, float]:
    """Return the midpoint of an AX button frame in global top-left points."""
    return (pos[0] + size[0] / 2.0, pos[1] + size[1] / 2.0)


# ponytail: click-point only; live AX/CGEvent needs a real Chrome sheet.
assert _button_center((3968.0, 657.0), (76.0, 36.0)) == (4006.0, 675.0)


def _attr(element, name):
    """One AX attribute, or None. Every read can fail (permission, torn-down
    element); callers treat None as 'not present' rather than crashing."""
    try:
        err, value = AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return None
    return value if err == kAXErrorSuccess else None


def _ref_alive(element) -> bool | None:
    """Whether an AX ref still points at a live node - the one thing _attr's
    None cannot say, since it also covers a merely empty attribute.

    True: the node answered. False: kAXErrorInvalidUIElement (-25202), which is
    what a dismissed sheet and every button under it return on any read once
    Chrome tears the dialog down, and which a live node never returns. None:
    no answer either way (Chrome busy, AX unreachable) - proof of nothing."""
    try:
        err, _ = AXUIElementCopyAttributeValue(element, "AXRole", None)
    except Exception:
        return None
    if err == kAXErrorSuccess:
        return True
    if err == kAXErrorInvalidUIElement:
        return False
    return None


def _children(element):
    """A window's dialog can hang off AXSheets as readily as AXChildren, and the
    probe may reveal AXWindows nesting too - walk all three the way ax_probe does."""
    out = []
    for bucket in ("AXChildren", "AXSheets", "AXWindows"):
        kids = _attr(element, bucket)
        if kids:
            out.extend(kids)
    return out


def _ax_point(element, name="AXPosition"):
    """Unpack a CGPoint-typed AX attribute to (x, y). A pyobjc AXValue does not
    expose .pointValue(); it must go through AXValueGetValue, which is why the
    first cut of the dedupe key silently fell back to object identity."""
    val = _attr(element, name)
    if val is None:
        return None
    ok, pt = AXValueGetValue(val, kAXValueCGPointType, None)
    return (pt.x, pt.y) if ok else None


def _ax_size(element, name="AXSize"):
    val = _attr(element, name)
    if val is None:
        return None
    ok, sz = AXValueGetValue(val, kAXValueCGSizeType, None)
    return (sz.width, sz.height) if ok else None


# The consent prompt shows up in the tree three ways: the AXSheet on the browser
# window, the AXGroup/AXApplicationAlertDialog it wraps, and - a false positive -
# the Window menu's AXMenuItem bearing the same title (plus the AXHeading inside).
# Only the first two are real dialog containers with the Allow button beneath them.
_DIALOG_ROLES = {"AXSheet", "AXDialog"}
_DIALOG_SUBROLES = {"AXApplicationAlertDialog", "AXDialog", "AXSystemDialog"}


def _is_dialog_role(element) -> bool:
    if (_attr(element, "AXRole") or "") in _DIALOG_ROLES:
        return True
    return (_attr(element, "AXSubrole") or "") in _DIALOG_SUBROLES


def _is_visible(element) -> bool:
    """Skip hidden/offscreen elements: a dismissed dialog lingers briefly in the
    tree and would otherwise be approved a second time."""
    if _attr(element, "AXHidden"):
        return False
    # A size of zero is the other tell-tale of a torn-down element.
    size = _ax_size(element)
    if size is not None and (size[0] <= 0 or size[1] <= 0):
        return False
    return True


class Engine:
    def __init__(self, observe: bool = False, poll_ms: int = POLL_MS_DEFAULT,
                 include_edge: bool = False, log_path: Path = LOG_PATH,
                 exit_with_parent: bool = False, diagnostics: bool = False) -> None:
        self.observe = observe
        self.poll_s = max(0.05, poll_ms / 1000.0)
        self.bundles = CHROME_BUNDLES + (EDGE_BUNDLES if include_edge else ())
        self.log_path = Path(log_path)
        self.approved = 0
        self.exit_with_parent = exit_with_parent
        self.diagnostics = diagnostics
        self._parent_pid = os.getppid()
        self._seen: dict[str, float] = {}    # dedupe key -> last-press wall clock
        self._next_diagnostic_at = 0.0

    # -------- logging: byte-for-byte the format the tray parses --------

    def log(self, message: str, level: str = "INFO") -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"
        line = f"{stamp} [{level}] {message}"
        print(line, flush=True)
        try:
            ensure_data_dir()
            # One generation, rolled at 1MB - an engine that runs for months
            # must not fill the disk. Matches watcher.ps1.
            if self.log_path.exists() and self.log_path.stat().st_size > 1_048_576:
                self.log_path.replace(self.log_path.with_name(self.log_path.name + ".1"))
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    # -------- finding Chrome, the dialog, and the button --------

    def workspace_chrome_pids(self) -> list[int]:
        """Read NSWorkspace's application list for diagnostic comparison."""
        out = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if (app.bundleIdentifier() or "") in self.bundles:
                out.append(app.processIdentifier())
        return out

    def chrome_pids(self) -> list[int]:
        """Query each bundle directly, bypassing NSWorkspace's stale app list."""
        out = []
        for bundle in self.bundles:
            apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
            out.extend(app.processIdentifier() for app in apps)
        return out

    def find_dialog_hosts(self, app_element, diag: list[str] | None = None) -> list:
        """Collect elements whose title matches the dialog.

        The dialog is an AXSheet titled 'Allow remote debugging?' one level
        below a browser AXWindow - confirmed on Chrome 152, but *which*
        attribute exposes it there is not stable. A live probe against a real
        prompt found window.AXSheets returning fourteen entries, every one a
        stale/invalid ref (AXError -25202), while the actual sheet showed up
        as a plain entry in window.AXChildren instead. So both are checked,
        one level deep, no further - which is still nothing like the old
        depth-12 recursive walk (~150-160ms/pid, see --diagnostics): a
        window's AXChildren here is a handful of top-level regions (toolbar,
        tab strip, the sheet if any, the web area), not the page's own
        AX-exposed DOM, which only appears if you recurse *into* the web-area
        child - and this doesn't."""
        hits: list = []
        seen: set = set()
        windows = _attr(app_element, "AXWindows") or []
        if diag is not None:
            diag.append(f"windows={len(windows)}")
        for window in windows:
            title = _attr(window, "AXTitle") or ""
            if DIALOG_PATTERN.match(str(title).strip()) and _is_dialog_role(window):
                self._add_host(window, hits, seen)
            candidates = (_attr(window, "AXSheets") or []) + (_attr(window, "AXChildren") or [])
            if diag is not None:
                diag.append(f"candidates={len(candidates)}")
            for candidate in candidates:
                title = _attr(candidate, "AXTitle") or _attr(candidate, "AXDescription") or ""
                if DIALOG_PATTERN.match(str(title).strip()) and _is_dialog_role(candidate):
                    self._add_host(candidate, hits, seen)
        return hits

    def _add_host(self, host, hits: list, seen: set) -> None:
        """Append host if its geometry signature hasn't already surfaced this
        sweep - a window and its sheet can't collide, but stay defensive."""
        sig = self._host_signature(host)
        if sig not in seen:
            seen.add(sig)
            hits.append(host)

    def find_approve_button(self, host, depth: int = 0):
        """The Allow button within one dialog subtree. Scoped to the dialog, never
        the whole app tree, so page-level Allow buttons are out of reach."""
        if depth > 6:
            return None
        if _attr(host, "AXRole") == "AXButton":
            label = _attr(host, "AXTitle") or _attr(host, "AXDescription") or ""
            if APPROVE_PATTERN.match(str(label).strip()):
                return host
        for child in _children(host):
            found = self.find_approve_button(child, depth + 1)
            if found is not None:
                return found
        return None

    def _button_labels(self, host, depth: int = 0, out=None) -> list[str]:
        if out is None:
            out = []
        if depth > 6:
            return out
        if _attr(host, "AXRole") == "AXButton":
            out.append(str(_attr(host, "AXTitle") or _attr(host, "AXDescription") or ""))
        for child in _children(host):
            self._button_labels(child, depth + 1, out)
        return out

    def _host_signature(self, host) -> str:
        """A stable identity for one on-screen dialog, used both to dedupe the
        several tree paths that surface it and to dedupe across sweeps. macOS AX
        has no GetRuntimeId(); Chrome sets no AXIdentifier on this alert (the probe
        confirmed None), so key on geometry, which the port doc anticipated.
        Object id() is useless here - every sweep rebuilds the app element and its
        refs, so id() changes each pass and defeats the dedupe entirely."""
        ident = _attr(host, "AXIdentifier")
        if ident:
            return f"id:{ident}"
        pos = _ax_point(host)
        size = _ax_size(host)
        if pos is not None:
            base = f"pos:{int(pos[0])},{int(pos[1])}"
            if size is not None:
                base += f";size:{int(size[0])},{int(size[1])}"
            return base
        return f"obj:{id(host)}"

    # Back-compat alias for the per-sweep dedupe call site.
    _dedupe_key = _host_signature

    def _press(self, button) -> str | None:
        """AXPress the button. Returns the action name on AX success, not Chrome accept."""
        try:
            err = AXUIElementPerformAction(button, "AXPress")
            if err == kAXErrorSuccess:
                return "AXPress"
        except Exception:
            pass
        return None

    def _raise_host(self, host) -> None:
        """Best-effort AXRaise on the sheet and its parent window so the click lands."""
        try:
            AXUIElementPerformAction(host, "AXRaise")
        except Exception:
            pass
        parent = _attr(host, "AXParent")
        if parent is None:
            return
        try:
            AXUIElementPerformAction(parent, "AXRaise")
        except Exception:
            pass

    def _sheet_still_up(self, host, button) -> bool:
        """True while the sheet that was pressed - that AX node, not whatever
        now sits at its coordinates - is still on screen with its Allow button.

        Identity, not geometry. Chrome queues the consent prompt when several
        CDP clients connect at once and draws the next one at exactly the
        position and size of the one just dismissed, so re-scanning and
        matching a signature reported "still up" for a sheet that was gone,
        and a real approval became a FAILED line the tray's burst guard never
        counted. A dismissed sheet's refs go invalid instead (-25202 on any
        read, verified on Chrome 152), and only a node that is really gone
        does that. AXPress that Chrome ignored, or a teardown slower than
        VERIFY_WAIT_S, leaves the same refs valid and visible: the CGEvent case."""
        host_alive = _ref_alive(host)
        if host_alive is False or _ref_alive(button) is False:
            return False
        if host_alive is None:
            # No answer is not a dismissal: no [ACTION] on this pass. If the
            # sheet is really gone the next sweep simply finds nothing.
            return True
        return _is_visible(host)

    def _hw_click(self, x: float, y: float) -> bool:
        """Left-click global point (x, y), then put the cursor back. Returns False if Quartz is missing."""
        if not _HAVE_QUARTZ:
            return False
        try:
            cursor = CGEventCreate(None)
            origin = CGEventGetLocation(cursor) if cursor else None
            point = (x, y)
            move = CGEventCreateMouseEvent(None, kCGEventMouseMoved, point, kCGMouseButtonLeft)
            down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, point, kCGMouseButtonLeft)
            up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, point, kCGMouseButtonLeft)
            CGEventPost(kCGHIDEventTap, move)
            time.sleep(0.03)
            CGEventPost(kCGHIDEventTap, down)
            time.sleep(0.03)
            CGEventPost(kCGHIDEventTap, up)
            if origin is not None:
                back = CGEventCreateMouseEvent(None, kCGEventMouseMoved, origin, kCGMouseButtonLeft)
                CGEventPost(kCGHIDEventTap, back)
            return True
        except Exception:
            return False

    def _approve(self, host, button) -> str | None:
        """Dismiss the consent sheet. Returns how it fell only after the sheet is gone."""
        self._raise_host(host)
        ax_ok = self._press(button) is not None
        time.sleep(VERIFY_WAIT_S)
        if not self._sheet_still_up(host, button):
            return "AXPress" if ax_ok else "AXRaise"

        # Geometry from the live button ref, read now, so the click targets
        # this button wherever it is - never a queued successor's coordinates.
        pos = _ax_point(button)
        size = _ax_size(button)
        if pos is None or size is None:
            # It stopped answering between the check and the read: if the ref
            # has gone invalid, AXPress did land and teardown just finished.
            if not self._sheet_still_up(host, button):
                return "AXPress" if ax_ok else "AXRaise"
            return None
        x, y = _button_center(pos, size)
        self.log(f"  AXPress left sheet up; CGEvent click at ({x:.1f}, {y:.1f})", "AUDIT")
        if not self._hw_click(x, y):
            return None
        time.sleep(VERIFY_WAIT_S)
        if not self._sheet_still_up(host, button):
            return "CGEvent"
        return None

    def _element_summary(self, element) -> str:
        """Return the AX identity and geometry used to audit a pending click."""
        attributes = {
            "title": _attr(element, "AXTitle"),
            "description": _attr(element, "AXDescription"),
            "role": _attr(element, "AXRole"),
            "subrole": _attr(element, "AXSubrole"),
            "identifier": _attr(element, "AXIdentifier"),
            "enabled": _attr(element, "AXEnabled"),
            "position": _ax_point(element),
            "size": _ax_size(element),
        }
        return " ".join(f"{key}={value!r}" for key, value in attributes.items())

    # -------- one sweep, and the loop --------

    def sweep(self) -> None:
        now = time.time()
        is_diagnostic_sweep = self.diagnostics and now >= self._next_diagnostic_at
        if is_diagnostic_sweep:
            self._next_diagnostic_at = now + 5
            self.log(f"diagnostic sweep start trusted={is_trusted(prompt=False)} "
                     f"parent={os.getppid()} expected_parent={self._parent_pid}", "DIAG")

        direct_started = time.monotonic()
        chrome_pids = self.chrome_pids()
        if is_diagnostic_sweep:
            direct_ms = int((time.monotonic() - direct_started) * 1000)
            workspace_started = time.monotonic()
            workspace_pids = self.workspace_chrome_pids()
            workspace_ms = int((time.monotonic() - workspace_started) * 1000)
            self.log(f"diagnostic pids direct={chrome_pids} ({direct_ms}ms) "
                     f"workspace={workspace_pids} ({workspace_ms}ms)", "DIAG")

        for pid in chrome_pids:
            app = AXUIElementCreateApplication(pid)
            scan_started = time.monotonic()
            diag: list[str] = [] if is_diagnostic_sweep else None
            hosts = self.find_dialog_hosts(app, diag=diag)
            if is_diagnostic_sweep:
                scan_ms = int((time.monotonic() - scan_started) * 1000)
                self.log(f"diagnostic AX pid={pid} hosts={len(hosts)} "
                         f"({scan_ms}ms) {' '.join(diag)}", "DIAG")
            for host in hosts:
                try:
                    if not _is_visible(host):
                        continue
                    labels = [b for b in self._button_labels(host) if b]
                    # A dismissed dialog lingers in the tree for a beat with its
                    # buttons already gone. Matching its title but finding no
                    # buttons is that teardown state, not a real prompt - skip it
                    # quietly so it neither logs noise nor burns a dedupe slot.
                    if not labels:
                        continue

                    key = self._dedupe_key(host)
                    last = self._seen.get(key)
                    if last is not None and now - last < DEDUPE_SECONDS:
                        continue
                    self._seen[key] = now

                    self.log(f"dialog found (pid={pid}) buttons: "
                             + ", ".join(f"'{b}'" for b in labels))

                    if self.observe:
                        self.log("  observe mode - not clicking", "OBSERVE")
                        continue

                    button = self.find_approve_button(host)
                    if button is None:
                        # Real buttons, but none is Allow/Approve (e.g. only "Turn
                        # off in settings"/"Cancel"). Leave it be and say why.
                        self.log(f"  no button matched /{APPROVE_PATTERN.pattern}/ - left alone", "WARN")
                        continue

                    self.log(f"approval pending host={key} siblings={labels!r} "
                             f"target={self._element_summary(button)}", "AUDIT")
                    how = self._approve(host, button)
                    # The dedupe slot has done its job either way. On success
                    # the pressed sheet is verified gone, and a queued prompt
                    # Chrome draws at the same coordinates carries the same
                    # key: it must be served next sweep, not after
                    # DEDUPE_SECONDS. On failure a leftover sheet is retried
                    # next sweep instead of sitting out the window.
                    self._seen.pop(key, None)
                    if how:
                        self.approved += 1
                        self.log(f"  APPROVED via {how}", "ACTION")
                        self.log(f"  total approved this session: {self.approved}")
                    else:
                        self.log("  FAILED: sheet still up after AXPress/CGEvent", "ERROR")
                except Exception as exc:
                    self.log(f"  host error: {exc!r}", "ERROR")

        # Bound the dedupe memory.
        if len(self._seen) > DEDUPE_MAX:
            cutoff = now - 300
            self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        if is_diagnostic_sweep:
            self.log("diagnostic sweep complete", "DIAG")

    def run(self) -> int:
        if not is_trusted(prompt=False):
            self.log("NOT trusted for Accessibility - every AX read will be empty. "
                     "Grant it in System Settings > Privacy & Security > Accessibility, "
                     "then restart.", "ERROR")
            # Keep running: the grant can be given while we are up, and the next
            # sweep will start seeing elements. Better than exiting and looking dead.
        self.log(f"engine started (observe={self.observe}, interval={int(self.poll_s * 1000)}ms, "
                 f"bundles={len(self.bundles)}, pid={os.getpid()})")
        while True:
            if self.exit_with_parent and os.getppid() != self._parent_pid:
                # The tray is gone (quit, crashed, killed, logged out) and we have
                # been reparented. Exiting matters more here than it looks: every
                # safety limit - the burst guard, the arm timer, the pause - lives
                # in the tray. An engine that outlives it keeps approving prompts
                # with nothing watching the rate and no way to turn it off short
                # of finding the pid.
                self.log("parent process is gone - exiting rather than approving "
                         "unsupervised", "WARN")
                return 0
            try:
                self.sweep()
            except Exception as exc:
                self.log(f"loop error: {exc!r}", "ERROR")
            time.sleep(self.poll_s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Yes, Dev macOS engine")
    ap.add_argument("--observe", action="store_true", help="log dialogs, never click")
    ap.add_argument("--once", action="store_true", help="one sweep then exit")
    ap.add_argument("--interval-ms", type=int, default=POLL_MS_DEFAULT)
    ap.add_argument("--include-edge", action="store_true")
    ap.add_argument("--log-path", default=str(LOG_PATH))
    ap.add_argument("--exit-with-parent", action="store_true",
                    help="stop as soon as the launching process goes away; the "
                         "tray passes this so a dead tray cannot leave an engine "
                         "approving prompts unsupervised")
    ap.add_argument("--diagnostics", action="store_true",
                    help="log trust, process discovery, and AX sweep timing every 5s")
    args = ap.parse_args(argv)

    engine = Engine(observe=args.observe, poll_ms=args.interval_ms,
                    include_edge=args.include_edge, log_path=Path(args.log_path),
                    exit_with_parent=args.exit_with_parent,
                    diagnostics=args.diagnostics)
    if args.once:
        if not is_trusted():
            engine.log("NOT trusted for Accessibility - results will be empty.", "ERROR")
        engine.sweep()
        return 0
    return engine.run()


if __name__ == "__main__":
    raise SystemExit(main())
