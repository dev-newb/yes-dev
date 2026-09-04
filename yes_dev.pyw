"""Yes, Dev - Windows tray control for the Chrome remote-debugging auto-approver.

The PowerShell engine (watcher.ps1) does the UI Automation work. This owns its
lifetime, exposes the options in a tray menu, and tails the log so approvals are
visible and a runaway burst can be caught.

Run with pythonw.exe so no console window appears.
"""
from __future__ import annotations

import ctypes
import json
import os
import random
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

import puffs        # the cloud artwork; the status icon is the app's own cloud

APP_NAME = "Yes, Dev"
APP_SLUG = "yes-dev"

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))   # so `import puffs` works however we were launched
WATCHER = BASE / "watcher.ps1"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "YesDev"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / f"{APP_SLUG}.log"
TRAY_LOG = DATA_DIR / "tray.log"
STARTUP_LNK = (
    Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
    / "Start Menu" / "Programs" / "Startup" / f"{APP_NAME}.lnk"
)

CREATE_NO_WINDOW = 0x08000000

ICON_PX = 128        # drawn large; Windows scales it down for the notification area
ICON_SEED = 7        # one silhouette for every state - matches the macOS build, so
                     # both platforms wear the same cloud. Only the colour changes.
CHECK_SCALE = 1.34   # the check is oversized on purpose; this small, a thin one
                     # turns to mush

DEFAULTS = {
    "enabled": True,
    "observe_only": False,
    "poll_ms": 250,
    "include_edge": False,
    # How routine approvals are announced: "puffs", "toast" or "none".
    # Warnings (burst guard, auto-disarm) always use a toast - they carry text.
    "notify_style": "puffs",
    # Approvals per minute before pausing; 0 disables the guard. Measured normal
    # load for several parallel agents peaks around 15/min, so this leaves real
    # headroom while still catching a runaway loop (which runs orders higher).
    "burst_limit": 60,
    # What a burst does: "ask" puts up a 5s dialog, "stop" acts without asking.
    "burst_action": "ask",
    # Minutes to stay armed before disarming automatically; 0 = until turned off.
    "arm_minutes": 0,
}

BURST_WINDOW = 60.0    # seconds
BURST_COOLDOWN = 60.0  # auto-resume after this long, rather than stranding agents
BURST_CHOICES = [("Off", 0), ("30 / min", 30), ("60 / min", 60), ("120 / min", 120)]
BURST_ACTIONS = [("Ask me first (5s)", "ask"), ("Stop silently", "stop")]
BURST_DIALOG = BASE / "burst_dialog.py"
ALLOW_HOUR = 3600.0

POLL_CHOICES = [("Snappy (150ms)", 150), ("Normal (250ms)", 250), ("Relaxed (750ms)", 750)]
ARM_CHOICES = [("Until I turn it off", 0), ("15 minutes", 15), ("1 hour", 60), ("4 hours", 240)]
NOTIFY_CHOICES = [
    ("Floating puffs", "puffs"),
    ("Toast card", "toast"),
    ("Silent", "none"),
]


def log(message: str) -> None:
    """Tray-side log. Under pythonw there is no console, so failures are invisible
    without this - the puff overlay in particular fails quietly if Tk is unhappy."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if TRAY_LOG.exists() and TRAY_LOG.stat().st_size > 1_000_000:
            TRAY_LOG.replace(TRAY_LOG.with_name(TRAY_LOG.name + ".1"))
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with TRAY_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def run_hidden(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, creationflags=CREATE_NO_WINDOW, capture_output=True, text=True
    )


class Config(dict):
    def __init__(self) -> None:
        super().__init__(DEFAULTS)
        if CONFIG_PATH.exists():
            try:
                # utf-8-sig, not utf-8: Notepad and PowerShell's -Encoding utf8 both
                # write a BOM, and a plain utf-8 read would reject the whole file and
                # silently reset every setting to its default.
                self.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
            except Exception as exc:
                log(f"config unreadable ({exc!r}) - using defaults")

        # Config from before puffs existed carried a notify_on_approve bool.
        old = self.pop("notify_on_approve", None)
        if old is not None and "notify_style" not in self:
            self["notify_style"] = "puffs" if old else "none"
        if self.get("notify_style") not in {"puffs", "toast", "none"}:
            self["notify_style"] = DEFAULTS["notify_style"]

        # The guard used to be an on/off flag with a fixed limit of 15/min, which
        # sat right on top of normal multi-agent load.
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
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(self, indent=2), encoding="utf-8")
        except Exception:
            pass


class YesDev:
    def __init__(self) -> None:
        self.cfg = Config()
        self.proc: subprocess.Popen | None = None
        self.approvals = 0
        self.recent: deque[float] = deque()      # timestamps, for the burst guard
        self.disarm_at: datetime | None = None
        self.paused_reason: str | None = None
        self.resume_at: datetime | None = None
        self.allow_until: datetime | None = None   # burst guard snoozed by the user
        self.icon: pystray.Icon | None = None
        self._log_pos = 0
        self._lock = threading.Lock()
        self._puffs = None            # built on first use; Tk costs a thread

    # ---------- engine lifetime ----------

    def engine_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start_engine(self) -> None:
        if self.engine_running():
            return
        browsers = "chrome,msedge" if self.cfg["include_edge"] else "chrome"
        args = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-File", str(WATCHER),
            "-IntervalMs", str(self.cfg["poll_ms"]),
            "-LogPath", str(LOG_PATH),
            "-BrowserProcess", browsers,
            # So the engine dies with us: it must never be left approving
            # prompts with no tray watching the rate.
            "-ParentPid", str(os.getpid()),
        ]
        if self.cfg["observe_only"]:
            args.append("-Observe")

        # Only surface approvals logged from here on, not the whole history.
        self._log_pos = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
        self.proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
        log(f"engine started pid={self.proc.pid} log_pos={self._log_pos}")

        mins = int(self.cfg["arm_minutes"] or 0)
        self.disarm_at = datetime.now() + timedelta(minutes=mins) if mins else None
        self.paused_reason = None

    def stop_engine(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            # Kill the tree: PowerShell may outlive a plain terminate().
            run_hidden(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"])
        self.proc = None
        self.disarm_at = None

    def restart_engine(self) -> None:
        """Apply an option that the engine reads only at startup."""
        if self.cfg["enabled"] and not self.paused_reason:
            self.stop_engine()
            time.sleep(0.4)   # let the global mutex release
            self.start_engine()

    # ---------- background loop ----------

    def monitor(self) -> None:
        log("monitor thread started")
        while True:
            try:
                self._tick()
            except Exception:
                import traceback
                log(f"tick error: {traceback.format_exc()}")
            time.sleep(1.0)

    def _tick(self) -> None:
        with self._lock:
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
                    self.start_engine()          # first run, or engine died - restart it
                if self.disarm_at and datetime.now() >= self.disarm_at:
                    self._pause("timer expired")
                    self.notify(f"Disarmed after {self.cfg['arm_minutes']} min")
            self._read_log()

    def _read_log(self) -> None:
        if not LOG_PATH.exists():
            return
        size = LOG_PATH.stat().st_size
        if size < self._log_pos:
            self._log_pos = 0                    # log was rotated or cleared
        if size == self._log_pos:
            return
        # Binary, not text: the engine's log is UTF-8-with-BOM and CRLF, and byte
        # offsets from stat() are only meaningful against a binary stream.
        try:
            with LOG_PATH.open("rb") as fh:
                fh.seek(self._log_pos)
                chunk = fh.read().decode("utf-8", errors="replace")
                self._log_pos = fh.tell()
        except OSError:
            # The engine holds a brief write lock on every append. Leave _log_pos
            # alone and pick the same bytes up on the next tick.
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

        log(f"burst of {count} - asking the user")
        choice = "stop"
        try:
            pyw = Path(sys.executable)
            exe = pyw.with_name("pythonw.exe") if pyw.with_name("pythonw.exe").exists() else pyw
            done = subprocess.run(
                [str(exe), str(BURST_DIALOG), str(count), APP_NAME],
                capture_output=True, text=True, timeout=30,
                creationflags=CREATE_NO_WINDOW)
            choice = (done.stdout or "").strip().splitlines()[-1:] or ["stop"]
            choice = choice[0]
        except Exception as exc:
            log(f"  burst dialog failed ({exc!r}) - stopping, the safe answer")

        if choice == "allow_hour":
            self.allow_until = datetime.now() + timedelta(seconds=ALLOW_HOUR)
            self.recent.clear()
            log("  user allowed bursts for one hour")
            self.notify(f"{APP_NAME} will keep approving for one hour")
        else:
            log("  user stopped the app (or let the timer run out)")
            self._pause("stopped after burst")   # no auto-resume: this was deliberate
            self.notify(f"{APP_NAME} stopped. Turn it back on from the tray when ready.")

        self.refresh()

    def announce(self, hits: int) -> None:
        """Routine approvals: a puff per approval, a toast, or nothing."""
        style = self.cfg["notify_style"]
        log(f"announce hits={hits} style={style}")
        if style == "toast":
            self.notify(f"Approved {hits} debugging request{'s' if hits > 1 else ''}")
        elif style == "puffs":
            overlay = self.puff_overlay()
            if overlay is not None:
                overlay.emit(hits)
                log(f"  emitted {hits} puff(s)")

    def puff_overlay(self):
        if self._puffs is None:
            try:
                import puffs
                self._puffs = puffs.PuffClient(log=log)
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

    # ---------- tray plumbing ----------

    def state(self) -> str:
        if self.paused_reason:
            return f"paused ({self.paused_reason})"
        if not self.cfg["enabled"]:
            return "off"
        if self.cfg["observe_only"]:
            return "observing"
        return "on"

    def color(self) -> tuple[int, int, int]:
        if self.paused_reason:
            return (218, 54, 51)          # red
        if not self.cfg["enabled"]:
            return (140, 140, 140)        # grey
        if self.cfg["observe_only"]:
            return (219, 154, 4)          # amber
        return (46, 160, 67)              # green

    def make_image(self) -> Image.Image:
        """The app's own cloud in the state colour, with a white check in front.

        The same silhouette as the macOS status icon - same seed - but the right
        way up. On Windows the clouds rise from the tray; on macOS they hang from
        the menu bar, so that build flips it. A plain circle read as an anonymous
        dot among the notification-area icons, where the silhouette is the only
        thing that says which app this is.
        """
        # Seeded: _render_cloud jitters its lobes per call, and an icon that
        # changed shape whenever it changed colour would look like a different app.
        random.seed(ICON_SEED)
        mask = puffs._render_cloud(ICON_PX, premultiply=False).split()[3]

        art = Image.new("RGBA", mask.size, (0, 0, 0, 0))
        body = Image.new("RGBA", mask.size, self.color() + (255,))
        body.putalpha(mask)
        art.alpha_composite(body)

        # Upright, the cloud's mass sits below its centre line, so the check goes
        # with it rather than floating in the lobes.
        w, h = mask.size
        cx, cy = w / 2, h * 0.60
        s = w * 0.0092 * CHECK_SCALE
        ImageDraw.Draw(art).line(
            [(cx - 13 * s, cy + 0.5 * s), (cx - 4.5 * s, cy + 9.5 * s),
             (cx + 13.5 * s, cy - 10 * s)],
            fill=(255, 255, 255, 255), width=max(1, int(5.6 * s)), joint="curve")

        # _render_cloud leaves ~15% margins for its blur. Keeping them would pay
        # for empty space in an icon that is only a few pixels tall on screen, so
        # crop to the art and centre it in the square the tray expects.
        art = art.crop(art.getbbox())
        side = max(art.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.alpha_composite(art, ((side - art.width) // 2, (side - art.height) // 2))
        return square

    def notify(self, message: str) -> None:
        if self.icon is None:
            return
        try:
            self.icon.notify(message, APP_NAME)
        except Exception:
            pass

    def refresh(self) -> None:
        if self.icon is None:
            return
        try:
            self.icon.icon = self.make_image()
            self.icon.title = f"{APP_NAME} - {self.state()} - {self.approvals} approved"
            self.icon.update_menu()
        except Exception:
            pass

    # ---------- menu actions ----------

    def toggle_enabled(self) -> None:
        with self._lock:
            self.cfg["enabled"] = not self.cfg["enabled"]
            self.paused_reason = None
            self.resume_at = None
            self.allow_until = None
            self.recent.clear()
            self.cfg.save()
            self.start_engine() if self.cfg["enabled"] else self.stop_engine()
        self.refresh()

    def toggle(self, key: str, restart: bool = False):
        def handler() -> None:
            with self._lock:
                self.cfg[key] = not self.cfg[key]
                self.cfg.save()
                if restart:
                    self.restart_engine()
            self.refresh()
        return handler

    def set_value(self, key: str, value, restart: bool = False):
        def handler() -> None:
            with self._lock:
                self.cfg[key] = value
                self.cfg.save()
                if key == "arm_minutes":
                    mins = int(value or 0)
                    self.disarm_at = datetime.now() + timedelta(minutes=mins) if mins else None
                if restart:
                    self.restart_engine()
            self.refresh()
        return handler

    def autostart_on(self) -> bool:
        return STARTUP_LNK.exists()

    def toggle_autostart(self) -> None:
        if self.autostart_on():
            try:
                STARTUP_LNK.unlink()
            except Exception:
                pass
        else:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            exe = pyw if pyw.exists() else Path(sys.executable)
            ps = (
                f"$s=(New-Object -COM WScript.Shell).CreateShortcut('{STARTUP_LNK}');"
                f"$s.TargetPath='{exe}';"
                f"$s.Arguments='\"{Path(__file__).resolve()}\"';"
                f"$s.WorkingDirectory='{BASE}';"
                f"$s.Description='{APP_NAME}: auto-approve Chrome remote debugging prompts';"
                f"$s.Save()"
            )
            run_hidden(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
        self.refresh()

    def status_text(self, _item) -> str:
        bits = [f"Status: {self.state()}"]
        if self.resume_at:
            bits.append(f"back in {max(0, int((self.resume_at - datetime.now()).total_seconds()))}s")
        elif self.disarm_at and not self.paused_reason:
            left = int((self.disarm_at - datetime.now()).total_seconds() // 60) + 1
            bits.append(f"{left} min left")
        return "  -  ".join(bits)

    def open_path(self, path: Path):
        def handler() -> None:
            try:
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                os.startfile(str(path))  # noqa: S606 - opening our own file in the shell
            except Exception:
                pass
        return handler

    def quit(self) -> None:
        self.stop_engine()
        if self._puffs:
            self._puffs.shutdown()
        if self.icon:
            self.icon.stop()

    def build_menu(self) -> pystray.Menu:
        M, I = pystray.Menu, pystray.MenuItem
        return M(
            I(self.status_text, None, enabled=False),
            I(lambda _: f"Approved: {self.approvals}", None, enabled=False),
            M.SEPARATOR,
            I("On", lambda _: self.toggle_enabled(),
              checked=lambda _: self.cfg["enabled"] and not self.paused_reason),
            I("Start at login", lambda _: self.toggle_autostart(),
              checked=lambda _: self.autostart_on()),
            M.SEPARATOR,
            I("Stay on for", M(*[
                I(label, lambda _, v=val: self.set_value("arm_minutes", v)(),
                  checked=lambda _, v=val: self.cfg["arm_minutes"] == v, radio=True)
                for label, val in ARM_CHOICES
            ])),
            I("Speed", M(*[
                I(label, lambda _, v=val: self.set_value("poll_ms", v, restart=True)(),
                  checked=lambda _, v=val: self.cfg["poll_ms"] == v, radio=True)
                for label, val in POLL_CHOICES
            ])),
            I("Approve notice", M(*[
                I(label, lambda _, v=val: self.set_value("notify_style", v)(),
                  checked=lambda _, v=val: self.cfg["notify_style"] == v, radio=True)
                for label, val in NOTIFY_CHOICES
            ])),
            I("Pause on burst", M(
                *[I(label, lambda _, v=val: self.set_value("burst_limit", v)(),
                    checked=lambda _, v=val: self.cfg["burst_limit"] == v, radio=True)
                  for label, val in BURST_CHOICES],
                M.SEPARATOR,
                *[I(label, lambda _, v=val: self.set_value("burst_action", v)(),
                    checked=lambda _, v=val: self.cfg["burst_action"] == v,
                    radio=True, enabled=lambda _: bool(self.cfg["burst_limit"]))
                  for label, val in BURST_ACTIONS],
            )),
            I("Options", M(
                I("Observe only (log, don't click)",
                  lambda _: self.toggle("observe_only", restart=True)(),
                  checked=lambda _: self.cfg["observe_only"]),
                I("Include Microsoft Edge",
                  lambda _: self.toggle("include_edge", restart=True)(),
                  checked=lambda _: self.cfg["include_edge"]),
            )),
            M.SEPARATOR,
            I("Open log", self.open_path(LOG_PATH)),
            I("Open config", self.open_path(CONFIG_PATH)),
            I("Quit", lambda _: self.quit()),
        )

    def run(self) -> None:
        self.cfg.save()
        self.icon = pystray.Icon(
            APP_SLUG, self.make_image(),
            f"{APP_NAME} - starting", self.build_menu(),
        )
        threading.Thread(target=self.monitor, daemon=True).start()
        self.icon.run()


def main() -> int:
    # One tray instance; a second would fight the first over the engine.
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\YesDevTray")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return 0
    app = YesDev()
    try:
        app.run()
    finally:
        app.stop_engine()
        if app._puffs:
            app._puffs.shutdown()
        ctypes.windll.kernel32.ReleaseMutex(handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
