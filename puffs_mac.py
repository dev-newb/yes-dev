"""Floating puff notifications for macOS - the NSWindow counterpart of puffs.py.

Each approval releases a small translucent cloud near the status item, which
drifts up and fades out. Everything is jittered so a burst scatters instead of
stacking into one blob.

The artwork and the shape of a cloud's life are imported from puffs.py rather
than restated here, so the Windows overlay, the documentation art and this one
can never quietly drift apart. Only the window system differs:

    Windows                          macOS
    ------------------------------   ------------------------------------------
    Tk toplevel + UpdateLayeredWindow  borderless transparent NSWindow
    premultiplied BGRA DIB             straight-alpha NSImage (native per-pixel)
    WS_EX_TRANSPARENT/NOACTIVATE       setIgnoresMouseEvents_ / orderFrontRegardless
    y decreases going up               y *increases* going up (origin bottom-left)
    SPI_GETWORKAREA                    NSScreen.visibleFrame
    released at the tray, rises away   rises *toward* the status item (see _Puff)

That last row is the one real behavioural difference, and it is forced: the tray
is at the bottom of the screen and the status item is at the top, so a cloud
released level with the item would leave the screen at once. See _Puff.__init__.

Same contract as `puffs.py --serve`, so the tray does not care which one it is
talking to: one integer per line on stdin means "show that many clouds";
`quit` or EOF means exit.
"""
from __future__ import annotations

import io
import os
import queue
import random
import sys
import threading
import time
from pathlib import Path

# The artwork generator and every motion range come from the Windows module.
# It imports cleanly off-Windows: its Tk import is guarded and the GDI binding
# only happens on win32.
try:
    from puffs import (
        ALPHA_RANGE, DRIFT_RANGE, FRAME_MS, IDLE_MS, LIFE_RANGE, MAX_LIVE,
        RISE_RANGE, SIZE_RANGE, SPAWN_X_BACK, SPAWN_Y_UP, _render_cloud,
    )
except ImportError:  # allow running from another cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from puffs import (
        ALPHA_RANGE, DRIFT_RANGE, FRAME_MS, IDLE_MS, LIFE_RANGE, MAX_LIVE,
        RISE_RANGE, SIZE_RANGE, SPAWN_X_BACK, SPAWN_Y_UP, _render_cloud,
    )

try:
    from platform_mac import DATA_DIR, ensure_data_dir
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from platform_mac import DATA_DIR, ensure_data_dir

try:
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyProhibited,
        NSBackingStoreBuffered,
        NSColor,
        NSImage,
        NSImageView,
        NSScreen,
        NSStatusWindowLevel,
        NSWindow,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorIgnoresCycle,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
    )
    from Foundation import (
        NSData, NSDate, NSDefaultRunLoopMode, NSMakePoint, NSMakeRect, NSMakeSize,
        NSRunLoop,
    )
except ImportError:
    sys.exit(
        "Yes, Dev macOS overlay needs pyobjc:\n"
        "    pip3 install pyobjc-framework-Cocoa"
    )

# Above normal windows, on every Space, and never in the window cycle. Stationary
# keeps a cloud from sliding when the user switches Spaces mid-flight.
_COLLECTION = (NSWindowCollectionBehaviorCanJoinAllSpaces
               | NSWindowCollectionBehaviorStationary
               | NSWindowCollectionBehaviorIgnoresCycle)


def _debug(message: str) -> None:
    """Overlay-side diagnostics; only written when YESDEV_DEBUG is set.

    puffs.py has its own version keyed to %LOCALAPPDATA%; this one writes beside
    the other logs in the macOS data directory.
    """
    if not os.environ.get("YESDEV_DEBUG"):
        return
    try:
        ensure_data_dir()
        with (DATA_DIR / "puffs.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{message}\n")
    except Exception:
        pass


def _status_screen():
    """The screen carrying the menu bar, which is where the status item lives.

    NSScreen.screens()[0] is that screen by definition; mainScreen() follows the
    key window and would wander to whichever display the user last clicked on.
    """
    screens = NSScreen.screens()
    return screens[0] if screens else NSScreen.mainScreen()


def _ns_image(pil_image, point_size) -> NSImage:
    """A PIL RGBA image as an NSImage, sized in points.

    Straight alpha, not premultiplied: NSImage wants it that way, which is why
    the Windows-only premultiplied path is skipped. Going through PNG bytes
    keeps this to one well-trodden API instead of hand-packing an
    NSBitmapImageRep's buffer.
    """
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    raw = buf.getvalue()
    image = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(raw, len(raw)))
    if image is None:
        raise RuntimeError("NSImage could not decode the rendered cloud")
    # The bitmap is rendered at the display's backing scale; declaring a smaller
    # point size is what makes it a crisp 2x asset rather than a soft 1x one.
    image.setSize_(NSMakeSize(*point_size))
    return image


class _Puff:
    """One cloud: rises, drifts sideways a little, fades out, then closes itself.

    Motion is a function of elapsed time, not of frame count, so a dropped frame
    shortens nothing and the drift looks the same whatever rate we achieve.
    """

    def __init__(self) -> None:
        screen = _status_screen()
        area = screen.visibleFrame()
        scale = screen.backingScaleFactor() or 1.0

        self.size = random.randint(*SIZE_RANGE)
        self.alpha0 = random.uniform(*ALPHA_RANGE)
        self.life = random.uniform(*LIFE_RANGE)            # seconds
        self.rise = random.uniform(*RISE_RANGE)            # px/sec
        self.drift = random.uniform(*DRIFT_RANGE)          # px/sec
        self.born = time.perf_counter()

        # Anchor near the status item, in from the right edge, exactly as the
        # Windows build anchors to the tray.
        self.x0 = float(area.origin.x + area.size.width - random.randint(*SPAWN_X_BACK))

        # The vertical anchor is the one thing that cannot be copied across. The
        # Windows tray sits at the *bottom*, so a cloud is released there and
        # rises away into open desktop. The macOS status item sits at the *top*,
        # where "rise away from it" means "leave the screen immediately": spawned
        # level with the item, a cloud crosses the menu bar within a second and
        # spends the rest of its life off-screen, still half opaque (measured).
        #
        # So the arc is anchored by its END instead: the cloud rises *toward* the
        # status item and evaporates just below it. SPAWN_Y_UP keeps its meaning
        # as the jittered gap from the menu bar, and working backwards by this
        # cloud's own travel puts the whole arc on screen whatever its speed and
        # lifetime. The window top ends below visibleFrame, so the menu bar is
        # never overlapped.
        top = area.origin.y + area.size.height
        self.y_end = float(top - random.randint(*SPAWN_Y_UP))
        self.y0 = self.y_end - self.rise * self.life

        art = _render_cloud(int(round(self.size * scale)), premultiply=False)
        self.w = art.width / scale
        self.h = art.height / scale

        self.win = None
        try:
            image = _ns_image(art, (self.w, self.h))
            self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(self.x0, self.y0, self.w, self.h),
                NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
            # Held by Python for as long as it is animating; letting Cocoa release
            # it on close would leave this object pointing at freed memory.
            self.win.setReleasedWhenClosed_(False)
            self.win.setOpaque_(False)
            self.win.setBackgroundColor_(NSColor.clearColor())
            self.win.setIgnoresMouseEvents_(True)          # click-through
            self.win.setLevel_(NSStatusWindowLevel)
            self.win.setCollectionBehavior_(_COLLECTION)
            self.win.setHasShadow_(False)                  # a shadow reads as a box
            self.win.setAlphaValue_(self.alpha0)

            view = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, self.w, self.h))
            view.setImage_(image)
            self.win.setContentView_(view)
            # Show without activating: the tray must never steal focus mid-typing.
            self.win.orderFrontRegardless()
        except Exception:
            # Never leave a half-built window on screen with nothing animating it.
            self.destroy()
            raise

        self._shown = (self.x0, self.y0)
        self._alpha_set = self.alpha0

    def step(self, now: float) -> bool:
        """Advance to wall-clock `now`. Returns False once the puff is gone."""
        t = now - self.born
        if t >= self.life:
            self.destroy()
            return False

        frac = t / self.life
        alpha = self.alpha0 * (1.0 - frac)                 # linear, as tuned
        x = self.x0 + self.drift * t
        y = self.y0 + self.rise * t     # macOS origin is bottom-left: up is +y

        try:
            if (int(x), int(y)) != (int(self._shown[0]), int(self._shown[1])):
                self.win.setFrameOrigin_(NSMakePoint(x, y))
                self._shown = (x, y)
            if abs(alpha - self._alpha_set) >= 0.004:
                self.win.setAlphaValue_(alpha)
                self._alpha_set = alpha
        except Exception:
            self.destroy()
            return False
        return True

    def destroy(self) -> None:
        win, self.win = self.win, None
        if win is None:
            return
        try:
            win.orderOut_(None)
            win.close()
        except Exception:
            pass


def serve() -> None:
    """Run the overlay, taking one integer per line on stdin.

    AppKit owns this thread. The run loop is pumped by hand rather than through
    NSApp.run() so the frame cadence can subtract its own work the way the
    Windows loop does, and so shutdown is a plain `break`.
    """
    app = NSApplication.sharedApplication()
    # No Dock icon, no menu bar, cannot be activated - this is an overlay, not an
    # app the user switches to.
    app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    app.finishLaunching()
    _debug("serve: NSApplication ready")

    pending: "queue.Queue[int | None]" = queue.Queue()
    live: list[_Puff] = []
    delayed: list[float] = []          # perf_counter deadlines for staggered releases

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
        pending.put(None)              # parent closed the pipe or asked us to stop

    threading.Thread(target=read_stdin, daemon=True).start()

    def spawn(count: int, now: float) -> None:
        room = MAX_LIVE - len(live) - len(delayed)
        for i in range(min(count, max(0, room))):
            # Stagger releases so simultaneous approvals don't fire as one clump.
            delay_ms = random.randint(0, 320) + i * random.randint(40, 120)
            delayed.append(now + delay_ms / 1000.0)

    stop = False
    while True:
        t0 = time.perf_counter()

        try:
            while True:
                item = pending.get_nowait()
                if item is None:
                    stop = True
                else:
                    spawn(item, t0)
        except queue.Empty:
            pass

        try:
            due = [d for d in delayed if d <= t0]
            if due:
                delayed[:] = [d for d in delayed if d > t0]
                for _ in due:
                    if len(live) >= MAX_LIVE:
                        break
                    try:
                        live.append(_Puff())
                        _debug(f"spawned puff, live={len(live)}")
                    except Exception:
                        import traceback
                        _debug(f"spawn failed: {traceback.format_exc()}")

            live[:] = [p for p in live if p.step(t0)]
        except Exception:
            # A frame that throws must not kill the loop - that would freeze every
            # live cloud on screen permanently, in the user's face.
            import traceback
            _debug(f"frame failed: {traceback.format_exc()}")
            for stuck in live:
                stuck.destroy()
            live.clear()

        if stop and not live and not delayed:
            break

        # Animate at full rate only while there is something to animate; otherwise
        # idle cheaply until the next approval arrives.
        busy = bool(live) or bool(delayed)
        work = time.perf_counter() - t0
        interval = max(0.001, FRAME_MS / 1000.0 - work) if busy else IDLE_MS / 1000.0
        # Returns early if an input source fires, which costs one cheap extra
        # frame and nothing else: the motion is time-based, not frame-counted.
        NSRunLoop.currentRunLoop().runMode_beforeDate_(
            NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(interval))

    for puff in live:
        puff.destroy()
    _debug("serve: exiting")


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    else:
        # Manual check: a few bursts through the real client path, exactly as the
        # tray will drive it.
        from puffs import PuffClient

        client = PuffClient(log=print, script=os.path.abspath(__file__))
        for n in (1, 3, 6):
            client.emit(n)
            time.sleep(1.8)
        time.sleep(2.5)
        client.shutdown()
