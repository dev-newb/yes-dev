# Windows tests

Run these tests with Windows PowerShell 5.1 and Python 3. The tests do not start
the tray app or the production watcher. They do not change installed app files.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/run-windows-tests.ps1
```

Use `-Python` to select a Python executable and `-ResultDirectory` to select a
new, empty result directory. Results include JSON reports and the native test log.

- `watcher-regression.ps1` extracts the original functions and one bounded loop
  body, with desktop access, processes and logging replaced by test data.
- Argument probes run the actual parameter and normalization code through
  `powershell.exe -File`.
- `tray-launch.py` extracts the real launch method and records process arguments.
  It never imports the app or starts a watcher.
- `tray-log.py` tests complete and partial events, event-marker text inside other
  log messages, replacement files, truncation, and bounded partial-line storage.
- `legacy-native.ps1` compiles the original C# helper and operates a separate
  test process with real Windows controls and an explicit test UIA provider.
  It verifies native COM actions, both languages, process filtering, observe
  mode, duplicate labels, rejected targets, and unavailable controls.
- `run-private-desktop.ps1` runs native tests on a separate Windows desktop.
  It does not switch the input desktop. Its default 45-second limit applies only
  to the test process tree. `-TimeoutSeconds` can extend it for a memory soak.
  The provider is stopped and desktop handles are closed.

The native controls are test fixtures. To also test the installed Chrome and
Edge browsers in English and Simplified Chinese, add `-IncludeBrowsers`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/run-windows-tests.ps1 -IncludeBrowsers
```

The browser tests use fresh profiles under the result directory and separate
Windows desktops. They set the remote-debugging preference only in these test
profiles, request a free loopback port, and require a real consent dialog and a
successful `Browser.getVersion` response. They test both the normal method and
the native fallback. They do not use the user's browser profiles, run a second
production watcher, disable browser sandboxing, or switch the input desktop.

The desktop is UI isolation, not a virtual machine or security sandbox. These
short tests do not prove long-duration memory usage or unattended stability.

## Memory soak

Create a new result directory with a `config.json` containing:

```json
{"mode":"normal","duration_seconds":1800,"idle_seconds":600}
```

Run `run-private-desktop.ps1` with `-ScriptPath tests/memory-soak.ps1`,
`-SourcePath watcher.ps1`, that `-ResultDirectory`, and `-TimeoutSeconds 1900`.
Use a second new result directory with `"mode":"forced-fallback"` to exercise the
native fallback. The two runs can use separate desktops at the same time.

The soak runs the full watcher in a separate process. The test copy has a unique
mutex; fallback mode also injects a failure at the primary invocation call. The
production source is unchanged. Each run opens fresh Chrome and Edge profiles,
waits through the idle phase, and then requests a new CDP connection every five
seconds. Every successful request must produce exactly one counted dismissal.

Samples record watcher private bytes, working set, handles and CPU time every
five seconds. The driver stops the watcher and its test browsers at the end.
`analyze-soak.py` evaluates the completed samples. A successful finite soak is
evidence for that workload and duration, not a guarantee for an indefinite run.
