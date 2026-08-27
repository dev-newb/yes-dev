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
import time
import tkinter as tk

from PIL import Image, ImageChops, ImageDraw, ImageFilter
from pathlib import Path

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
SPI_GETWORKAREA = 0x0030
CREATE_NO_WINDOW = 0x08000000

# Colour-keying can only make one exact colour transparent, which forces hard
# aliased edges. These clouds are drawn into a 32-bit bitmap and pushed with
# UpdateLayeredWindow instead, so every pixel carries its own alpha and the
# edges can be soft. Tk only supplies the window; it never paints it.
CLOUD_TOP = (247, 250, 255)      # lit top
CLOUD_BOTTOM = (176, 193, 214)   # shaded underside
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
ULW_ALPHA = 0x02
DIB_RGB_COLORS = 0

FRAME_MS = 16          # ~60fps while animating
IDLE_MS = 120          # cheap heartbeat while waiting for the next approval
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


class _BlendFunction(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Size(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", ctypes.c_uint32 * 3)]


def _bind_gdi():
    """Handles are pointer-sized; the default c_int restype truncates them on 64-bit."""
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    for fn in (u.GetDC, u.GetParent):
        fn.restype = ctypes.c_void_p
    for fn in (g.CreateCompatibleDC, g.CreateDIBSection, g.SelectObject):
        fn.restype = ctypes.c_void_p
    g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    # Without argtypes ctypes passes a Python int as a C int, and a 64-bit HDC
    # overflows it: "argument 1: OverflowError: int too long to convert".
    g.CreateDIBSection.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint32]
    g.DeleteObject.argtypes = [ctypes.c_void_p]
    g.DeleteDC.argtypes = [ctypes.c_void_p]
    u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.UpdateLayeredWindow.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_Point), ctypes.POINTER(_Size),
        ctypes.c_void_p, ctypes.POINTER(_Point), ctypes.c_uint32,
        ctypes.POINTER(_BlendFunction), ctypes.c_uint32]
    return u, g


_USER32, _GDI32 = _bind_gdi()


def _render_cloud(width: int) -> "Image.Image":
    """A soft, shaded little cloud as premultiplied BGRA.

    Lobes are jittered per cloud so no two are identical, blurred for soft edges,
    then put through a contrast curve so the silhouette still reads as a shape
    rather than a smudge. A vertical gradient does the shading: lit on top,
    cooler underneath.
    """
    w = max(24, int(width))
    h = int(w * 0.78)
    ss = 3                                   # supersample, then downscale to anti-alias
    W, H = w * ss, h * ss

    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    # Circles in pixel space, kept inside ~15% margins so the blur has room and
    # nothing gets cut off flat against the edge of the bitmap.
    lobes = [(0.28, 0.60, 0.20), (0.50, 0.52, 0.26), (0.72, 0.60, 0.19),
             (0.40, 0.43, 0.17), (0.62, 0.46, 0.15)]
    for cx, cy, r in lobes:
        cx += random.uniform(-0.025, 0.025)
        cy += random.uniform(-0.025, 0.025)
        rr = r * random.uniform(0.9, 1.1) * W
        d.ellipse([cx * W - rr, cy * H - rr, cx * W + rr, cy * H + rr], fill=255)
    # A flat base keeps it from looking like a bunch of loose bubbles.
    d.rectangle([0.24 * W, 0.58 * H, 0.78 * W, 0.84 * H], fill=255)

    mask = mask.filter(ImageFilter.GaussianBlur(radius=W * 0.030))
    mask = mask.point(lambda v: 0 if v < 55 else min(255, int((v - 55) * 2.0)))
    mask = mask.resize((w, h), Image.LANCZOS)

    grad = Image.new("RGB", (1, h))
    for y in range(h):
        f = y / max(1, h - 1)
        grad.putpixel((0, y), tuple(
            int(CLOUD_TOP[i] + (CLOUD_BOTTOM[i] - CLOUD_TOP[i]) * f) for i in range(3)))
    rgb = grad.resize((w, h))

    r, g, b = rgb.split()
    # UpdateLayeredWindow wants premultiplied alpha; ImageChops.multiply is exactly that.
    r, g, b = (ImageChops.multiply(c, mask) for c in (r, g, b))
    return Image.merge("RGBA", (r, g, b, mask))


class _Puff:
    """One cloud: rises, drifts sideways a little, fades out, then destroys itself.

    Motion is a function of elapsed time, not of frame count, so a dropped frame
    shortens nothing and the drift speed looks the same whatever rate we achieve.
    """

    def __init__(self, root: tk.Tk) -> None:
        area = _work_area()
        # These are the original hand-tuned values, converted from per-frame to
        # per-second at the ~33fps the loop actually used to run at. Keep them in
        # seconds and px/sec: a faster, shorter-lived cloud is easy to miss over a
        # remote desktop, where only a fraction of frames ever reach the viewer.
        self.size = random.randint(26, 46)
        self.alpha0 = random.uniform(0.55, 0.80)
        self.life = random.uniform(2.9, 4.5)               # seconds
        self.rise = random.uniform(26.0, 56.0)             # px/sec
        self.drift = random.uniform(-12.0, 12.0)           # px/sec
        self.born = time.perf_counter()

        # Anchor near the tray, with enough spread that a burst never stacks.
        self.x0 = float(area.right - random.randint(60, 210))
        self.y0 = float(area.bottom - random.randint(45, 120))
        self.x, self.y = self.x0, self.y0
        self.alpha = self.alpha0
        self._shown = (int(self.x0), int(self.y0))
        self._alpha_set = self.alpha0

        art = _render_cloud(self.size)
        self.w, self.h = art.size
        self._screen_dc = self._mem_dc = self._bitmap = self._old_bitmap = None

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{self.w}x{self.h}+{int(self.x0)}+{int(self.y0)}")
        # Realize before touching the ex-style: changing it on a window that has
        # not been created yet drops the layered flag and it never appears.
        self.win.update_idletasks()
        _make_click_through(self.win)

        try:
            self._attach_bitmap(art)
        except Exception:
            # Never leave a bare Tk window mapped: it shows as a white rectangle
            # and, with nothing animating it, sits there until the process dies.
            self.destroy()
            raise

    def _attach_bitmap(self, art) -> None:
        # Hand the pixels straight to the compositor. Tk never draws this window.
        self._screen_dc = _USER32.GetDC(None)
        self._mem_dc = _GDI32.CreateCompatibleDC(self._screen_dc)
        bi = _BitmapInfo()
        bi.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        bi.bmiHeader.biWidth = self.w
        bi.bmiHeader.biHeight = -self.h          # negative = top-down
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bits = ctypes.c_void_p()
        self._bitmap = _GDI32.CreateDIBSection(
            self._mem_dc, ctypes.byref(bi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        raw = art.tobytes("raw", "BGRA")
        ctypes.memmove(bits, raw, len(raw))
        self._old_bitmap = _GDI32.SelectObject(self._mem_dc, self._bitmap)
        self._push(self.x0, self.y0, self.alpha0)

    def step(self, now: float) -> bool:
        """Advance to wall-clock `now`. Returns False once the puff is gone."""
        t = now - self.born
        if t >= self.life:
            self.destroy()
            return False

        frac = t / self.life
        self.alpha = self.alpha0 * (1.0 - frac)            # linear, as originally tuned
        self.x = self.x0 + self.drift * t
        self.y = self.y0 - self.rise * t

        # One call moves and fades together, so a frame costs a single round trip.
        pos = (int(self.x), int(self.y))
        if pos != self._shown or abs(self.alpha - self._alpha_set) >= 0.004:
            if not self._push(self.x, self.y, self.alpha):
                return False
        return True

    def _push(self, x: float, y: float, alpha: float) -> bool:
        blend = _BlendFunction(AC_SRC_OVER, 0, max(0, min(255, int(alpha * 255))),
                               AC_SRC_ALPHA)
        dst = _Point(int(x), int(y))
        src = _Point(0, 0)
        size = _Size(self.w, self.h)
        try:
            hwnd = _USER32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            ok = _USER32.UpdateLayeredWindow(
                hwnd, self._screen_dc, ctypes.byref(dst), ctypes.byref(size),
                self._mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        except (tk.TclError, OSError):
            return False
        self._shown = (int(x), int(y))
        self._alpha_set = alpha
        return bool(ok)

    def destroy(self) -> None:
        # GDI objects are not garbage collected; leaking one per cloud would bleed
        # handles for as long as the overlay process lives.
        for release in (
            lambda: self._old_bitmap and _GDI32.SelectObject(self._mem_dc, self._old_bitmap),
            lambda: self._bitmap and _GDI32.DeleteObject(self._bitmap),
            lambda: self._mem_dc and _GDI32.DeleteDC(self._mem_dc),
            lambda: self._screen_dc and _USER32.ReleaseDC(None, self._screen_dc),
            self.win.destroy,
        ):
            try:
                release()
            except Exception:
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
    # Windows' default timer granularity is ~15.6ms, so after(16) actually lands
    # around 31ms and the animation runs at half the intended rate. Ask for 1ms
    # while clouds are on screen - but only then, since a raised timer resolution
    # costs power and this process outlives every animation by a long way.
    hires = [False]

    def set_hires(on: bool) -> None:
        if on == hires[0]:
            return
        try:
            if on:
                ctypes.windll.winmm.timeBeginPeriod(1)
            else:
                ctypes.windll.winmm.timeEndPeriod(1)
            hires[0] = on
        except Exception:
            pass

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

    timing = {"last": None, "gaps": [], "work": []}

    def tick() -> None:
        t0 = time.perf_counter()
        if timing["last"] is not None and live:
            timing["gaps"].append((t0 - timing["last"]) * 1000)
        timing["last"] = t0

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

        n = len(live)
        try:
            live[:] = [p for p in live if p.step(t0)]
        except Exception:
            # A frame that throws must not kill the after() chain - that freezes
            # every live cloud on screen permanently.
            import traceback
            _debug(f"frame failed: {traceback.format_exc()}")
            for stuck in live:
                stuck.destroy()
            live.clear()
        work_ms = (time.perf_counter() - t0) * 1000
        if n:
            timing["work"].append(work_ms)

        if stop and not live and not delayed[0]:
            if timing["gaps"]:
                gaps = sorted(timing["gaps"])
                work = sorted(timing["work"])
                _debug(f"frames={len(gaps)} gap_ms median={gaps[len(gaps) // 2]:.1f} "
                       f"p95={gaps[int(len(gaps) * 0.95)]:.1f} max={gaps[-1]:.1f} | "
                       f"work_ms median={work[len(work) // 2]:.2f} max={work[-1]:.2f}")
            root.quit()
            return

        # Animate at full rate only while there is something to animate; otherwise
        # idle cheaply until the next approval arrives.
        busy = bool(live) or delayed[0] > 0
        set_hires(busy)
        # after() waits N ms *after* this callback returns, so subtract the work to
        # keep a steady cadence rather than drifting to work+N.
        root.after(max(1, round(FRAME_MS - work_ms)) if busy else IDLE_MS, tick)

    root.after(FRAME_MS, tick)
    try:
        root.mainloop()
    except Exception:
        pass
    finally:
        set_hires(False)


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
