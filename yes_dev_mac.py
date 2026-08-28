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

import json
import random
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

from PIL import Image, ImageDraw, ImageOps

import platform_mac
import puffs                     # the icon is drawn from the same cloud renderer
from platform_mac import (
    APP_NAME, APP_SLUG, CONFIG_PATH, DATA_DIR, LOG_PATH, TRAY_LOG,
    accessibility_settings_url, ensure_data_dir, is_trusted,
)

WATCHER = BASE / "watcher_mac.py"
OVERLAY = BASE / "puffs_mac.py"
BURST_DIALOG = BASE / "burst_dialog.py"

# The config schema is shared with the Windows build byte for byte - the same
# file, the same keys - so a synced config works on either. Keep them in step.
DEFAULTS = {
    "enabled": True,
    "observe_only": False,
    "poll_ms": 250,
    "include_edge": False,
    # How routine approvals are announced: "puffs", "toast" or "none".
    "notify_style": "puffs",
    # Approvals per minute before pausing; 0 disables the guard.
    "burst_limit": 60,
    # What a burst does: "ask" puts up a 5s dialog, "stop" acts without asking.
    "burst_action": "ask",
    # Minutes to stay armed before disarming automatically; 0 = until turned off.
    "arm_minutes": 0,
}

BURST_WINDOW = 60.0    # seconds
BURST_COOLDOWN = 60.0  # auto-resume after this long, rather than stranding agents
ALLOW_HOUR = 3600.0

BURST_CHOICES = [("Off", 0), ("30 / min", 30), ("60 / min", 60), ("120 / min", 120)]
BURST_ACTIONS = [("Ask me first (5s)", "ask"), ("Stop silently", "stop")]
POLL_CHOICES = [("Snappy (150ms)", 150), ("Normal (250ms)", 250), ("Relaxed (750ms)", 750)]
ARM_CHOICES = [("Until I turn it off", 0), ("15 minutes", 15), ("1 hour", 60), ("4 hours", 240)]
NOTIFY_CHOICES = [("Floating puffs", "puffs"), ("Toast card", "toast"), ("Silent", "none")]

# rumps renders the status item image at 20x20 points; drawing at 2x keeps it
# crisp on retina, the same trick puffs_mac.py uses for the clouds.
ICON_PT = 20
ICON_PX = ICON_PT * 2
ICON_SS = 6          # draw this much larger, then downscale, for clean edges
ICON_SEED = 7        # one silhouette for every state: the icon must not change
                     # shape when it changes colour, only its colour
CHECK_SCALE = 1.34   # the check is oversized on purpose; at 20pt a thin one
                     # turns to mush
ICON_VERSION = 2     # bump when the drawing changes, so cached files are dropped

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

    R = ICON_PX * ICON_SS
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

    canvas.resize((ICON_PX, ICON_PX), Image.LANCZOS).save(path)
    return str(path)


class Config(dict):
    def __init__(self) -> None:
        super().__init__(DEFAULTS)
        if CONFIG_PATH.exists():
            try:
                # utf-8-sig, kept from the Windows build: nothing on macOS writes
                # a BOM, but a config synced from a Windows machine has one, and
                # utf-8-sig reads files with and without it. We always write plain
                # utf-8 (see save()).
                self.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
            except Exception as exc:
                log(f"config unreadable ({exc!r}) - using defaults")

        # Config from before puffs existed carried a notify_on_approve bool.
        old = self.pop("notify_on_approve", None)
        if old is not None and "notify_style" not in self:
            self["notify_style"] = "puffs" if old else "none"
        if self.get("notify_style") not in {"puffs", "toast", "none"}:
            self["notify_style"] = DEFAULTS["notify_style"]

        # The guard used to be an on/off flag with a fixed limit of 15/min.
        guard = self.pop("burst_guard", None)
        if guard is not None and "burst_limit" not in self:
            self["burst_limit"] = DEFAULTS["burst_limit"] if guard else 0
        try:
            self["burst_limit"] = max(0, int(self["burst_limit"]))
        except (TypeError, ValueError):
            self["burst_limit"] = DEFAULTS["burst_limit"]
        if self.get("burst_action") not in {"ask", "stop"}:
            self["burst_action"] = DEFAULTS["burst_action"]

    def save(self) -> None:
        try:
            ensure_data_dir()
            CONFIG_PATH.write_text(json.dumps(self, indent=2), encoding="utf-8")
        except Exception:
            pass


class YesDev(rumps.App):
    def __init__(self) -> None:
        self.cfg = Config()
        super().__init__(APP_NAME, title=None, icon=icon_path("off"), quit_button=None)

        self.proc: subprocess.Popen | None = None
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

        # Materialise the config on first run, so "Open config" has something to
        # open and the defaults are visible rather than implied.
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
                "--log-path", str(LOG_PATH)]
        if self.cfg["observe_only"]:
            args.append("--observe")
        if self.cfg["include_edge"]:
            args.append("--include-edge")

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

    def stop_engine(self) -> None:
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

    def restart_engine(self) -> None:
        """Apply an option that the engine reads only at startup."""
        if self.cfg["enabled"] and not self.paused_reason:
            self.stop_engine()
            time.sleep(0.2)
            self.start_engine()

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
        self.mi_access = rumps.MenuItem("Accessibility: checking",
                                        callback=self.on_grant_accessibility)
        self.mi_on = rumps.MenuItem("On", callback=self.on_toggle_enabled)
        self.mi_autostart = rumps.MenuItem("Start at login",
                                           callback=self.on_toggle_autostart)
        self.mi_observe = rumps.MenuItem("Observe only (log, don't click)",
                                         callback=self.on_toggle("observe_only", restart=True))
        self.mi_edge = rumps.MenuItem("Include Microsoft Edge",
                                      callback=self.on_toggle("include_edge", restart=True))

        # Radio groups: rumps has no radio flag, so the check marks are managed
        # in refresh() from the config, which is the single source of truth.
        self.radios: dict[str, list[tuple[rumps.MenuItem, object]]] = {}

        def group(key: str, choices, restart: bool = False):
            items = []
            for label, value in choices:
                item = rumps.MenuItem(label, callback=self.on_set(key, value, restart))
                items.append((item, value))
            self.radios[key] = items
            return [i for i, _ in items]

        self.menu = [
            self.mi_status,
            self.mi_count,
            self.mi_access,
            None,
            self.mi_on,
            self.mi_autostart,
            None,
            ["Stay on for", group("arm_minutes", ARM_CHOICES)],
            ["Speed", group("poll_ms", POLL_CHOICES, restart=True)],
            ["Approve notice", group("notify_style", NOTIFY_CHOICES)],
            ["Pause on burst", group("burst_limit", BURST_CHOICES)
                               + [None] + group("burst_action", BURST_ACTIONS)],
            ["Options", [self.mi_observe, self.mi_edge]],
            None,
            rumps.MenuItem("Open log", callback=self.on_open(LOG_PATH)),
            rumps.MenuItem("Open config", callback=self.on_open(CONFIG_PATH)),
            None,
            rumps.MenuItem("Quit", callback=self.on_quit),
        ]

    def refresh(self) -> None:
        try:
            state = self.state()
            if state != self._icon_state:
                self.icon = icon_path(state)
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
            self.mi_autostart.state = 1 if platform_mac.autostart_enabled() else 0
            self.mi_observe.state = 1 if self.cfg["observe_only"] else 0
            self.mi_edge.state = 1 if self.cfg["include_edge"] else 0
            for key, items in self.radios.items():
                for item, value in items:
                    item.state = 1 if self.cfg.get(key) == value else 0
        except Exception:
            import traceback
            log(f"refresh error: {traceback.format_exc()}")

    # ---------- menu actions ----------

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

    def on_toggle(self, key: str, restart: bool = False):
        def handler(_item) -> None:
            self.cfg[key] = not self.cfg[key]
            self.cfg.save()
            if restart:
                self.restart_engine()
            self.refresh()
        return handler

    def on_set(self, key: str, value, restart: bool = False):
        def handler(_item) -> None:
            self.cfg[key] = value
            self.cfg.save()
            if key == "arm_minutes":
                mins = int(value or 0)
                self.disarm_at = datetime.now() + timedelta(minutes=mins) if mins else None
            if restart:
                self.restart_engine()
            self.refresh()
        return handler

    def on_toggle_autostart(self, _item) -> None:
        if platform_mac.autostart_enabled():
            platform_mac.disable_autostart()
        else:
            platform_mac.enable_autostart(Path(__file__).resolve())
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
        self.stop_engine()
        if self._asking is not None:
            try:
                self._asking.kill()
            except Exception:
                pass
        if self._puffs:
            self._puffs.shutdown()


def main() -> int:
    # One tray instance; a second would fight the first over the engine.
    if not platform_mac.acquire_single_instance("tray"):
        log("another instance is already running - exiting")
        return 0

    app = YesDev()
    if not is_trusted():
        log("Accessibility NOT granted - the engine cannot click until it is. "
            "Use the menu's Accessibility item, or System Settings > Privacy & "
            "Security > Accessibility.")

    rumps.Timer(app._tick, 1).start()
    try:
        app.run()
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
