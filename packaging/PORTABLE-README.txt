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

Optional local Chrome and Edge relays

"Keep one Chrome connection open" and "Keep one Edge connection open" are off by
default. Enable the required options in the tray menu. To approve Edge prompts,
also enable "Include Microsoft Edge". The relays run while Yes, Dev is enabled
and is not paused or in observe mode. CDP clients must connect through
http://127.0.0.1:9333 for Chrome or http://127.0.0.1:9334 for Edge, or use the
WebSocket URL returned by that address. Direct browser connections do not use
the relays. Only local clients can connect.

The relay reads Chrome's DevToolsActivePort file from the Chrome User Data
directory. The default is %LOCALAPPDATA%\Google\Chrome\User Data. For another
directory, quit Yes, Dev, set "relay_profile" in %LOCALAPPDATA%\YesDev\config.json
to that User Data directory, then start Yes, Dev. Use the User Data root, not a
profile subfolder such as Default. Use JSON path escapes (\\) for backslashes.
Chrome must expose its DevToolsActivePort file for that directory. The relay does
not start Chrome or change Chrome settings. Set "relay_port" in the same config
file if port 9333 is already in use; point clients at the selected port.

Edge defaults to %LOCALAPPDATA%\Microsoft\Edge\User Data. Use
"edge_relay_profile" and "edge_relay_port" to change its directory and port.
Both browsers must have remote debugging enabled through their inspect page.

The first client request opens the browser connection. Concurrent and later
clients share that one socket. Browser restart, connection loss, or cleanup
failure can require another approval. The relay does not restore Windows focus.
Use the separate status rows and relay log items in the tray to inspect state.

Playwright 1.60+ connections need noDefaults: true. Raw CDP attachment requires
flatten: true. Browser.close disconnects its caller and leaves the browser open.
Tabs and edits remain shared. Browser-wide tracing and default-context download
or permission changes are not supported. Use client-owned contexts for downloads
and permissions. Claude/Codex examples and the complete limits are at:
https://github.com/dev-newb/yes-dev/blob/v1.6.2/docs/concurrent-relay.md

For a runtime-only check, run YesDev.exe --check-runtime. It writes
runtime-check.json in this folder without starting the tray or approving prompts.
The check imports the relay and validates the pinned websockets version.

release-manifest.json records the source commit, dependency versions, and file
hashes. SHA256SUMS.txt is distributed beside the ZIP. The ZIP includes Python and
package licenses. The app and pure-Python dependencies remain editable files.

Project and source: https://github.com/dev-newb/yes-dev
License: see LICENSE.txt and licenses/.
