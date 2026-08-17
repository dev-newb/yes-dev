"""Floating puff notifications - a wordless alternative to Windows toast cards.

Each approval releases a small translucent cloud near the tray that drifts up
and fades out. Everything is jittered - position, size, speed, lifetime, start
delay - so a burst of approvals scatters instead of stacking into one blob.

The windows are layered, click-through and non-activating, so they never take
focus or intercept a click.

Tk gets its own process on purpose. Toplevels created from a worker thread are
created and reported visible by Windows but never paint (verified: identical
code paints fine on the main thread, renders solid black off it), and the tray
already owns the main thread of the parent. So the parent runs PuffClient, which
speaks one integer per line over stdin to this module running as `--serve`.
"""
from __future__ import annotations

import ctypes
import os
import queue
import random
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
SPI_GETWORKAREA = 0x0030
CREATE_NO_WINDOW = 0x08000000

# Any colour that won't show up in the artwork; these pixels become fully
# transparent AND click-through.
COLOR_KEY = "#010203"
CLOUD_FILL = "#dcebff"
CLOUD_EDGE = "#7aa7e0"   # keeps the shape readable on light backgrounds too

FRAME_MS = 16          # ~60fps
MAX_LIVE = 40          # hard cap, so a runaway loop can't spawn endless windows


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _work_area() -> _Rect:
    """Desktop area excluding the taskbar."""
    r = _Rect()
    try:
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    except Exception:
        r.left, r.top, r.right, r.bottom = 0, 0, 1920, 1040
    return r


def _make_click_through(win: tk.Toplevel) -> None:
    """Layered + transparent + no-activate, so clicks and focus pass straight through."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
        )
    except Exception:
        pass


class _Puff:
    """One cloud: rises, drifts sideways a little, fades out, then destroys itself."""

    def __init__(self, root: tk.Tk) -> None:
        area = _work_area()
        self.size = random.randint(26, 46)
        self.alpha = random.uniform(0.55, 0.80)
        self.fade = self.alpha / random.uniform(95, 150)   # frames to live
        self.rise = random.uniform(0.8, 1.7)
        self.drift = random.uniform(-0.35, 0.35)

        # Anchor near the tray, with enough spread that a burst never stacks.
        self.x = float(area.right - random.randint(60, 210))
        self.y = float(area.bottom - random.randint(45, 120))

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", self.alpha)
        self.win.configure(bg=COLOR_KEY)
        try:
            self.win.attributes("-transparentcolor", COLOR_KEY)
        except tk.TclError:
            pass   # very old Tk: the cloud still shows, just on a solid square

        s = self.size
        canvas = tk.Canvas(self.win, width=s, height=s, bg=COLOR_KEY,
                           highlightthickness=0, bd=0)
        canvas.pack()
        # Three overlapping blobs read as a cloud at this size.
        for box in ((0.02, 0.42, 0.52, 0.92), (0.46, 0.40, 0.98, 0.90),
                    (0.24, 0.16, 0.80, 0.74)):
            canvas.create_oval(s * box[0], s * box[1], s * box[2], s * box[3],
                               fill=CLOUD_FILL, outline=CLOUD_EDGE, width=1)

        self.win.geometry(f"{s}x{s}+{int(self.x)}+{int(self.y)}")
        # Realize and paint BEFORE touching the ex-style. Changing ex-style on a
        # not-yet-realized layered window drops its layered attributes and the
        # window stays invisible forever, so re-assert them afterwards too.
        self.win.update_idletasks()
        _make_click_through(self.win)
        self.win.attributes("-alpha", self.alpha)
        try:
            self.win.attributes("-transparentcolor", COLOR_KEY)
        except tk.TclError:
            pass

    def step(self) -> bool:
        """Advance one frame. Returns False once the puff is gone."""
        self.alpha -= self.fade
        if self.alpha <= 0.02:
            self.destroy()
            return False
        self.y -= self.rise
        self.x += self.drift
        try:
            self.win.attributes("-alpha", self.alpha)
            self.win.geometry(f"+{int(self.x)}+{int(self.y)}")
        except tk.TclError:
            return False
        return True

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass


def _debug(message: str) -> None:
    """Overlay-side diagnostics; only written when YESDEV_DEBUG is set."""
    if not os.environ.get("YESDEV_DEBUG"):
        return
    try:
        path = Path(os.environ.get("LOCALAPPDATA", ".")) / "YesDev" / "puffs.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{message}\n")
    except Exception:
        pass


def serve() -> None:
    """Run the overlay, taking one integer per line on stdin. Tk owns this thread."""
    try:
        root = tk.Tk()
        root.withdraw()
        _debug("serve: Tk root created")
    except Exception:
        import traceback
        _debug(f"serve: Tk FAILED {traceback.format_exc()}")
        return

    pending: queue.Queue[int | None] = queue.Queue()
    live: list[_Puff] = []
    delayed = [0]

    def read_stdin() -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line or line == "quit":
                    break
                try:
                    pending.put(max(1, min(int(line), MAX_LIVE)))
                except ValueError:
                    pass
        except Exception:
            pass
        pending.put(None)          # parent closed the pipe or asked us to stop

    threading.Thread(target=read_stdin, daemon=True).start()

    def release() -> None:
        delayed[0] = max(0, delayed[0] - 1)
        if len(live) >= MAX_LIVE:
            return
        try:
            live.append(_Puff(root))
            _debug(f"spawned puff, live={len(live)}")
        except Exception:
            import traceback
            _debug(f"spawn failed: {traceback.format_exc()}")

    def spawn(count: int) -> None:
        room = MAX_LIVE - len(live) - delayed[0]
        for i in range(min(count, max(0, room))):
            # Stagger releases so simultaneous approvals don't fire as one clump.
            delay = random.randint(0, 320) + i * random.randint(40, 120)
            delayed[0] += 1
            root.after(delay, release)

    def tick() -> None:
        stop = False
        try:
            while True:
                item = pending.get_nowait()
                if item is None:
                    stop = True
                else:
                    spawn(item)
        except queue.Empty:
            pass

        live[:] = [p for p in live if p.step()]

        if stop and not live and not delayed[0]:
            root.quit()
            return
        root.after(FRAME_MS, tick)

    root.after(FRAME_MS, tick)
    try:
        root.mainloop()
    except Exception:
        pass


class PuffClient:
    """Parent-side handle. Spawns the overlay process on demand and feeds it."""

    def __init__(self, log=None) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._log = log or (lambda _m: None)

    def _interpreter(self) -> str:
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        return str(pyw if pyw.exists() else exe)

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _spawn(self) -> bool:
        try:
            self._proc = subprocess.Popen(
                [self._interpreter(), os.path.abspath(__file__), "--serve"],
                stdin=subprocess.PIPE, creationflags=CREATE_NO_WINDOW,
            )
            self._log(f"  puff overlay process started pid={self._proc.pid}")
            return True
        except Exception as exc:
            self._log(f"  puff overlay spawn failed: {exc!r}")
            self._proc = None
            return False

    def emit(self, count: int = 1) -> None:
        with self._lock:
            if not self._alive() and not self._spawn():
                return
            try:
                self._proc.stdin.write(f"{int(count)}\n".encode())
                self._proc.stdin.flush()
            except Exception:
                # Overlay died (logged off, killed); try once more with a fresh one.
                self._proc = None
                if self._spawn():
                    try:
                        self._proc.stdin.write(f"{int(count)}\n".encode())
                        self._proc.stdin.flush()
                    except Exception as exc:
                        self._log(f"  puff emit failed: {exc!r}")

    def shutdown(self) -> None:
        with self._lock:
            if self._alive():
                try:
                    self._proc.stdin.write(b"quit\n")
                    self._proc.stdin.flush()
                    self._proc.stdin.close()
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    else:
        # Manual check: a few bursts through the real client path.
        import time
        client = PuffClient(log=print)
        for n in (1, 3, 6):
            client.emit(n)
            time.sleep(1.8)
        time.sleep(2.5)
        client.shutdown()
