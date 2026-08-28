# Handoff — macOS port of Yes, Dev (for a future Claude Code session)

> Written by a Cowork (work-mode) session on 2026-08-28 that could **not** push
> to the repo (Cowork can't attach GitHub repos yet) and had **no Mac to test
> on**. It got partway. This is where it stopped and what's left. A Claude Code
> session on the actual mac mini — with repo access and native macOS — is the
> right place to finish.

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
