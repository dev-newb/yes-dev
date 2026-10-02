"""Yes, Dev - macOS status-bar control for the Chrome remote-debugging auto-approver.

The pyobjc engine (watcher_mac.py) does the Accessibility work. This owns its
lifetime, exposes the options in a status-bar menu, and tails the log so
approvals are visible and a runaway burst can be caught.

This is the macOS counterpart of yes_dev.pyw. The *logic* is deliberately the
same - config schema, burst guard, auto-resume, arm timer, log tailing for
`[ACTION]` - because the config file and the log are a shared contract between
the two builds. Only the platform I/O differs:

    Windows                       macOS
    ---------------------------   --------------------------------------------
    pystray                       rumps (NSStatusItem)
    powershell watcher.ps1        python3 watcher_mac.py
    taskkill /T /F                terminate(), then kill()
    Startup folder .lnk           LaunchAgent (platform_mac.enable_autostart)
    named mutex                   flock (platform_mac.acquire_single_instance)
    os.startfile                  open(1)
    %LOCALAPPDATA%\\YesDev         ~/Library/Application Support/YesDev
    -                             an Accessibility grant, which gates everything

Run it with the repo's Python: `python3 yes_dev_mac.py`.
"""
from __future__ import annotations

import argparse
import json
import random
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))   # so `import puffs` works however we were launched

try:
    import rumps
except ImportError:
    sys.exit("Yes, Dev needs rumps for the status bar:\n    pip3 install rumps")

from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
from PIL import Image, ImageDraw, ImageOps

import platform_mac
import puffs                     # the icon is drawn from the same cloud renderer
from platform_mac import (
    APP_NAME, APP_SLUG, CONFIG_PATH, DATA_DIR, LOG_PATH, TRAY_LOG,
    accessibility_settings_url, ensure_data_dir, is_trusted,
)

WATCHER = BASE / "watcher_mac.py"
OVERLAY = BASE / "puffs_mac.py"
FOCUS_GUARD = BASE / "focus_guard_mac.py"
BURST_DIALOG = BASE / "burst_dialog.py"

from settings_model import normalize, save_atomic

RELAY = BASE / "relay_mac.py"
RELAY_STATUS = DATA_DIR / "relay-status.json"

BURST_WINDOW = 60.0    # seconds
BURST_COOLDOWN = 60.0  # auto-resume after this long, rather than stranding agents
ALLOW_HOUR = 3600.0

# rumps forces every status icon into a 20x20 point square. A cloud is much wider
# than it is tall, so squaring it wastes the top and bottom of the box and the
# icon reads small next to neighbouring menu extras. We size it ourselves instead:
# the art is cropped to what it actually covers and given its own aspect ratio, so
# the height is the thing that fills the bar. 18pt is about as tall as an icon
# should be in a 24pt menu bar.
ICON_H_PT = 18
ICON_SCALE = 2       # draw at 2x and size in points: crisp on retina
ICON_SS = 6          # draw this much larger again, then downscale, for clean edges
ICON_SEED = 7        # one silhouette for every state: the icon must not change
                     # shape when it changes colour, only its colour
CHECK_SCALE = 1.34   # the check is oversized on purpose; this small, a thin one
                     # turns to mush
ICON_VERSION = 3     # bump when the drawing changes, so cached files are dropped

STATE_COLORS = {
    "paused": (218, 54, 51),      # red
    "off": (140, 140, 140),       # grey
    "observing": (219, 154, 4),   # amber
    "on": (46, 160, 67),          # green
}


def log(message: str) -> None:
    """Tray-side log. Launched from a LaunchAgent there is no console, so
    failures are invisible without this."""
    try:
        ensure_data_dir()
        if TRAY_LOG.exists() and TRAY_LOG.stat().st_size > 1_000_000:
            TRAY_LOG.replace(TRAY_LOG.with_name(TRAY_LOG.name + ".1"))
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with TRAY_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def icon_path(state: str) -> str:
    """One PNG per state, written once and then referenced by path.

    Separate files rather than one rewritten file: rumps hands the path to
    NSImage's initByReferencingFile_, and a path whose contents changed under it
    is not guaranteed to be re-read.

    The icon is the app's own cloud - the very shape puffs_mac.py drops from the
    status item - turned upside down so it hangs from the menu bar the way those
    clouds do, filled with the state colour, with a white check in front of it.
    The Windows build uses a plain circle; a circle reads as a dot in a row of
    menu extras, where the silhouette is the only thing that identifies an app.
    """
    ensure_data_dir()
    path = DATA_DIR / f"icon-{state}-v{ICON_VERSION}.png"
    if path.exists():
        return str(path)

    R = ICON_H_PT * ICON_SCALE * ICON_SS
    canvas = Image.new("RGBA", (R, R), (0, 0, 0, 0))

    # Seeded, so all four states share one silhouette - _render_cloud jitters its
    # lobes per call, and an icon that changed shape on every state change would
    # look like a different app rather than the same one in a different mood.
    random.seed(ICON_SEED)
    mask = ImageOps.flip(puffs._render_cloud(R, premultiply=False).split()[3])
    body = Image.new("RGBA", mask.size,
                     STATE_COLORS.get(state, STATE_COLORS["off"]) + (255,))
    body.putalpha(mask)
    canvas.alpha_composite(body, (0, (R - mask.size[1]) // 2))

    # Nudged up slightly: flipped, the cloud's mass sits above its centre line.
    d = ImageDraw.Draw(canvas)
    cx, cy = R / 2, R / 2 - 0.02 * R
    s = R * 0.0092 * CHECK_SCALE
    d.line([(cx - 13 * s, cy + 0.5 * s), (cx - 4.5 * s, cy + 9.5 * s),
            (cx + 13.5 * s, cy - 10 * s)],
           fill=(255, 255, 255, 255), width=int(5.6 * s), joint="curve")

    # Crop to what the art actually covers. _render_cloud leaves ~15% margins so
    # its blur has room, and those margins would otherwise be paid for in icon
    # size - the cloud would sit small inside a box of nothing.
    canvas = canvas.crop(canvas.getbbox())
    h_px = ICON_H_PT * ICON_SCALE
    w_px = max(1, round(canvas.width * h_px / canvas.height))
    canvas.resize((w_px, h_px), Image.LANCZOS).save(path)
    return str(path)


def icon_point_size(path: str) -> tuple[float, float]:
    """The icon's size in points, from the pixels actually on disk."""
    with Image.open(path) as img:
        return (img.width / ICON_SCALE, img.height / ICON_SCALE)


class Config(dict):
    def __init__(self) -> None:
        saved = {}
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                log(f"config unreadable ({exc!r}) - using defaults")
        super().__init__(normalize(saved))

    def save(self) -> None:
        try:
            save_atomic(CONFIG_PATH, self)
        except Exception as exc:
            log(f"config save failed: {exc!r}")


class YesDev(rumps.App):
    def __init__(self) -> None:
        self.cfg = Config()
        super().__init__(APP_NAME, title=None, icon=icon_path("off"), quit_button=None)

        self.proc: subprocess.Popen | None = None
        self.guard: subprocess.Popen | None = None   # normal focus guard
        self.relay: subprocess.Popen | None = None
        self._relay_retry_at = 0.0
        self._settings = None
        self.approvals = 0
        self.recent: deque[float] = deque()      # timestamps, for the burst guard
        self.disarm_at: datetime | None = None
        self.paused_reason: str | None = None
        self.resume_at: datetime | None = None
        self.allow_until: datetime | None = None   # burst guard snoozed by the user
        self._log_pos = 0
        self._puffs = None            # built on first use
        self._asking: subprocess.Popen | None = None   # burst dialog, if one is up
        self._asking_count = 0
        self._icon_state: str | None = None

        # Persist migrated defaults; settings are edited in the native window.
        self.cfg.save()
        self._build_menu()
        self.refresh()

    # ---------- engine lifetime ----------

    def engine_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start_engine(self) -> None:
        if self.engine_running():
            return
        args = [sys.executable, str(WATCHER),
                "--interval-ms", str(self.cfg["poll_ms"]),
                "--log-path", str(LOG_PATH),
                # If this tray dies without cleaning up, the engine stops itself.
                # Every safety limit lives here, not in the engine, so an engine
                # that outlives the tray approves prompts with no burst guard and
                # no arm timer behind it.
                "--exit-with-parent"]
        if self.cfg["observe_only"]:
            args.append("--observe")
        if self.cfg["include_edge"]:
            args.append("--include-edge")
        if self.cfg.get("diagnostics"):
            args.append("--diagnostics")

        # Only surface approvals logged from here on, not the whole history.
        self._log_pos = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
        try:
            # DEVNULL, not PIPE: the engine also prints every line to stdout, and
            # a pipe nobody drains would fill and block it mid-sweep.
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        except Exception as exc:
            log(f"engine failed to start: {exc!r}")
            return
        log(f"engine started pid={self.proc.pid} log_pos={self._log_pos}")

        mins = int(self.cfg["arm_minutes"] or 0)
        self.disarm_at = datetime.now() + timedelta(minutes=mins) if mins else None
        self.paused_reason = None
        self.sync_guard()

    def stop_engine(self) -> None:
        self.stop_relay()
        self.stop_guard()
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            # No taskkill here: the engine is a plain child process with no shell
            # between us and it, so a signal is enough.
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass
        self.disarm_at = None

    # ---------- focus guard lifetime ----------
    #
    # The guard lives only while the engine is approving. With the engine off,
    # observing, or paused, the user has to see the sheet to click it themselves,
    # and taking focus away from it would be exactly wrong.

    def guard_running(self) -> bool:
        return self.guard is not None and self.guard.poll() is None

    def guard_wanted(self) -> bool:
        return bool(self.cfg.get("quiet_focus")) and self.engine_running() and not self.cfg["observe_only"]

    def sync_guard(self) -> None:
        if self.guard_wanted() and self.cfg.get("relay_enabled"):
            self.stop_guard()
            self.start_relay()
            return
        self.stop_relay()
        if self.guard_wanted():
            if not self.guard_running():
                self.start_guard()
        elif self.guard_running():
            self.stop_guard()

    def start_guard(self) -> None:
        args = [sys.executable, str(FOCUS_GUARD), "--exit-with-parent"]
        if self.cfg["include_edge"]:
            args.append("--include-edge")
        try:
            self.guard = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        except Exception as exc:
            log(f"focus guard failed to start: {exc!r}")
            return
        log(f"focus guard started pid={self.guard.pid}")

    def stop_guard(self) -> None:
        proc, self.guard = self.guard, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass

    def start_relay(self) -> None:
        if self.relay is not None and self.relay.poll() is None:
            return
        if time.monotonic() < self._relay_retry_at:
            return
        self._relay_retry_at = time.monotonic() + 10
        args = [sys.executable, str(RELAY), "--profile", self.cfg["relay_profile"],
                "--port", str(self.cfg["relay_port"]), "--exit-with-parent"]
        if self.cfg.get("relay_hold"):
            args.append("--hold")
        try:
            self.relay = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log(f"relay started pid={self.relay.pid}")
        except Exception as exc:
            log(f"relay failed to start: {exc!r}")

    def stop_relay(self) -> None:
        proc, self.relay = self.relay, None
        if proc is not None:
            self._relay_retry_at = 0
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except Exception as exc:
                log(f"relay shutdown: {exc!r}")

    def relay_status_text(self) -> str:
        if not self.cfg.get("relay_enabled"):
            return "Fast focus: off"
        if not self.guard_wanted():
            return "Fast focus: paused while approval is off or observing"
        try:
            status = json.loads(RELAY_STATUS.read_text())
            if self.relay is not None and status.get("pid") == self.relay.pid:
                if status.get("last_error"):
                    return "Fast focus: " + status["last_error"]
                if status.get("state") == "listening" and self.relay.poll() is None:
                    held = " · Chrome connection held" if status.get("held") else ""
                    return f"Fast focus: ready{held} · {status.get('connections', 0)} connected"
        except (OSError, ValueError):
            pass
        return "Fast focus: starting — check the relay log if this persists"

    # ---------- background loop ----------

    def _tick(self, _timer=None) -> None:
        """Runs on the main thread, via rumps' NSTimer.

        The Windows build puts this on a worker thread, but AppKit menu updates
        must happen on the main thread, so it lives here instead - which is why
        nothing in this path is allowed to block (see _on_burst).
        """
        try:
            self._poll_burst_dialog()

            # A burst pause is a speed bump, not a stop: re-arm on its own so a
            # busy stretch never leaves agents waiting on a human again.
            if self.paused_reason and self.resume_at and datetime.now() >= self.resume_at:
                log(f"auto-resumed after {self.paused_reason}")
                self.paused_reason = None
                self.resume_at = None
                self.recent.clear()
                self.notify(f"{APP_NAME} is back on")

            if self.cfg["enabled"] and not self.paused_reason:
                if not self.engine_running():
                    self.start_engine()      # first run, or engine died - restart it
                if self.disarm_at and datetime.now() >= self.disarm_at:
                    self._pause("timer expired")
                    self.notify(f"Disarmed after {self.cfg['arm_minutes']} min")
            self.sync_guard()        # start it, or restart it if it died
            self._read_log()
            self.refresh()
        except Exception:
            import traceback
            log(f"tick error: {traceback.format_exc()}")

    def _read_log(self) -> None:
        if not LOG_PATH.exists():
            return
        size = LOG_PATH.stat().st_size
        if size < self._log_pos:
            self._log_pos = 0                    # log was rotated or cleared
        if size == self._log_pos:
            return
        # Binary, not text: byte offsets from stat() are only meaningful against
        # a binary stream.
        try:
            with LOG_PATH.open("rb") as fh:
                fh.seek(self._log_pos)
                chunk = fh.read().decode("utf-8", errors="replace")
                self._log_pos = fh.tell()
        except OSError:
            return

        hits = sum(1 for line in chunk.splitlines() if "[ACTION]" in line)
        if not hits:
            return

        self.approvals += hits
        now = time.time()
        self.recent.extend([now] * hits)
        while self.recent and now - self.recent[0] > BURST_WINDOW:
            self.recent.popleft()

        if self.paused_reason:
            return   # already paused; keep consuming the log but don't re-pause,
                     # which would keep pushing the auto-resume further out

        limit = self.cfg["burst_limit"]
        snoozed = self.allow_until is not None and datetime.now() < self.allow_until
        if limit and not snoozed and len(self.recent) >= limit:
            self._on_burst(len(self.recent))
        else:
            self.announce(hits)

    def _on_burst(self, count: int) -> None:
        """A burst tripped the guard. Either act silently, or put the choice to
        the user with a short deadline - deciding nothing means stop."""
        if self.cfg["burst_action"] != "ask":
            self._pause("burst guard", resume_after=BURST_COOLDOWN)
            self.notify(f"Paused: {count} approvals in under a minute. "
                        f"Resuming automatically in {int(BURST_COOLDOWN)}s.")
            return

        if self._asking is not None:
            return          # a dialog is already up; don't stack a second one

        log(f"burst of {count} - asking the user")
        try:
            # Spawned, not waited on. The Windows build blocks its worker thread
            # here for up to 30s, which is what keeps a second dialog from being
            # raised; on the main thread that would freeze the menu, so the
            # answer is collected in _poll_burst_dialog and _asking is the guard
            # against re-entry.
            self._asking = subprocess.Popen(
                [sys.executable, str(BURST_DIALOG), str(count), APP_NAME],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            self._asking_count = count
        except Exception as exc:
            log(f"  burst dialog failed ({exc!r}) - stopping, the safe answer")
            self._apply_burst_choice("stop")

    def _poll_burst_dialog(self) -> None:
        if self._asking is None or self._asking.poll() is None:
            return
        proc, self._asking = self._asking, None
        try:
            out = (proc.stdout.read() if proc.stdout else "") or ""
        except Exception:
            out = ""
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        # Deciding nothing means stop, because that is the safe answer to a
        # burst you were not expecting.
        self._apply_burst_choice(lines[-1] if lines else "stop")

    def _apply_burst_choice(self, choice: str) -> None:
        if choice == "allow_hour":
            self.allow_until = datetime.now() + timedelta(seconds=ALLOW_HOUR)
            self.recent.clear()
            log("  user allowed bursts for one hour")
            self.notify(f"{APP_NAME} will keep approving for one hour")
        else:
            log("  user stopped the app (or let the timer run out)")
            self._pause("stopped after burst")   # no auto-resume: deliberate
            self.notify(f"{APP_NAME} stopped. Turn it back on from the menu when ready.")
        self.refresh()

    def announce(self, hits: int) -> None:
        """Routine approvals: a puff per approval, a toast, or nothing."""
        style = self.cfg["notify_style"]
        if style == "toast":
            self.notify(f"Approved {hits} debugging request{'s' if hits > 1 else ''}")
        elif style == "puffs":
            overlay = self.puff_overlay()
            if overlay is not None:
                overlay.emit(hits)

    def puff_overlay(self):
        if self._puffs is None:
            try:
                import puffs
                # Same client, same stdin protocol - only the overlay differs.
                self._puffs = puffs.PuffClient(log=log, script=OVERLAY)
            except Exception:
                import traceback
                log(f"  puff client FAILED: {traceback.format_exc()}")
                self._puffs = False   # unavailable; don't retry on every approval
        return self._puffs or None

    def _pause(self, reason: str, resume_after: float | None = None) -> None:
        self.paused_reason = reason
        self.resume_at = (datetime.now() + timedelta(seconds=resume_after)
                          if resume_after else None)
        log(f"PAUSED ({reason})" + (f", auto-resume in {int(resume_after)}s" if resume_after else ""))
        self.stop_engine()
        self.refresh()

    # ---------- status ----------

    def state(self) -> str:
        if self.paused_reason:
            return "paused"
        if not self.cfg["enabled"]:
            return "off"
        if self.cfg["observe_only"]:
            return "observing"
        return "on"

    def state_text(self) -> str:
        if self.paused_reason:
            return f"paused ({self.paused_reason})"
        return self.state()

    def notify(self, message: str) -> None:
        """A user-visible notification, when the platform allows one.

        Notification Center refuses notifications from an unbundled script, so
        this quietly degrades to the log until Yes, Dev ships as a signed .app.
        """
        log(f"notify: {message}")
        try:
            rumps.notification(APP_NAME, "", message)
        except Exception:
            pass

    # ---------- menu ----------

    def _build_menu(self) -> None:
        self.mi_status = rumps.MenuItem("Status: starting")
        self.mi_count = rumps.MenuItem("Approved: 0")
        self.mi_access = rumps.MenuItem("Accessibility: checking", callback=self.on_grant_accessibility)
        self.mi_on = rumps.MenuItem("On", callback=self.on_toggle_enabled)
        self.mi_relay = rumps.MenuItem("Fast focus: off")
        self.menu = [
            self.mi_status, self.mi_count, self.mi_access, None, self.mi_on,
            rumps.MenuItem("Settings…", callback=self.on_settings), self.mi_relay, None,
            rumps.MenuItem("Open log", callback=self.on_open(LOG_PATH)),
            rumps.MenuItem("Open relay log", callback=self.on_open(DATA_DIR / "relay.log")), None,
            rumps.MenuItem("Quit", callback=self.on_quit),
        ]

    def _size_icon(self, path: str) -> None:
        """Give the status image its real aspect ratio.

        rumps' icon setter hardcodes setSize_((20, 20)), which squares a cloud
        that is not square and leaves it small in the bar. There is no supported
        way to pass a size through App.icon, so the NSImage it just built is
        resized in place and the status item asked to pick it up again - the same
        two calls rumps' own setter makes, in the same order.
        """
        image = getattr(self, "_icon_nsimage", None)
        if image is None:
            return
        try:
            image.setSize_(icon_point_size(path))
            nsapp = getattr(self, "_nsapp", None)
            if nsapp is not None:
                nsapp.setStatusBarIcon()
        except Exception:
            # Cosmetic only: if a future rumps renames either of these, the icon
            # is merely back to being square rather than the app being broken.
            log("could not resize the status icon - falling back to rumps' square")

    def refresh(self) -> None:
        try:
            state = self.state()
            if state != self._icon_state:
                self.icon = icon_path(state)
                self._size_icon(icon_path(state))
                self._icon_state = state

            bits = [f"Status: {self.state_text()}"]
            if self.resume_at:
                left = max(0, int((self.resume_at - datetime.now()).total_seconds()))
                bits.append(f"back in {left}s")
            elif self.disarm_at and not self.paused_reason:
                left = int((self.disarm_at - datetime.now()).total_seconds() // 60) + 1
                bits.append(f"{left} min left")
            self.mi_status.title = "  -  ".join(bits)
            self.mi_count.title = f"Approved: {self.approvals}"

            trusted = is_trusted()
            self.mi_access.title = ("Accessibility: granted" if trusted
                                    else "Accessibility: NOT granted - click to fix")

            self.mi_on.state = 1 if (self.cfg["enabled"] and not self.paused_reason) else 0
            self.mi_relay.title = self.relay_status_text()
            if self._settings is not None and self._settings.window.isVisible():
                self._settings.refresh_status()
        except Exception:
            import traceback
            log(f"refresh error: {traceback.format_exc()}")

    # ---------- menu actions ----------

    def on_settings(self, _item=None) -> None:
        if self._settings is None:
            from settings_mac import SettingsWindow
            self._settings = SettingsWindow(self)
        self._settings.show()

    def apply_settings(self, values, start_at_login) -> None:
        """Commit validated values before reconciling the running helpers."""
        old = dict(self.cfg)
        save_atomic(CONFIG_PATH, values)  # errors stay visible in Settings
        self.cfg.clear()
        self.cfg.update(values)
        if old["enabled"] != values["enabled"]:
            self.paused_reason = self.resume_at = self.allow_until = None
            self.recent.clear()
        restart = any(old.get(key) != values.get(key) for key in
                      ("poll_ms", "observe_only", "include_edge", "diagnostics"))
        if not values["enabled"] or self.paused_reason:
            self.stop_engine()
        elif restart or not self.engine_running():
            self.stop_engine()
            self.start_engine()
        if any(old.get(key) != values.get(key) for key in
               ("relay_enabled", "relay_port", "relay_profile", "relay_hold")):
            self.stop_relay()
            self._relay_retry_at = 0
        if old["arm_minutes"] != values["arm_minutes"] and self.engine_running():
            mins = values["arm_minutes"]
            self.disarm_at = datetime.now() + timedelta(minutes=mins) if mins else None
        self.sync_guard()
        self.refresh()
        if start_at_login != platform_mac.autostart_enabled():
            try:
                if start_at_login:
                    platform_mac.enable_autostart(Path(__file__).resolve())
                else:
                    platform_mac.disable_autostart()
            except Exception as exc:
                raise RuntimeError(f"Settings saved, but Start at login could not be changed: {exc}") from exc

    def on_toggle_enabled(self, _item) -> None:
        self.cfg["enabled"] = not self.cfg["enabled"]
        self.paused_reason = None
        self.resume_at = None
        self.allow_until = None
        self.recent.clear()
        self.cfg.save()
        if self.cfg["enabled"]:
            self.start_engine()
        else:
            self.stop_engine()
        self.refresh()

    def on_grant_accessibility(self, _item) -> None:
        """Ask for the grant, and open the pane so it can be given.

        Without it every AX read returns empty and the engine looks broken
        rather than unpermitted, so this is a first-class menu item.
        """
        if is_trusted(prompt=True):
            self.refresh()
            return
        try:
            subprocess.run(["open", accessibility_settings_url()], check=False)
        except Exception:
            pass
        self.refresh()

    def on_open(self, path: Path):
        def handler(_item) -> None:
            try:
                if not path.exists():
                    ensure_data_dir()
                    path.touch()
                subprocess.run(["open", str(path)], check=False)
            except Exception:
                pass
        return handler

    def on_quit(self, _item) -> None:
        self.shutdown()
        rumps.quit_application()

    def shutdown(self) -> None:
        self.stop_engine()           # stops the guard too
        if self._asking is not None:
            try:
                self._asking.kill()
            except Exception:
                pass
        if self._puffs:
            self._puffs.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", action="store_true", help="open the settings window on launch")
    opts = parser.parse_args()
    # One tray instance; a second would fight the first over the engine.
    if not platform_mac.acquire_single_instance("tray"):
        log("another instance is already running - exiting")
        return 0

    # A bare `python3 yes_dev_mac.py` run has no Info.plist to set
    # LSUIElement, so AppKit defaults to a regular app - Dock icon, Cmd-Tab
    # entry, the lot. Accessory matches what a proper .app bundle would
    # declare: menu-bar item only, no Dock presence.
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory)

    app = YesDev()
    if not is_trusted():
        log("Accessibility NOT granted - the engine cannot click until it is. "
            "Use the menu's Accessibility item, or System Settings > Privacy & "
            "Security > Accessibility.")

    # Quit from the menu cleans up through on_quit, but a tray that is killed
    # rather than asked (logout, a stray kill, the terminal that launched it going
    # away) would otherwise take its `finally` with it and orphan the engine.
    # The engine's own --exit-with-parent is the real backstop; this makes the
    # common case tidy and immediate. The handler only runs when the interpreter
    # next executes Python, which the 1s timer below guarantees.
    def _on_signal(signum, _frame):
        log(f"signal {signum} - shutting down")
        app.shutdown()
        rumps.quit_application()

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_sig, _on_signal)
        except (ValueError, OSError):
            pass

    rumps.Timer(app._tick, 1).start()
    if opts.settings:
        # Wait until rumps has created the status item and finished launching.
        def show_settings(timer):
            timer.stop()
            app.on_settings()
        rumps.Timer(show_settings, .25).start()
    try:
        app.run()
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
