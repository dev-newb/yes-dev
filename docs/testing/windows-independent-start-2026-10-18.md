# Windows independent startup and lifecycle validation

Validated on Windows with Python 3.14.5 and Windows PowerShell 5.1.

## Launch behavior

`yes_dev.pyw` enters the runtime guard before importing tray dependencies. A
caller outside desktop Explorer is handed to the desktop's Shell automation
interface. The child verifies its desktop parent and current job limits.
An unsuccessful verification produces a launch failure record and exits.
The code does not treat a single breakaway flag as proof that outer jobs were left.

The no-UI process test starts a caller inside an owned kill-on-close job. It
waits for the independent child to report ready, closes the caller's job, and
checks that the caller ends while the child stays alive and writes another
heartbeat. The child then exits normally and its exit watcher records code zero.

The installed app was also started through the normal `yes_dev.pyw` entry point
from a tool process. The launch record showed caller job flags `0x2800`. The
ready tray's parent was desktop Explorer; its current job flags were `0x800`,
without kill-on-close. The exit watcher opened the tray's process handle before
the application loaded. A duplicate real-app start recorded `duplicate_instance`
and actual exit code zero, while the existing tray stayed active.

## Diagnostics

Records are under `%LOCALAPPDATA%\YesDev\lifecycle`. `active.json` points to the
last session that reached tray readiness. Per-session files contain process
identity, parent image, job limits, Python version, working directory, readiness,
five-second heartbeats, exit requests, tracebacks, and the observed exit code.

The app records tray Quit, tray-loop stop, Windows session-end messages, and
expected/unexpected engine exits. The exit watcher holds a handle to the exact
tray process and checks its creation time, so PID reuse cannot substitute another
process. It can observe termination that bypasses Python cleanup. A missing
cleanup record is marked unknown, not attributed to a named external program.

If Windows or a whole-tree termination also stops the watcher, the next startup
reconciles the missing exit record using the saved process identities and last
heartbeat. An error loading the runtime helper itself is written to the separate
`bootstrap-error.log` file. No system-wide process auditing was enabled.

## Results

- 12 new runtime tests passed: launch policy, caller-job termination, import/main/
  thread exceptions, forced termination with code 77, cleanup bypass with code 23,
  normal exit, stale-session recovery, missing bootstrap helper, and simulated
  Windows session-end/tray-stop messages.
- The speed-menu test and seven worker-scheduler tests passed.
- The existing Windows regression/native/launch/log suite passed 136 checks.
- Four concurrent writers saved 800 distinct records. The stress fixture retries
  bounded lock timeouts in the same way as pending approval confirmations. Its
  process exit codes are checked through explicit process handles.
- The private-desktop Chrome/Edge matrix passed 24 handshakes, version replies,
  and matching ACTION events across 150, 250 and 750 ms. Observe mode and forced
  stall/shutdown checks passed too.
- Main Chrome and Edge processes retained their original creation times.

Run `tests/run-windows-tests.ps1` with an explicit Python executable to include
the runtime checks. Run `tests/run-supervisor-browser-tests.ps1` for the separate
browser matrix. Runtime fixtures have no UI and use private diagnostic folders.
They neither open a browser nor send session-end messages to Windows. Browser
tests use private desktops, fresh profiles, and only test-owned process cleanup.
No long-duration or real Windows logoff test was performed.
