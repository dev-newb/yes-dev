"""macOS platform bits for Yes, Dev.

Everything here is the macOS answer to something the Windows build did with a
Win32 call: where config and logs live, how a single instance is enforced, how
autostart is registered, and whether the process holds the Accessibility grant
the engine cannot work without.

Kept in one small module so the tray, the engine and the overlay share exactly
one definition of each - the paths in particular, since three processes write
into the same directory and a mismatch would scatter logs silently.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Yes, Dev"
APP_SLUG = "yes-dev"
BUNDLE_ID = "com.dev-newb.yesdev"

# %LOCALAPPDATA%\YesDev on Windows; its macOS counterpart. One directory holds
# the config and both logs, matching the Windows layout so the tray/engine
# contract ("[ACTION]" lines in yes-dev.log) ports unchanged.
DATA_DIR = Path.home() / "Library" / "Application Support" / "YesDev"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / f"{APP_SLUG}.log"
TRAY_LOG = DATA_DIR / "tray.log"

LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS / f"{BUNDLE_ID}.plist"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Single instance
# --------------------------------------------------------------------------
# Windows used a named mutex ("Global\\YesDevTray"). The portable equivalent is
# an flock on a file that lives as long as the process: the lock releases when
# the fd closes, which happens on any exit, clean or not - no stale lock to
# clear the way a bare pidfile would leave behind.

_lock_handle = None  # kept alive for the life of the process


def acquire_single_instance(name: str = "tray") -> bool:
    """True if we got the lock, False if another instance already holds it.

    The handle is stashed at module scope on purpose: let it be garbage
    collected and the lock releases while we are still running.
    """
    global _lock_handle
    import fcntl

    ensure_data_dir()
    path = DATA_DIR / f".{name}.lock"
    handle = open(path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    try:
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        pass
    _lock_handle = handle
    return True


# --------------------------------------------------------------------------
# Accessibility (TCC) permission
# --------------------------------------------------------------------------
# The whole engine is inert without this grant: every AX attribute read returns
# empty and the app looks broken rather than unpermitted, so we check up front
# and say so plainly. `prompt=True` triggers the one-time system dialog that
# deep-links into System Settings.


def is_trusted(prompt: bool = False) -> bool:
    """Whether this process may drive other apps' UI (AXIsProcessTrusted).

    On anything that is not macOS, or if pyobjc is missing, returns False -
    the caller logs and degrades rather than crashing.
    """
    if sys.platform != "darwin":
        return False
    try:
        from ApplicationServices import (
            AXIsProcessTrusted,
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
    except ImportError:
        return False

    if not prompt:
        return bool(AXIsProcessTrusted())
    try:
        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
    except Exception:
        return bool(AXIsProcessTrusted())


def accessibility_settings_url() -> str:
    """Deep link to the Accessibility pane, for a menu item or a log hint."""
    return "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


# --------------------------------------------------------------------------
# Autostart via LaunchAgent
# --------------------------------------------------------------------------
# Windows dropped a .lnk in the Startup folder. The macOS counterpart is a
# LaunchAgent plist under ~/Library/LaunchAgents. RunAtLoad starts it at login;
# KeepAlive is deliberately false so a user Quit stays quit until next login.
#
# Note on packaging (see docs/MACOS_PORT.md): run as a loose script and the
# Accessibility grant attaches to the python binary, which is fragile and far
# too broad. A signed .app should instead register a login item with
# SMAppService. This plist is the script-mode path, useful during development.

_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{script}</string>
  </array>
  <key>WorkingDirectory</key><string>{workdir}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
"""


def autostart_enabled() -> bool:
    return PLIST_PATH.exists()


def enable_autostart(script_path: Path) -> None:
    """Write and load the LaunchAgent so the tray starts at login."""
    import subprocess

    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    plist = _PLIST_TEMPLATE.format(
        label=BUNDLE_ID,
        python=sys.executable,
        script=str(Path(script_path).resolve()),
        workdir=str(Path(script_path).resolve().parent),
    )
    PLIST_PATH.write_text(plist, encoding="utf-8")
    # bootstrap is the modern verb; fall back to load -w on older systems.
    uid = os.getuid()
    try:
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
                       capture_output=True, check=False)
    except Exception:
        subprocess.run(["launchctl", "load", "-w", str(PLIST_PATH)],
                       capture_output=True, check=False)


def disable_autostart() -> None:
    import subprocess

    if PLIST_PATH.exists():
        uid = os.getuid()
        try:
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{BUNDLE_ID}"],
                           capture_output=True, check=False)
        except Exception:
            subprocess.run(["launchctl", "unload", "-w", str(PLIST_PATH)],
                           capture_output=True, check=False)
        try:
            PLIST_PATH.unlink()
        except OSError:
            pass
