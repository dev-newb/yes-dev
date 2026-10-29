# Windows install and update

[Home](../README.md) | [Troubleshooting](troubleshooting.md) | [Architecture](architecture.md)

## Portable app

Download [YesDev-1.6.2-windows-x64.zip](https://github.com/dev-newb/yes-dev/releases/download/v1.6.2/YesDev-1.6.2-windows-x64.zip)
and extract the whole folder to a permanent location. Open `YesDev.exe`.
Keep `app` and `runtime` beside the launcher. Python, Tcl/Tk, and the app
dependencies are included. No Python or pip installation is needed.

The package requires Windows 10/11 x64, the normal desktop Explorer shell,
Windows PowerShell 5.1, and .NET Framework 4.x. The launcher is unsigned.
Normal use does not require administrator rights. You can compare the ZIP
against the release's [SHA256SUMS.txt](https://github.com/dev-newb/yes-dev/releases/download/v1.6.2/SHA256SUMS.txt).
The included manifest identifies the source commit and payload files.

Right-click the tray icon and enable **Start at login** if wanted. It creates
a shortcut in your Startup folder. There is no Windows service to install.

The entry point checks the launch context before it imports the tray libraries.
A start from Codex, a terminal, or another tool is handed to desktop Explorer.
The child verifies its desktop parent and job limits. If that verification fails,
the app records the failure rather than starting a tray tied to the caller.
Existing shortcuts use the same guarded entry point.

Independence from the caller is not automatic recovery after every external
termination. If another program stops the tray, use the lifecycle records to
check the exit and start Yes, Dev again. There is no idle-shutdown rule.

## Update or switch installations

1. Choose **Quit** in the old Yes, Dev tray. Chrome and Edge can stay open.
2. Extract the new ZIP to a permanent folder and open its `YesDev.exe`.
3. If the folder changed, turn **Start at login** off, then on, to replace its shortcut.

Both source and portable installations use `%LOCALAPPDATA%\YesDev\config.json`.
Settings and logs stay there across updates. Only one tray instance runs; a
second start exits without replacing the first. Replacing files while the old
app is running does not make it load the new code.

## Source installation

Use a real Windows Python 3.9+ interpreter and Windows PowerShell 5.1. Download
the [v1.6.2 source ZIP](https://github.com/dev-newb/yes-dev/releases/download/v1.6.2/YesDev-1.6.2-source.zip)
or clone the repository, then run these commands from the source folder.

```powershell
python -m pip install -r requirements.txt
pythonw yes_dev.pyw
```

If `python` resolves to a Windows Store alias, use the full path to the installed
interpreter. For a Git installation, quit the tray and run `git pull --ff-only`,
then install requirements again and start the app. A source ZIP update replaces
the old source files; it does not require deleting the user data folder.

## Share one connection per browser

Version 1.6.2 includes independent Chrome and Edge relays. Several clients can
share each browser's upstream connection at the same time.

1. Enable remote debugging at `chrome://inspect/#remote-debugging` or
   `edge://inspect/#remote-debugging`.
2. Select **Options > Keep one Chrome connection open** and/or
   **Keep one Edge connection open** in the tray. Select **Include Microsoft
   Edge** as well if Yes, Dev should approve Edge prompts.
3. Connect your browser tool to `http://127.0.0.1:9333` for Chrome or
   `http://127.0.0.1:9334` for Edge.

The default Chrome User Data directory is
`%LOCALAPPDATA%\Google\Chrome\User Data`. It must contain Chrome's
`DevToolsActivePort` file. For another directory or port, use **Open config**,
quit Yes, Dev, change `relay_profile` or `relay_port`, then restart it.

Edge defaults to `%LOCALAPPDATA%\Microsoft\Edge\User Data`; its configuration
keys are `edge_relay_profile` and `edge_relay_port`.

The first client can need an approval. Later clients share that connection,
including concurrent clients. Both relays are off by default and stop on Off,
pause, Observe only, Quit, or loss of the parent tray process. Browsers stay open.
Connection loss or failed cleanup can require a new approval.

Use the status row and **Open relay log** for connection errors. Quiet focus,
Fast focus, and launch-time connection opening remain macOS features. The
Windows relay does not change an existing Chrome DevTools MCP bridge or its
scheduled task. [Client examples, settings, and limits](concurrent-relay.md)

## Diagnostics and supported mode

Use **Open startup/exit records** for startup, heartbeat, exception, and exit
details. Use **Open log** for approval records. See [troubleshooting](troubleshooting.md)
for the file map and what a missing exit record means.

For a portable-runtime check that does not start the tray or approve anything:

```powershell
.\YesDev.exe --check-runtime
```

Read `runtime-check.json` beside the launcher. Keep that check separate from
approval testing; successful imports do not prove a browser connection.

Version 1.6.2 supports English and Simplified Chinese consent titles and Allow
labels, and includes the optional Windows relay described above.

Cloud notices use the primary monitor and have limited multi-monitor/DPI test
coverage. Overlay positioning does not control dialog matching. See
[build instructions](../packaging/README.md) for package internals and
[validation notes](../tests/README.md) for isolated test commands and limits.
