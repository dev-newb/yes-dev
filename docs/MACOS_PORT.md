> **Temporary working document — delete this file once the macOS port is underway.**
> It exists to hand context to whoever picks the port up. It is not user
> documentation and should not outlive its purpose. When the next model (or
> person) has read it and started work, remove it: `git rm docs/MACOS_PORT.md`.

# Porting Yes, Dev to macOS

## The problem is the same there

Chrome's consent prompt is a browser-level security feature in Chrome 144+, not
a Windows one. It fires on every connection attempt to the remote debugging
endpoint on every desktop platform, and the approval does not persist.

This is not a gap waiting to be closed upstream. The request to persist
approval, [#825](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/825),
was **closed as `not_planned`** on 2026-03-19.
[#1794](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/1794)
(prompts stacking when several clients connect at once) is still open. Clicking
the button remains the only route.

## What you are inheriting

Three processes, and the seams between them are why this port is tractable:

```
yes_dev.pyw  (tray, pystray)          owns config, the menu, the burst guard
    |
    +-- spawns --> watcher.ps1        finds and clicks the dialog
    |              writes yes-dev.log   <-- tray tails this for "[ACTION]" lines
    |
    +-- spawns --> puffs.py --serve   the clouds
    |              stdin: one integer per line = "show N clouds"
    |
    +-- spawns --> burst_dialog.py    the 5s prompt; prints its answer on stdout
```

Two of those seams are plain text, which means **a macOS engine and a macOS
overlay can be dropped in without touching the tray at all**, provided they
honour the same contracts:

- The engine appends a line containing `[ACTION]` to the log for each approval.
  That is the entire interface the tray depends on. Match the existing format
  (`2026-08-27 16:11:28.644 [ACTION]   APPROVED via ...`) and the counter, the
  clouds and the burst guard all work unchanged.
- The overlay reads integers on stdin, one per line, and shows that many clouds.
  `quit` or EOF means exit.

## Component by component

| Component | On macOS | Effort |
|---|---|---|
| `watcher.ps1` (UI Automation) | Rewrite against the Accessibility API | **The real work** |
| `puffs.py` artwork (`_render_cloud`) | Reuse as-is, pure Pillow | None |
| `puffs.py` compositing (`UpdateLayeredWindow`) | Rewrite as a transparent `NSWindow` | Moderate, and easier than Windows |
| `yes_dev.pyw` tray, menu, burst logic | Mostly reuse; pystray has a darwin backend | Small |
| `burst_dialog.py` | Reuse; Tk is cross-platform. Restyle if you like | Trivial |
| Autostart (Startup shortcut) | LaunchAgent plist, or `SMAppService` | Small |
| Paths (`%LOCALAPPDATA%`) | `~/Library/Application Support/YesDev` | Trivial |
| Process control (`taskkill /T /F`) | `terminate()` / signals | Trivial |
| Single instance (named mutex) | Lock file with `fcntl.flock` | Trivial |
| **Accessibility permission** | New problem with no Windows equivalent | **See below** |

## Step 0 — run the probe before writing anything

`docs/mac/ax_probe.py`. The entire engine design depends on how the dialog
appears in the accessibility tree, and guessing is how you lose an afternoon.

On Windows the equivalent dump was the most valuable thing done on day one: the
dialog turned out **not** to be a top-level window at all, but a Views bubble
parented two levels inside the browser frame. Any watcher that enumerates
top-level windows silently finds nothing. Expect a comparable surprise on
macOS — most likely an `AXSheet` attached to the browser window, possibly a
separate `AXWindow`.

What you need out of the probe:

1. Is the dialog an `AXSheet`, an `AXWindow`, or a child of something else?
2. The exact `AXTitle` of the dialog and of the approve button.
3. Whether the button exposes `AXPress`.
4. Something stable to dedupe on. Windows had `GetRuntimeId()`; macOS AX has no
   direct equivalent, so plan to key on `(window, title, position)`, or
   `AXIdentifier` if Chrome sets one.

## Step 1 — the engine

`pyobjc` is enough; a Swift helper is optional.

```python
from ApplicationServices import (
    AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
    AXUIElementPerformAction, kAXErrorSuccess)

app = AXUIElementCreateApplication(chrome_pid)           # per Chrome process
err, wins = AXUIElementCopyAttributeValue(app, "AXWindows", None)
# ... walk AXSheets and AXChildren looking for the dialog title ...
AXUIElementPerformAction(button, "AXPress")              # the click
```

Rules that carry over from the Windows engine, all learned the hard way:

- **Anchor the button match** to `^(allow|approve)$`. Web pages have their own
  Allow buttons (ad blockers, permission chips) and a loose match will click
  them. It also keeps you off "Turn off in settings", which disables the whole
  feature.
- **Scope the search to the dialog subtree**, never the whole app tree.
- **Skip hidden or offscreen elements.** A dismissed dialog lingers in the tree
  briefly and will otherwise be approved a second time.
- **Dedupe with a short window** (2s worked) so one dialog is not clicked twice
  mid-teardown, but **drop the dedupe entry when a click fails**, so a failed
  press is retried on the next sweep instead of sitting out the window.
- Poll at 250ms. Comfortably fast enough, invisible on CPU.
- Enumerate only Chrome's processes, never the whole system tree.

Keep writing the same log lines and the tray needs no changes at all.

## Step 2 — the tray

pystray has a darwin backend (`NSStatusItem`), so the menu structure should
port. `rumps` is the fallback if pystray's macOS support disappoints; it is
better maintained for status-bar apps specifically.

The main-thread rule is the same as on Windows: the GUI toolkit owns the main
thread, so the supervisor loop stays on a worker thread, and anything else that
needs a main thread (the overlay, the dialog) stays in its own process.

## Step 3 — the clouds

**Reuse the artwork generator unchanged.** `puffs._render_cloud(width,
premultiply=False)` returns a straight-alpha RGBA image, which is what `NSImage`
wants. The premultiplied path exists only because `UpdateLayeredWindow` demands
it; macOS does not.

The window is where the rewrite happens, and it is **easier than Windows**:

```python
win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
win.setOpaque_(False)
win.setBackgroundColor_(NSColor.clearColor())
win.setIgnoresMouseEvents_(True)          # click-through
win.setLevel_(NSStatusWindowLevel)        # above normal windows
win.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces |
                           NSWindowCollectionBehaviorStationary)
win.setAlphaValue_(alpha)                 # the fade
win.setFrameOrigin_(NSMakePoint(x, y))    # the drift
```

No colour key, no premultiplication, no layered-window ordering trap. Per-pixel
alpha is native.

Three things to get right:

- **Coordinates are flipped.** The macOS origin is bottom-left, so a rising
  cloud means `y` *increasing*. On Windows it decreases. This will bite once.
- **Use `NSScreen.visibleFrame`**, not `frame`, to find the area inside the Dock
  and menu bar. It is the counterpart of `SPI_GETWORKAREA`.
- **Keep the motion time-based**, reading the ranges from `puffs.py`
  (`SIZE_RANGE`, `ALPHA_RANGE`, `LIFE_RANGE`, `RISE_RANGE`, `DRIFT_RANGE`,
  `SPAWN_X_BACK`, `SPAWN_Y_UP`). Those are module constants precisely so the
  documentation art and any second implementation share one source of truth.

Keep the stdin protocol and the tray does not change.

## Step 4 — autostart

`~/Library/LaunchAgents/com.dev-newb.yesdev.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.dev-newb.yesdev</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>/path/to/yes_dev.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict></plist>
```

Load it with `launchctl load -w`. If you end up packaging an `.app` (below),
prefer `SMAppService.loginItemServiceWithIdentifier` instead: it survives
updates and appears properly under System Settings > General > Login Items.

## The permission problem — read before you start

macOS will not let any process drive another app's UI without an explicit
**Accessibility** grant in System Settings > Privacy & Security > Accessibility.
This is TCC, it is deliberate, and it cannot be scripted around. AppleScript and
System Events need exactly the same grant, so there is no side door.

The consequence that shapes the project: **the grant attaches to a binary.** Run
the tool as a loose script and the permission lands on `python3` or on Terminal,
which is both fragile (it breaks when the interpreter path changes) and far too
broad (everything that interpreter ever runs inherits it). Doing it properly
means shipping a signed `.app` bundle — py2app or PyInstaller, then `codesign` —
so the grant attaches to Yes, Dev itself.

That packaging step is the main reason this is a bigger job on macOS than the
code diff suggests. Budget for it.

Useful while developing: `tccutil reset Accessibility <bundle-id>` clears the
grant so the first-run experience can be tested. `AXIsProcessTrusted()` reports
whether you currently hold it — check at startup and say so plainly in the log,
because without it every attribute read silently returns empty and the app looks
broken rather than unpermitted.

## Lessons from the Windows build worth carrying over

These cost real time here, and most are not Windows-specific:

- **GUI apps fail silently.** Under `pythonw` there is no console, so failures
  vanished entirely until a tray-side log was added. Log first, then build.
- **Never let one frame kill the animation loop.** An exception between
  scheduled callbacks stopped the reschedule and froze every cloud on screen
  permanently, in the user's face. Catch per frame, destroy what is live, and
  keep the loop running.
- **Construction must clean up after itself.** A failure partway through
  building a cloud abandoned an already-visible window, which sat there as a
  bare rectangle with nothing animating it.
- **Declare argtypes for anything taking a handle.** With only a restype set,
  ctypes passes a Python int as a C int and a 64-bit handle overflows. This is
  the single bug that produced those white rectangles.
- **Set safety thresholds from measurement, not intuition.** The first burst
  limit (15/min) landed exactly on the user's real peak and kept switching the
  app off mid-work. Measure real load first; it is now 60/min.
- **A guard that waits for a human recreates the problem the tool exists to
  solve.** Auto-resume, or ask with a deadline and a safe default.
- **Never test against the live config.** Setting the burst limit to 3 for a
  test and leaving it there took the app out of service, with no obvious cause.

## Suggested staging

Each phase has a check that means "done":

1. **Probe.** `ax_probe.py` prints the dialog and its buttons. → You know the shape.
2. **Engine, headless.** A loop that clicks the prompt, logging in the existing
   format. → Attach four CDP clients in parallel; all connect with no human.
3. **Tray.** pystray menu, engine supervision, config. → On/Off starts and stops
   the engine; the approval counter moves.
4. **Clouds.** NSWindow overlay behind the existing stdin protocol. → Clouds
   appear near the status bar, click through, and leave nothing behind.
5. **Autostart.** → Survives a real logout/login. Verify this properly; on
   Windows it was the last thing proven, and the only proof was a real reboot.
6. **Packaging.** Signed `.app` with the Accessibility grant on the bundle. →
   Works from a fresh user account.

## What is unverified

**All of the macOS material in this file, and `docs/mac/ax_probe.py`, was written
without a Mac to test on.** The Windows side is measured; the macOS side is
informed design, not tested code. Treat the AX attribute names, the NSWindow
flags and the probe script as a strong starting point that will need a first run
to shake out — and do the probe before trusting the rest of it.
