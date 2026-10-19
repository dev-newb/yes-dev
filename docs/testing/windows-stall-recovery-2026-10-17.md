# Windows speed menu and stall recovery validation

The Windows tray now starts `watcher_supervisor.py`. That process discovers
consent windows without UI Automation and runs at most four scoped PowerShell
workers. A worker that stops completing sweeps is terminated after five seconds;
the next attempt uses the native legacy action first. Startup has a separate
ten-second allowance. Worker processes belong to a job with kill-on-close.

Each worker retains the existing exact title, owner, button-name and runtime-ID
checks. An ACTION record still requires dismissal of the original button or
dialog after an attempted action. Recovery timeouts do not count as approvals.

## Checks

- Speed-menu regression: the real pystray menu items save and launch integer
  intervals for 150, 250 and 750 ms. All three fail on the original menu code.
- Seven scheduler tests cover blocked work, continued discovery, startup timeouts,
  progress heartbeats, worker limits, retry fairness, owner identity, and cleanup.
- The Windows regression/native/log suite passes 136 existing checks. Native
  controls also verify that a scoped worker cannot select another HWND or PID.
- Four simultaneous writers persist 800 distinct approval records. File append
  and rotation use a shared mutex; console output occurs after its release.
- Private-desktop browser matrix: eight connections per case, split between
  fresh Chrome and Edge profiles, at 150, 250 and 750 ms. Each of the 24 connections
  must finish the CDP handshake, answer `Browser.getVersion`, and produce exactly
  one ACTION event. The 250 ms case requests and checks Chinese labels.
- At 150 ms, a test-only worker copy blocks for 30 seconds after finding Allow.
  Other dialogs must complete before timeout recovery. The supervisor must stop
  the blocked worker and retry with the native legacy method; that prompt's
  connection must then complete. The first measured recovery stopped the worker
  after 5.1 seconds without progress.
- Observe mode leaves connections pending and writes no ACTION records.
- Terminating the supervisor during the injected stall leaves no worker processes.

## Reproduce

Use an explicit Python executable with the Windows app dependencies installed.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/run-windows-tests.ps1 -Python python
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/run-supervisor-browser-tests.ps1 -Python python
```

The browser driver creates private desktops without switching the input desktop.
It uses fresh profiles, separate logs, and a test-specific engine mutex. Cleanup
targets only processes started by the test. No user profile or active browser is
used. These short tests prove the injected-stall recovery path, not the precise
cause of every possible browser-provider delay or indefinite unattended stability.

Direct `watcher.ps1` use retains its synchronous standalone mode. The tray and
`python watcher_supervisor.py` use the new supervised path.
