"""Yes, Dev engine for macOS: auto-approves Chrome's "Allow remote debugging?"
consent dialog through the Accessibility API.

This is the macOS counterpart of watcher.ps1. The contract with the tray is the
only thing that must stay identical: append a line containing `[ACTION]` to the
log for each approval, in the existing format

    2026-08-27 16:11:28.644 [ACTION]   APPROVED via AXPress

and the counter, the clouds and the burst guard in the tray all work unchanged.

  >>> UNVERIFIED <<<  Written without a Mac to test on, from docs/MACOS_PORT.md.
  The AX attribute names and, above all, WHERE the dialog sits in the tree
  (AXSheet on the window? separate AXWindow? nested deeper?) are the informed
  guess that docs/mac/ax_probe.py exists to confirm. Run the probe first and
  reconcile find_dialog_hosts() with what it prints before trusting this.

Runs standalone for exactly that shake-out:

    python3 watcher_mac.py --observe        # log dialogs and buttons, never click
    python3 watcher_mac.py --once           # one sweep, then exit (prints findings)
    python3 watcher_mac.py                   # the real loop, clicking Allow
"""
from __future__ import annotations

import argparse
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
        kAXErrorSuccess,
    )
    from AppKit import NSWorkspace
except ImportError:
    sys.exit(
        "Yes, Dev macOS engine needs pyobjc:\n"
        "    pip3 install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices"
    )

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
MAX_TREE_DEPTH = 12


def _attr(element, name):
    """One AX attribute, or None. Every read can fail (permission, torn-down
    element); callers treat None as 'not present' rather than crashing."""
    try:
        err, value = AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return None
    return value if err == kAXErrorSuccess else None


def _children(element):
    """A window's dialog can hang off AXSheets as readily as AXChildren, and the
    probe may reveal AXWindows nesting too - walk all three the way ax_probe does."""
    out = []
    for bucket in ("AXChildren", "AXSheets", "AXWindows"):
        kids = _attr(element, bucket)
        if kids:
            out.extend(kids)
    return out


def _is_visible(element) -> bool:
    """Skip hidden/offscreen elements: a dismissed dialog lingers briefly in the
    tree and would otherwise be approved a second time."""
    hidden = _attr(element, "AXHidden")
    if hidden:
        return False
    # AXFrame/AXSize of zero is the other tell-tale of a torn-down element.
    size = _attr(element, "AXSize")
    if size is not None:
        try:
            w, h = size.sizeValue() if hasattr(size, "sizeValue") else (size.width, size.height)
            if w <= 0 or h <= 0:
                return False
        except Exception:
            pass
    return True


class Engine:
    def __init__(self, observe: bool = False, poll_ms: int = POLL_MS_DEFAULT,
                 include_edge: bool = False, log_path: Path = LOG_PATH) -> None:
        self.observe = observe
        self.poll_s = max(0.05, poll_ms / 1000.0)
        self.bundles = CHROME_BUNDLES + (EDGE_BUNDLES if include_edge else ())
        self.log_path = Path(log_path)
        self.approved = 0
        self._seen: dict[str, float] = {}    # dedupe key -> last-press wall clock

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

    def chrome_pids(self) -> list[int]:
        out = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if (app.bundleIdentifier() or "") in self.bundles:
                out.append(app.processIdentifier())
        return out

    def find_dialog_hosts(self, app_element, depth: int = 0, hits=None) -> list:
        """Collect elements whose title matches the dialog, scanning windows and
        their sheets/children. Bounded depth so a pathological tree can't hang the
        sweep.

        >>> The exact shape here is what ax_probe.py confirms. If the probe shows
        the dialog is always an AXSheet on the browser window, this can be
        tightened to windows->sheets and stop walking arbitrary children. <<<"""
        if hits is None:
            hits = []
        if depth > MAX_TREE_DEPTH:
            return hits
        title = _attr(app_element, "AXTitle") or _attr(app_element, "AXDescription") or ""
        if DIALOG_PATTERN.match(str(title).strip()):
            hits.append(app_element)
            # don't descend into a matched dialog; its buttons are found separately
            return hits
        for child in _children(app_element):
            self.find_dialog_hosts(child, depth + 1, hits)
        return hits

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

    def _dedupe_key(self, host) -> str:
        """macOS AX has no GetRuntimeId(). Key on identifier if Chrome sets one,
        else on (title, position) as the port doc suggests - stable enough for a
        2-second window, which is all the dedupe needs."""
        ident = _attr(host, "AXIdentifier")
        if ident:
            return f"id:{ident}"
        pos = _attr(host, "AXPosition")
        try:
            if pos is not None:
                p = pos.pointValue() if hasattr(pos, "pointValue") else pos
                return f"pos:{int(p.x)},{int(p.y)}"
        except Exception:
            pass
        return f"obj:{id(host)}"

    def _press(self, button) -> str | None:
        """AXPress the button. Returns the action name on success, None on failure."""
        try:
            err = AXUIElementPerformAction(button, "AXPress")
            if err == kAXErrorSuccess:
                return "AXPress"
        except Exception:
            pass
        return None

    # -------- one sweep, and the loop --------

    def sweep(self) -> None:
        now = time.time()
        for pid in self.chrome_pids():
            app = AXUIElementCreateApplication(pid)
            for host in self.find_dialog_hosts(app):
                try:
                    if not _is_visible(host):
                        continue
                    key = self._dedupe_key(host)
                    last = self._seen.get(key)
                    if last is not None and now - last < DEDUPE_SECONDS:
                        continue
                    self._seen[key] = now

                    labels = self._button_labels(host)
                    self.log(f"dialog found (pid={pid}) buttons: "
                             + ", ".join(f"'{b}'" for b in labels if b))

                    if self.observe:
                        self.log("  observe mode - not clicking", "OBSERVE")
                        continue

                    button = self.find_approve_button(host)
                    if button is None:
                        self.log(f"  no button matched /{APPROVE_PATTERN.pattern}/ - left alone", "WARN")
                        continue

                    how = self._press(button)
                    if how:
                        self.approved += 1
                        self.log(f"  APPROVED via {how}", "ACTION")
                        self.log(f"  total approved this session: {self.approved}")
                    else:
                        # Drop the dedupe entry so a failed press is retried on the
                        # next sweep instead of sitting out the window.
                        self._seen.pop(key, None)
                        self.log("  FAILED to invoke Allow button", "ERROR")
                except Exception as exc:
                    self.log(f"  host error: {exc!r}", "ERROR")

        # Bound the dedupe memory.
        if len(self._seen) > DEDUPE_MAX:
            cutoff = now - 300
            self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}

    def run(self) -> int:
        if not is_trusted(prompt=False):
            self.log("NOT trusted for Accessibility - every AX read will be empty. "
                     "Grant it in System Settings > Privacy & Security > Accessibility, "
                     "then restart.", "ERROR")
            # Keep running: the grant can be given while we are up, and the next
            # sweep will start seeing elements. Better than exiting and looking dead.
        self.log(f"engine started (observe={self.observe}, interval={int(self.poll_s * 1000)}ms, "
                 f"bundles={len(self.bundles)}, pid={__import__('os').getpid()})")
        while True:
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
    args = ap.parse_args(argv)

    engine = Engine(observe=args.observe, poll_ms=args.interval_ms,
                    include_edge=args.include_edge, log_path=Path(args.log_path))
    if args.once:
        if not is_trusted():
            engine.log("NOT trusted for Accessibility - results will be empty.", "ERROR")
        engine.sweep()
        return 0
    return engine.run()


if __name__ == "__main__":
    raise SystemExit(main())
