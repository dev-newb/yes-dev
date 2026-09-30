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
- `legacy-native.ps1` compiles the original C# helper and operates a separate
  test process with real Windows controls and an explicit test UIA provider.
  It verifies native COM actions, both languages, process filtering, observe
  mode, duplicate labels, rejected targets, and unavailable controls.
- `run-private-desktop.ps1` runs native tests on a separate Windows desktop.
  It does not switch the input desktop. Its 45-second limit applies only to the
  test process tree. The provider is stopped and desktop handles are closed.

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
tests do not prove long-duration memory usage or unattended stability.
