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
