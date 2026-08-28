# Handoff — macOS port of Yes, Dev (for a future Claude Code session)

> Written by a Cowork (work-mode) session on 2026-08-28 that could **not** push
> to the repo (Cowork can't attach GitHub repos yet) and had **no Mac to test
> on**. It got partway. This is where it stopped and what's left. A Claude Code
> session on the actual mac mini — with repo access and native macOS — is the
> right place to finish.

## STEP 0 — DONE (2026-08-27, real mac mini, Chrome 152)

The probe was run against a live "Allow remote debugging?" prompt and the engine
was verified end-to-end. Findings, and what changed in `watcher_mac.py`:

- **Trigger:** `--remote-debugging-port` is *ignored* on the default profile
  (Chrome ≥136 requires a non-default `--user-data-dir`), and a throwaway profile
  never prompts. The prompt only fires on the **real** profile via
  chrome://inspect/#remote-debugging → "Remote Debugging" ON; each CDP attach to
  the DevToolsActivePort browser endpoint then raises it. Consent gates the
  WebSocket **handshake** itself.
- **Dialog shape:** an `AXSheet` titled `Allow remote debugging?` on the browser
  window, wrapping an `AXGroup`/`AXSubrole=AXApplicationAlertDialog`. Buttons:
  `Turn off in settings`, `Cancel`, `Allow`. `AXPress` on `Allow` works; the
  anchored `^(allow|approve)$` match hits only `Allow`. **Confirmed the design.**
- **Bug fixed — dedupe was inert.** `_dedupe_key` read `AXPosition` via a
  nonexistent `.pointValue()`, silently falling back to `obj:id(host)`, which
  changes every sweep → no cross-sweep dedupe at all. Now uses `AXValueGetValue`
  (position+size); verified a 2nd sweep 300 ms later re-presses nothing.
- **Bug fixed — false positives.** The title also matches the Window-menu
  `AXMenuItem` and an inner `AXHeading`. Added `_is_dialog_role()` so only real
  sheet/alert containers count. Torn-down button-less dialogs are now skipped
  silently (no WARN spam). One on-screen dialog → exactly one host.
- **Off-screen queued prompts** (parked at negative coords when several clients
  stack) are real and *do* get approved — this is wanted, it's how parallel
  attaches clear unattended. Only *dismissed/button-less* dialogs are skipped.
- **Verified:** continuous loop cleared a live stream of prompts, `[ACTION]` log
  lines in the exact tray format, written to `~/Library/Application Support/
  YesDev/yes-dev.log`. Zero WARN. `platform_mac.py` paths + `is_trusted` work.

**Still not run:** the 4-parallel-attach acceptance test in isolation (the box
had a live chrome-devtools-mcp server on 9222 generating its own prompts, which
confounds a clean count). Engine correctness itself is confirmed.

## `puffs_mac.py` — WRITTEN AND VERIFIED (2026-08-27)

The NSWindow clouds overlay is done and measured on the real machine. It imports
`_render_cloud` and every motion range from `puffs.py`, so there is still one
source of truth for the artwork and the timing.

- **Verified:** windows appear at `NSStatusWindowLevel` (CGWindowList layer 25),
  rise, fade linearly to ~0, and leave **nothing** behind (0 windows after the
  arcs finish, 0 after exit, exit code 0). Idle cost ~0.01 s CPU per 3 s. A burst
  of 100 peaked at 37 live windows against the `MAX_LIVE = 40` cap. The real
  `PuffClient(script=...)` spawn path works unchanged on macOS.
- **Retina:** the art is rendered at `backingScaleFactor` and the NSImage is then
  sized in points, so it is a crisp 2x asset rather than an upscaled 1x one.
- **The one real behavioural difference from Windows, and why.** The Windows tray
  is at the *bottom*, so a cloud is released there and rises away into open
  desktop. The macOS status item is at the *top*, so "away from it" is **down**:
  a cloud is released just under the menu bar and drifts downward. (Rising was
  tried first and measured — clouds crossed the menu bar within a second and
  spent the rest of their life off-screen, still ~40% opaque.) Two details follow
  from that, both settled by looking at it on the real display:
  - `SPAWN_Y_DOWN = (6, 54)` replaces `SPAWN_Y_UP` as the release gap. The
    Windows range is measured off a ~48 px taskbar; against a 30 pt menu bar the
    same numbers put a cloud up to four bar-heights away and it reads as
    appearing in mid-air rather than coming off the status item.
  - The silhouette is flipped (`_flip_hanging`): the cloud hangs from the bar
    rather than sitting on something, so the flat side goes up and the lobes
    hang below. Only the **alpha mask** is flipped — flipping the whole image
    takes the vertical shading with it and lights the cloud from below.
- **Not verified by eye:** taking a screenshot needs a Screen Recording (TCC)
  grant this session did not have, so the geometry/alpha/level/teardown were
  checked through `CGWindowListCopyWindowInfo` instead, and the artwork was
  inspected as a rendered PNG. Worth one human glance at a real burst.

## `yes_dev_mac.py` — WRITTEN AND VERIFIED (2026-08-27)

The status-bar tray is done and was run for real: it appears in the menu bar,
supervises the engine, tails the log, counts approvals and emits puffs. Verified
live through the Accessibility API (reading its own menu) rather than by eye.

- **Observed running:** menu showed `Status: on`, `Approved: 4` climbing from
  real Chrome prompts, `Accessibility: granted`, and every radio group with
  exactly one correct check mark. Engine and overlay ran as child processes.
  Pressing **Quit** from the menu tore down all three cleanly (exit 0).
- **21 logic checks pass** (`Config` round-trip, `[ACTION]` counting, non-ACTION
  lines ignored, burst→pause, auto-resume, no re-pause while paused, ask-mode
  dialog spawned exactly once, empty answer = stop, `allow_hour` snooze, log
  rotation, menu/radio state) — all against a temp config, never the live one.
- **Autostart** plist generation verified in a sandbox (valid plist, venv
  interpreter, correct `launchctl bootstrap`/`bootout`). Deliberately **not**
  installed for real — that is a standing change for the user to make from the
  menu. Surviving a real logout/login is still unproven, as on Windows.
- **One design change forced by AppKit.** The Windows build runs its monitor on
  a worker thread and blocks it for up to 30 s on the burst dialog, which is
  also what stops a second dialog being raised. AppKit menu updates must happen
  on the main thread, so the tick is a `rumps.Timer` and nothing in it may
  block: the burst dialog is now spawned and its answer collected on a later
  tick, with an explicit `_asking` guard against re-entry. Same semantics,
  responsive menu.
- **New menu item Windows never needed:** an Accessibility status line that
  prompts for the grant and deep-links to the settings pane.
- **Known limitation:** `notify_style: "toast"` degrades to log-only. Notification
  Center refuses notifications from an unbundled script; it will work once the
  app is packaged and signed. Puffs (the default) are unaffected.

### Orphaned engines — found in use, fixed

A tray killed rather than asked (SIGTERM from a terminal going away, logout, a
crash) took its `finally` with it and left `watcher_mac.py` running with
**PPID 1**. This is worse than an ordinary stray process: every safety limit —
the burst guard, the arm timer, pause/resume — lives in the *tray*. An engine
that outlives it keeps approving Chrome prompts with no rate guard, no timer and
no way to stop it short of finding the pid. Two fixes, belt and braces:

- **`watcher_mac.py --exit-with-parent`** (the tray always passes it): the engine
  records its parent pid at startup and exits as soon as it is reparented.
  Verified — SIGKILL the parent, engine is gone in ~0.5 s with a `[WARN]` line
  saying why. Works even where no cleanup is possible, which is the point.
- **SIGTERM/SIGINT/SIGHUP handlers in the tray**, so the common case shuts down
  tidily and immediately. Verified: signal logged, tray and engine both gone in
  ~0.5 s. The handler runs when the interpreter next executes Python, which the
  1 s `rumps.Timer` guarantees.

Running `watcher_mac.py` by hand is unaffected — the flag is opt-in.

To run it: `python3 yes_dev_mac.py` (use the venv that has pyobjc/rumps/pillow).

Next up: packaging — the signed `.app` so the Accessibility grant attaches to
Yes, Dev instead of to the Python binary.

## Read these two first
- `docs/MACOS_PORT.md` — the original design handoff. Still the source of truth
  for the AX approach, the NSWindow flags, the TCC/packaging problem, and the
  staging plan. **Not yet deleted** — its own header says to `git rm` it once
  the port is underway; leave that until step 0 (the probe) is actually done,
  since it's the only record of the untested design.
- `docs/mac/ax_probe.py` — **step 0, still not run.** The whole engine design
  hinges on where the "Allow remote debugging?" dialog sits in the AX tree
  (AXSheet on the window? separate AXWindow? nested deeper?). Nobody has seen
  real probe output yet. Run this before trusting the engine.

## What this session produced (all UNVERIFIED, none run on a Mac)

Branch: `macos` (local only — never pushed; Cowork couldn't authenticate to the
git proxy). Re-create the branch on the Mac from these files.

| File | State | Notes |
|---|---|---|
| `platform_mac.py` | **Draft, complete** | Paths (`~/Library/Application Support/YesDev`), `fcntl.flock` single-instance, `AXIsProcessTrusted`/`…WithOptions` permission check, LaunchAgent autostart (`launchctl bootstrap`/`bootout`). |
| `watcher_mac.py` | **Draft, complete but unverified** | The pyobjc engine. Writes the **exact** `[ACTION]` log line the tray parses. Has `--observe`, `--once`, `--interval-ms`, `--include-edge`. `find_dialog_hosts()` walks AXChildren/AXSheets/AXWindows — **this is the part the probe will correct.** |
| `puffs.py` | **Edited** | Made import-safe off-Windows: `tkinter` import guarded, `_bind_gdi()` only on win32, `PuffClient` takes a `script=` arg and drops `creationflags` off-Windows. `_render_cloud` + the motion-range constants are now reusable on macOS. Windows behaviour unchanged. |
| `burst_dialog.py` | **Reuse as-is** | Pure Tk, already cross-platform. Only caveat: Tk must be present in the Mac Python (python.org builds include it; some Homebrew ones don't). |
| `puffs_mac.py` | **NOT WRITTEN** | The NSWindow clouds overlay. See below. |
| `yes_dev_mac.py` | **NOT WRITTEN** | The tray. See below. |

## What's left to build

### 1. `puffs_mac.py` — the clouds overlay (moderate)
Same stdin protocol as `puffs.py --serve`: one integer per line = "show N
clouds"; `quit`/EOF exits. Reuse `puffs._render_cloud(width, premultiply=False)`
for the artwork (straight alpha — what `NSImage` wants; the premultiplied path
is Windows-only) and the `SIZE_RANGE`/`ALPHA_RANGE`/`LIFE_RANGE`/`RISE_RANGE`/
`DRIFT_RANGE`/`SPAWN_*` constants so the motion matches. The window recipe is in
`docs/MACOS_PORT.md` step 3 verbatim (borderless transparent `NSWindow`,
`setIgnoresMouseEvents_(True)`, `NSStatusWindowLevel`, join-all-spaces). Three
traps called out there: **coordinates are flipped** (rising = y *increasing*),
use `NSScreen.visibleFrame` not `frame`, keep motion time-based. Drive the fade
with an `NSTimer`/display link; guard every frame so one exception can't freeze
the loop (the Windows build's hardest-won lesson).

### 2. `yes_dev_mac.py` — the tray (small–medium)
The Windows `yes_dev.pyw` is the reference for the **logic** (config schema,
burst guard, auto-resume, arm timer, log tailing for `[ACTION]`) but its I/O is
all Win32 (`ctypes.windll`, `taskkill`, `.lnk`). Port the class, swapping:
- Menu → `rumps` (recommended over pystray for status-bar apps per the doc), or
  pystray's darwin backend.
- `start_engine()` → spawn `python3 watcher_mac.py` with the config flags
  (`--observe`, `--interval-ms`, `--include-edge`, `--log-path`), no
  `creationflags`.
- Overlay → `PuffClient(script="puffs_mac.py")` (already supported).
- `stop_engine()` → `proc.terminate()` / signals instead of `taskkill /T /F`.
- Single instance → `platform_mac.acquire_single_instance()`.
- Autostart → `platform_mac.enable_autostart()/disable_autostart()`.
- **New menu item the Windows build never needed:** an Accessibility-permission
  status line + a "Grant Accessibility…" action that calls
  `is_trusted(prompt=True)` and/or opens `accessibility_settings_url()`. Without
  the grant every AX read is empty and the app looks broken.
- The config file and `[ACTION]` log contract are identical — reuse `Config`
  almost verbatim (drop the BOM/`utf-8-sig` reading; that was for Notepad).

### 3. Packaging (the real reason this is bigger than the diff — do last)
Loose-script mode works for development, but the Accessibility grant then
attaches to the `python3` binary (fragile + far too broad). Ship a signed `.app`
(py2app or PyInstaller → `codesign`) so the grant lands on Yes, Dev itself, and
prefer `SMAppService` over the LaunchAgent plist for the login item. Budget for
this. `tccutil reset Accessibility com.dev-newb.yesdev` resets the grant for
first-run testing.

## Suggested order on the Mac
1. `pip3 install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices`.
2. **Run the probe** (`docs/mac/ax_probe.py`) with the real dialog up. Grant
   Terminal Accessibility first. This tells you if `find_dialog_hosts()` in
   `watcher_mac.py` is right; fix it against reality.
3. `python3 watcher_mac.py --observe` → confirm it *sees* the dialog and lists
   the buttons. Then drop `--observe` and confirm four parallel CDP attaches all
   clear with no human (the Windows acceptance test).
4. Build `puffs_mac.py`, then `yes_dev_mac.py`. Verify autostart survives a real
   logout/login (on Windows this was the last thing proven and only a real
   reboot proved it).
5. Package + sign. Test from a fresh user account.
6. `git rm docs/MACOS_PORT.md` and this file once the port is real, per the
   original doc's instruction.

## Contracts you must not break (why the tray is untouched by the engine swap)
- Engine appends a line containing `[ACTION]` to `yes-dev.log`, format:
  `2026-08-27 16:11:28.644 [ACTION]   APPROVED via AXPress`. That's the entire
  interface the tray depends on for its counter, clouds, and burst guard.
- Overlay reads integers on stdin, one per line; `quit`/EOF exits.
- Button match stays anchored `^(allow|approve)$`; dialog search stays scoped to
  the dialog subtree, never the whole app tree; skip offscreen elements; dedupe
  ~2s but drop the entry on a failed press.

## Delivery note
No files were pushed. This Cowork session had no repo access and no connected
folder. The four written/edited files (`platform_mac.py`, `watcher_mac.py`, the
`puffs.py` edits, this doc) were produced in a scratch clone. Bring them onto the
Mac by re-applying them to a fresh `git clone` on a `macos` branch, or ask this
session to hand them over as a patch/bundle.
