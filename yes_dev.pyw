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
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

APP_NAME = "Yes, Dev"
APP_SLUG = "yes-dev"

BASE = Path(__file__).resolve().parent
WATCHER = BASE / "watcher.ps1"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "YesDev"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / f"{APP_SLUG}.log"
STARTUP_LNK = (
    Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
    / "Start Menu" / "Programs" / "Startup" / f"{APP_NAME}.lnk"
)

CREATE_NO_WINDOW = 0x08000000

DEFAULTS = {
    "enabled": True,
    "observe_only": False,
    "poll_ms": 250,
    "include_edge": False,
    "notify_on_approve": True,
    "burst_guard": True,
    # Minutes to stay armed before disarming automatically; 0 = until turned off.
    "arm_minutes": 0,
}

BURST_LIMIT = 15       # approvals within BURST_WINDOW before auto-pausing
BURST_WINDOW = 60.0    # seconds

POLL_CHOICES = [("Snappy (150ms)", 150), ("Normal (250ms)", 250), ("Relaxed (750ms)", 750)]
ARM_CHOICES = [("Until I turn it off", 0), ("15 minutes", 15), ("1 hour", 60), ("4 hours", 240)]


def run_hidden(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, creationflags=CREATE_NO_WINDOW, capture_output=True, text=True
    )


class Config(dict):
    def __init__(self) -> None:
        super().__init__(DEFAULTS)
        try:
            self.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass  # first run, or a hand-edit we can't parse - fall back to defaults

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
        self.icon: pystray.Icon | None = None
        self._log_pos = 0
        self._lock = threading.Lock()

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
        ]
        if self.cfg["observe_only"]:
            args.append("-Observe")

        # Only surface approvals logged from here on, not the whole history.
        self._log_pos = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
        self.proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)

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
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(1.0)

    def _tick(self) -> None:
        with self._lock:
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
        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self._log_pos)
            chunk = fh.read()
            self._log_pos = fh.tell()

        hits = sum(1 for line in chunk.splitlines() if "[ACTION]" in line)
        if not hits:
            return

        self.approvals += hits
        now = time.time()
        self.recent.extend([now] * hits)
        while self.recent and now - self.recent[0] > BURST_WINDOW:
            self.recent.popleft()

        if self.cfg["burst_guard"] and len(self.recent) >= BURST_LIMIT:
            self._pause("burst guard")
            self.notify(
                f"Paused: {len(self.recent)} approvals in under a minute. "
                f"Turn {APP_NAME} back on if that was expected."
            )
        elif self.cfg["notify_on_approve"]:
            self.notify(f"Approved {hits} debugging request{'s' if hits > 1 else ''}")

        self.refresh()

    def _pause(self, reason: str) -> None:
        self.paused_reason = reason
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
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([2, 2, size - 2, size - 2], fill=self.color())
        # Checkmark: it says yes.
        d.line([(18, 33), (28, 44), (46, 20)], fill=(255, 255, 255, 255), width=8, joint="curve")
        return img

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
        if self.disarm_at and not self.paused_reason:
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
            I("Options", M(
                I("Notify on approve", lambda _: self.toggle("notify_on_approve")(),
                  checked=lambda _: self.cfg["notify_on_approve"]),
                I(f"Pause on burst ({BURST_LIMIT}/min)", lambda _: self.toggle("burst_guard")(),
                  checked=lambda _: self.cfg["burst_guard"]),
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
        ctypes.windll.kernel32.ReleaseMutex(handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
