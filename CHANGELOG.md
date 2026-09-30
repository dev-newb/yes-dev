# Changelog

Newest first. Each entry says what changed and, where it matters, what was
measured - the numbers are from this repo's own runs, not estimates.

## Unreleased

### Windows

Fix Chrome and Edge process detection, add Simplified Chinese consent-dialog
support, and improve the native approval fallback, counters, and log handling.
See [#5](https://github.com/dev-newb/yes-dev/pull/5) for the changes and test results.

Thanks to [@Icather](https://github.com/Icather) (ChengLong Han) for identifying
these Windows issues, providing clear reproductions, and contributing the fixes in
[#2](https://github.com/dev-newb/yes-dev/pull/2) and
[#3](https://github.com/dev-newb/yes-dev/pull/3). This update builds on that work
with further fixes and testing. The original commits are preserved in its history.

### macOS

macOS on Chrome 153 and 154. The engine found the sheet, logged an approval, and
Chrome granted nothing. Two separate bugs, both invisible from the log as it was.

Thanks to [@crimsonsunset](https://github.com/crimsonsunset) for finding both,
proving the false approvals with a websocket held open across the approval rather
than trusting the sheet to vanish, and contributing the fix in
[#4](https://github.com/dev-newb/yes-dev/pull/4). That commit is preserved in the
history; the gates that follow came out of running it against Chrome 154.

#### macOS: the sheet is there, the title is not

Chrome 152 put "Allow remote debugging?" on the `AXSheet` itself. Chrome 153
leaves `AXTitle` and `AXDescription` empty and moves the string to an `AXHeading`
inside the sheet, so a title match found nothing while the prompt was on screen.
An untitled dialog whose heading matches now counts as the host. The heading walk
is capped at five levels and only runs for dialog-role children that are
themselves untitled, so the idle scan still costs what 1.2.0 measured.

#### macOS: AXPress is acknowledged and Allow never runs

`AXPress` returns `kAXErrorSuccess` on this button and the button does not fire -
Chrome's accessibility shim answers the action without dispatching it. Worse, the
re-press that 1.2.0 added to cover slow teardown *removes the sheet* on 153 while
leaving the debug socket unapproved. Both AX references go invalid, which is
exactly the signal used to mean "dismissed", so every one of those was logged
`APPROVED` with nothing granted. Verified by holding a CDP websocket open across
the approval: the log said approved, the socket never opened.

So the re-press is gone, and the fallback is a keystroke instead. `AXFocused` is
an attribute write rather than a command dispatch, and Chrome honours it, so
focus moves onto Allow; `Space` then goes to Chrome's pid via
`CGEventPostToPid`, which delivers keyboard events even though it silently drops
mouse events. Nine of nine approvals landed on Chrome 153.0.8010.48, each
verified by a websocket reaching `OPEN` rather than by the sheet vanishing, at
about 1.0 s from prompt to approval. The pointer does not move and the engine
never activates Chrome. `pyobjc-framework-Quartz` is required again, for
keyboard events only.

One promise the engine cannot keep on Chrome's behalf: Chrome activates its own
window when it shows this prompt. `DevToolsConnectionDialog` calls
`browser->GetWindow()->Activate()` before the sheet is built, unconditionally,
so the frontmost app becomes Chrome the moment a client connects, before the
engine has seen anything. WindowServer's own log confirms it on every run, on
the AXPress path and the keystroke path alike. The engine adds no activation
of its own; the approval, by either route, leaves the frontmost app as it found
it.

A sheet that survives both is logged `FAILED` and retried next sweep, as before.
There is still no synthetic mouse click anywhere in the engine.

#### macOS: a sheet is pressed only once it has stood for the activation guard

Chrome 154.0.8037.59, three clients queued, run hands-off: every `AXPress` made
within a poll of the sheet appearing returned success and did nothing, and every
press made a second later granted, each grant confirmed by the client's socket
reaching `OPEN` and answering `Browser.getVersion`. That is Chromium's
`InputEventActivationProtector`, which drops input for 500 ms after a
security-sensitive dialog appears, and on 154 it covers `AXPress`. The engine now
notes the sweep that first sees a sheet and presses it only once the guard has
passed, measured from then. A queued successor shares the predecessor's dedupe key
and is drawn the instant the predecessor goes, so the clock resets on each
verified approval. Cost: at most one poll plus the guard of latency on a fresh
sheet. `--once` therefore sees a sheet without pressing it; `--observe` is
unaffected.

On 154 the sheet is titled again (`AXTitle` "Allow remote debugging?", no heading
needed); the heading path stays for 153.

#### macOS: no keystroke without a live target, and no Tab walk without Keyboard navigation

Every keystroke is gated on the pressed sheet still being the same live node:
the references are re-read before the focus write, before each Tab and
immediately before Space, which must also still find Allow focused. An `AXPress`
that takes effect late would otherwise reach the keyboard path with the sheet
already gone, walk Tabs into whatever Chrome focuses next, and log a real
approval as `FAILED`, which the burst guard never counts. It is now reported as
the approval it was, with no key sent. A sheet that stops answering gets no key
either, and is retried next sweep.

The keyboard fallback depends on Keyboard navigation (System Settings > Keyboard,
bit 2 of `AppleKeyboardUIMode`), which is off by default. Chrome's dialogs follow
it. On 154 with it off, the `AXFocused` write on Allow read back false every time
and eighteen Tabs in eighteen attempts never reached the button; with it on, the
focus write landed and Space granted, four of four across three queued clients
and one with Chrome behind another app, each confirmed on the socket. With it off
the Tab walk is skipped and the sheet is retried next sweep. On 154 the fallback
is not reached at all once the press-age guard is in place.

#### macOS: the log says which decision was made

Every approval path now leaves a trail: each dialog-role candidate with its
label, heading and accept decision; the `AXPress` error code; and the liveness
and visibility of both references after each attempt. The false approvals above
were indistinguishable from real ones in the old log, which is why they survived
two releases.

## 1.2.0

The macOS engine, hardened against real load. Most of this began as a pull
request from [crimsonsunset](https://github.com/crimsonsunset) (#1). The rest
came out of testing it against queued prompts on a live Chrome 152.

### macOS: a scan that costs nothing when idle

The engine walked every Chrome window's accessibility tree twelve levels deep,
four times a second, whether or not a prompt existed. On a loaded page that walk
descends into the page's own AX-exposed DOM. Measured here at ~85 ms per sweep,
a third of a core spent finding nothing. The PR author saw 150-160 ms and CPU in
double digits over hours. The dialog only ever lives one level below a browser
window, as a sheet or a direct child, so that is all the scan reads now.

| idle scan, per Chrome process | |
|---|---|
| 1.1.0 | ~85 ms |
| 1.2.0 | 0.4 ms |

### macOS: an approval is counted only once the sheet is really gone

`[ACTION]` used to be written the moment `AXPress` returned success. It is now
written only after the sheet is verified gone, and "gone" is judged by the
identity of the AX references that were pressed, never by what sits at their
coordinates. That distinction matters. Chrome queues the consent prompt when
several clients connect at once and draws the next one at exactly the position
and size of the one just dismissed, so a geometry check reports "still up" for a
sheet that has gone, and a real approval gets logged FAILED. The burst guard
counts `[ACTION]` lines, so it went blind in precisely the burst it exists for.

Measured, three clients queued. Before: one client let in, zero `[ACTION]`, one
FAILED. After: three in, three `[ACTION]`, zero FAILED.

### macOS: no synthetic clicks, and the pointer never moves

Under that load `AXPress` can report success while the sheet outlives the verify
wait. That is slow teardown, not a button that ignores AX. A longer AX messaging
timeout and a re-press clears it. In the same three-client run one approval
landed first time and two on the retry, with no click of any kind.

The PR had covered that case with a hardware mouse click, which works but moves
the pointer, the one thing this engine promises never to do. A cursorless
alternative, `CGEventPostToPid` bound to the sheet's own window, was tried four
ways and Chrome ignored every one. So there is no synthetic click in the engine
at all now. A sheet that outlives the retries is logged FAILED and pressed again
next sweep, an unbounded AX retry at poll rate with a visible trail in the log,
the same observable failure mode as Windows rather than a blind click.
`pyobjc-framework-Quartz` is no longer required.

### Also

- Run from a bare script, the macOS tray no longer puts an icon in the Dock or
  an entry in Cmd-Tab. It declares itself an accessory, as a proper bundle would.
- `watcher_mac.py --diagnostics` logs trust, process discovery and per-sweep
  scan timing every five seconds. Cheap, and it is what produced the numbers
  above.

## 1.1.0

Two headline changes: **macOS support**, and a **critical memory fix for
Windows**. If you run the Windows build, this update is not optional.

### Windows: the engine leaked, and could outlive its tray

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

### macOS support

The full port: an Accessibility-API engine, an `NSWindow` cloud overlay, a
status-bar tray, and a LaunchAgent for start-at-login. Verified on real hardware
(macOS 26, Chrome 152). The prompt is a browser-level feature, so it behaves the
same there.

The macOS build is newer and has far less mileage than the Windows one; the
README's Known limitations section says so plainly, including that the
Accessibility grant attaches to your Python binary until a signed `.app` bundle
exists.

### Also

- The Windows status icon is the app's own cloud with a white check, from the
  same seed as the macOS one, so both platforms wear one silhouette and only the
  colour changes with state. A plain circle read as an anonymous dot among the
  notification-area icons.
- One `requirements.txt` for both platforms, with markers.
- A logo, and documentation art generated from the app's own renderer.

## 1.0.0

First release.

Chrome 144+ asks for consent every time a client attaches to its remote
debugging endpoint. Drive Chrome with several agents and those prompts stack up,
each one blocking its client until a human clicks Allow. There is no flag or
policy to turn this off, and the request to persist approval was closed as not
planned, so the only route is to click it. `Yes, Dev` sits in the tray and
clicks it for you.

Four parallel attaches go from ~35 seconds of waiting on a human to 2.4-4.4
seconds, unattended.

### What's in it

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

### Field data

From 8 days on the machine it was built on: 454 approvals, one failed click
(99.8%), no burst pauses at the default limit, and it restarted itself correctly
after a reboot. Twenty-one transient UI Automation errors were logged and
recovered from on the next 250ms sweep, costing nothing observable.

### Requirements

Windows, Chrome 144+, Python 3.9+ with `pystray` and `pillow`.

### Known limitations

English-language Chrome only (the dialog and button are matched by their text,
though both are overridable on `watcher.ps1`); the clouds are positioned for a
single unscaled display; Windows only by construction.
