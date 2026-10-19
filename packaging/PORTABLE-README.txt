Yes, Dev - portable Windows x64 app

1. Extract this entire ZIP to a permanent folder.
2. Open YesDev.exe. No Python or pip installation is needed.
3. Use the Yes, Dev tray icon to change settings or quit.
4. To start at login, enable "Start at login" in the tray menu.

Windows 10/11 x64, Windows PowerShell 5.1, .NET Framework 4.x, and the normal
Explorer desktop shell are required. The ZIP includes a private CPython runtime
and the app dependencies. No administrator rights are needed for normal use.
The launcher is not code-signed. Windows may show a publisher warning.

Keep the app and runtime subfolders next to YesDev.exe. Do not run from inside
the ZIP. The launcher starts the same guarded Windows entry point used by the
source app; the tray is started by desktop Explorer, independently of the caller.

If Yes, Dev is already running, the second copy exits without replacing it.
To update: quit the old copy, extract the new release to a permanent location,
and start YesDev.exe. Your browsers can stay open. If you changed the app folder,
turn "Start at login" off and then on so the shortcut uses the new path.

Settings and logs remain in %LOCALAPPDATA%\YesDev. The ZIP uses the same settings
as a source installation. Quit the previous copy before switching installations.
Use "Open startup/exit records" for lifecycle diagnostics. A record can give the
exit code and last heartbeat; it cannot always identify an external terminator.

For a runtime-only check, run YesDev.exe --check-runtime. It writes
runtime-check.json in this folder without starting the tray or approving prompts.

release-manifest.json records the source commit, dependency versions, and file
hashes. SHA256SUMS.txt is distributed beside the ZIP. The ZIP includes Python and
package licenses. The app and pure-Python dependencies remain editable files.

Project and source: https://github.com/dev-newb/yes-dev
License: see LICENSE.txt and licenses/.
