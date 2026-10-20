# Windows portable release

Build from a full 64-bit Windows CPython 3.14 installation with Tcl/Tk and pip.
Use the Microsoft .NET Framework 4.x C# compiler installed with Windows. This
release was built with CPython 3.14.5; the manifest records the exact runtime.

```powershell
python packaging/build_windows.py --version 1.5.0 --output C:\builds\yesdev-1.5.0
python packaging/test_portable.py C:\builds\yesdev-1.5.0\YesDev-1.5.0-windows-x64.zip --extract-to "C:\builds\extracted with spaces"
```

Use an explicit real Python executable if `python` is a Windows Store alias.
The build requires a clean Git commit by default. `--allow-dirty` is for local
QA only; its manifest clearly records that the build contains uncommitted work.
The output directory must not exist, so an earlier build cannot be overwritten.

The app has editable Python files, the PowerShell worker, and a native launcher.
The private runtime copies the selected CPython install's interpreter, DLLs,
stdlib, Tcl/Tk, and original notices. It excludes its installed site-packages
and installs only the pinned app dependencies into a new target directory.
An isolated `python314._pth` blocks user/global Python imports and environment
path injection. Package and runtime licenses are included. This is a portable
distribution, not a single-file executable or an installer. The launcher is
unsigned. Build timestamps and native toolchain details can change archive
bytes; the manifest and checksum identify the exact published package.

Validate the extracted package's supervisor with real private browsers:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/run-supervisor-browser-tests.ps1 -Python "C:\builds\extracted with spaces\YesDev-1.5.0-windows-x64\runtime\python.exe" -SourcePath "C:\builds\extracted with spaces\YesDev-1.5.0-windows-x64\app\watcher.ps1" -ResultDirectory C:\builds\browser-results
```

These tests copy the worker/supervisor into private desktops, use fresh profiles,
and terminate only test-owned processes. They check all three speeds, Chinese
labels, exact ACTION persistence, forced-stall recovery, observe mode, and cleanup.
Do not test by closing an active user browser.

Create the final ZIP only after release commits are merged. Tag that exact commit
and publish the ZIP, `SHA256SUMS.txt`, and a source ZIP generated with `git archive`
from the same commit. Verify the downloaded asset checksum after publication.
The current runtime and user configuration must not be copied into the package.

The Windows relay uses the shared `cdp_relay.py` core and the `relay_windows.py`
host. Both files and `websockets==15.0.1` are included in new builds. The portable
runtime check imports both relay modules and the websockets asyncio client and
server APIs, then checks all dependency versions against the package pins. It
does not start the relay, tray, or a browser. The relay is off by default; the
portable README gives the opt-in steps and the Chrome User Data path setting.

Run the non-native package checks before building:

```powershell
python -m unittest discover -s tests -p test_windows_relay_packaging.py
```
