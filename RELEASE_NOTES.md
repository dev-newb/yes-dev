# Yes, Dev 1.1.0

Two headline changes: **macOS support**, and a **critical memory fix for
Windows**. If you run the Windows build, this update is not optional.

## Windows: the engine leaked, and could outlive its tray

The 1.0.0 engine walked the UI Automation tree on every sweep - four times a
second - to look for a dialog that is almost never there. UI Automation elements
are COM objects behind managed wrappers, and their memory is native: it exerts
no pressure on the managed heap, so .NET never feels any need to collect, and
nothing is released. The classic huge-private-bytes, tiny-managed-heap shape.

Reproduced at **~9 MB/min - about 13 GB/day - with no dialogs occurring at all**,
so the leak was entirely in the idle path. Found in the field at **51.5 GB**
private, 20.4 GB working set, after ten days, with Windows compressing 11.8 GB
of memory to cope.

The fix came from re-testing an assumption. The dialog is nested inside the
browser frame *in the UI Automation tree*, which is what the original engine was
built around - but as a **Win32 window it is top-level**
(`class=Chrome_WidgetWin_1`, `title='Allow remote debugging?'`). So finding it
needs no COM at all. The sweep is now one `EnumWindows` pass filtered to that
class, and UI Automation is touched only once a dialog actually exists, to read
the buttons and press Allow.

Measured over the same 2.5 minutes:

| | start | end |
|---|---|---|
| 1.0.0 | 97.9 MB | 123.3 MB, still climbing |
| 1.1.0 | 80.2 MB | 81.1 MB, flat across six samples |

After two hours of live running: 81.9 MB, up 0.3 MB in the last hour. Handle
count is flat too, and approval latency is unchanged at 2.3s.

Belt and braces, since the remaining UI Automation use is not zero: a garbage
collection after any sweep that touched it, `$Error` cleared periodically
because an ErrorRecord retains whatever threw, and a 400 MB ceiling that exits
so the tray starts a clean engine rather than growing without bound.

**The orphan.** That 51.5 GB engine had been running for five days with no tray
behind it. Both mitigations - the burst guard and the disarm timer - live in the
tray, so an orphaned engine is not merely a leak: it is approving prompts with
nothing watching the rate. The engine now takes `-ParentPid` and exits when the
tray goes, matching `--exit-with-parent` on macOS. Verified: killing the tray
alone stops the engine within 1.5 seconds.

If you are updating from 1.0.0, `git pull` and restart the tray. Nothing in your
config changes.

## macOS support

The full port: an Accessibility-API engine, an `NSWindow` cloud overlay, a
status-bar tray, and a LaunchAgent for start-at-login. Verified on real hardware
(macOS 26, Chrome 152). The prompt is a browser-level feature, so it behaves the
same there.

The macOS build is newer and has far less mileage than the Windows one; the
README's Known limitations section says so plainly, including that the
Accessibility grant attaches to your Python binary until a signed `.app` bundle
exists.

## Also

- The Windows status icon is the app's own cloud with a white check, from the
  same seed as the macOS one, so both platforms wear one silhouette and only the
  colour changes with state. A plain circle read as an anonymous dot among the
  notification-area icons.
- One `requirements.txt` for both platforms, with markers.
- A logo, and documentation art generated from the app's own renderer.

---

# Yes, Dev 1.0.0

First release.

Chrome 144+ asks for consent every time a client attaches to its remote
debugging endpoint. Drive Chrome with several agents and those prompts stack up,
each one blocking its client until a human clicks Allow. There is no flag or
policy to turn this off, and the request to persist approval was closed as not
planned, so the only route is to click it. `Yes, Dev` sits in the tray and
clicks it for you.

Four parallel attaches go from ~35 seconds of waiting on a human to 2.4-4.4
seconds, unattended.

## What's in it

- **Clicks the consent dialog** through UI Automation - no mouse movement, no
  focus stealing, works on background windows while you carry on typing.
- **Floating puff notices.** A toast card per approval is worse than the problem
  when approvals fire dozens of times an hour, so each one instead releases a
  small translucent cloud that drifts up from the tray and fades. Toast and
  silent modes are there if you prefer them.
- **Burst guard.** Reacts if approvals spike past 60/min (adjustable to
  30/120/off). By default it asks, with a five-second visible countdown: stop,
  or allow for one hour. Running the timer out stops it, since that is the safe
  answer to a burst you did not expect. It can also be set to stop silently and
  re-arm a minute later.
- **Auto-disarm timer.** Stay armed for 15 minutes, an hour, four hours, or
  until you turn it off - auto-approval is only a risk while it is on.
- **Start at login** via a Startup shortcut. No scheduled task, no admin rights.
- **Observe mode** logs the dialogs without clicking, for a first look.
- Optional Microsoft Edge support, adjustable poll rate, and an approval counter
  in the tray tooltip.

## Field data

From 8 days on the machine it was built on: 454 approvals, one failed click
(99.8%), no burst pauses at the default limit, and it restarted itself correctly
after a reboot. Twenty-one transient UI Automation errors were logged and
recovered from on the next 250ms sweep, costing nothing observable.

## Requirements

Windows, Chrome 144+, Python 3.9+ with `pystray` and `pillow`.

## Known limitations

English-language Chrome only (the dialog and button are matched by their text,
though both are overridable on `watcher.ps1`); the clouds are positioned for a
single unscaled display; Windows only by construction.
