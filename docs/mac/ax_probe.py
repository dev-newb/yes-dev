#!/usr/bin/env python3
"""Dump Chrome's accessibility tree around the "Allow remote debugging?" dialog.

Run this FIRST, before writing any macOS engine. The whole design depends on how
that dialog appears to the Accessibility API - whether it is a sheet attached to
a browser window, a separate window, or something nested deeper - and that is
not worth guessing at. This is the macOS counterpart of the UI Automation dump
that shaped watcher.ps1 on Windows, where the dialog turned out to be a bubble
parented two levels inside the browser frame rather than a top-level window.

    pip install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices
    python3 docs/mac/ax_probe.py            # dump anything dialog-shaped
    python3 docs/mac/ax_probe.py --all      # dump the whole window tree

You must grant Accessibility permission to whatever runs this (Terminal, iTerm,
your IDE) in System Settings > Privacy & Security > Accessibility, or every
attribute read comes back empty.

To make a prompt appear, attach any CDP client to a Chrome started with
--remote-debugging-port and a non-default --user-data-dir. The repo's
trigger-once.mjs pattern works: open a WebSocket to the endpoint in
<user-data-dir>/DevToolsActivePort and send Target.getTargets.

NOTE: written without a Mac to test on. Expect to fix a detail or two; the
structure and the attribute names are the point.
"""
from __future__ import annotations

import re
import sys

try:
    from ApplicationServices import (
        AXIsProcessTrusted,
        AXUIElementCopyAttributeNames,
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        kAXErrorSuccess,
    )
    from AppKit import NSWorkspace
except ImportError:
    sys.exit("pip install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices")

DIALOG = re.compile(r"allow remote debugging", re.I)
APPROVE = re.compile(r"^(allow|approve)$", re.I)
CHROME_BUNDLES = ("com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.canary")
MAX_DEPTH = 12


def attr(element, name):
    """One accessibility attribute, or None if the element does not carry it."""
    err, value = AXUIElementCopyAttributeValue(element, name, None)
    return value if err == kAXErrorSuccess else None


def names(element) -> list[str]:
    err, value = AXUIElementCopyAttributeNames(element, None)
    return list(value) if err == kAXErrorSuccess else []


def label(element) -> str:
    role = attr(element, "AXRole") or "?"
    sub = attr(element, "AXSubrole")
    title = attr(element, "AXTitle") or attr(element, "AXDescription") or ""
    ident = attr(element, "AXIdentifier") or ""
    pos, size = attr(element, "AXPosition"), attr(element, "AXSize")
    bits = [role + (f"/{sub}" if sub else "")]
    if title:
        bits.append(f'title={title!r}')
    if ident:
        bits.append(f"id={ident!r}")
    if pos is not None or size is not None:
        bits.append(f"pos={pos} size={size}")
    return "  ".join(bits)


def walk(element, depth: int = 0, show_all: bool = False, hits=None) -> None:
    """Print the tree. Sheets matter as much as children - a macOS dialog is
    often attached to its parent window rather than being a window of its own."""
    if depth > MAX_DEPTH:
        return
    text = label(element)
    interesting = DIALOG.search(text) or (attr(element, "AXRole") == "AXButton")
    if show_all or interesting or depth < 3:
        print("  " * depth + text)
    if hits is not None and DIALOG.search(text):
        hits.append(element)

    for bucket in ("AXSheets", "AXChildren", "AXWindows"):
        for child in (attr(element, bucket) or []):
            walk(child, depth + 1, show_all, hits)


def main() -> int:
    show_all = "--all" in sys.argv

    if not AXIsProcessTrusted():
        print("!! This process is NOT trusted for Accessibility.")
        print("!! System Settings > Privacy & Security > Accessibility, add the app")
        print("!! running this (Terminal/iTerm/your IDE), then run it again.\n")

    apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications()
            if (a.bundleIdentifier() or "") in CHROME_BUNDLES]
    if not apps:
        return print("No Chrome process found.") or 1

    for app in apps:
        pid = app.processIdentifier()
        print(f"=== {app.bundleIdentifier()} pid {pid} ===")
        root = AXUIElementCreateApplication(pid)
        print("app-level attributes:", ", ".join(names(root)) or "(none - permission?)")
        hits: list = []
        walk(root, 0, show_all, hits)

        print(f"\n--- {len(hits)} element(s) matching /allow remote debugging/i ---")
        for host in hits:
            print(label(host))
            buttons = []

            def collect(el, d=0):
                if d > 6:
                    return
                if attr(el, "AXRole") == "AXButton":
                    buttons.append(attr(el, "AXTitle") or attr(el, "AXDescription") or "")
                for bucket in ("AXChildren", "AXSheets"):
                    for kid in (attr(el, bucket) or []):
                        collect(kid, d + 1)

            collect(host)
            print("   buttons:", buttons)
            print("   would press:", [b for b in buttons if APPROVE.match(b or "")])
            print("   ACTIONS on host:", names(host))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
