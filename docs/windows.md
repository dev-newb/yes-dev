# Windows install and update

[Home](../README.md) | [Troubleshooting](troubleshooting.md) | [Architecture](architecture.md)

## Portable app

Download [YesDev-1.5.0-windows-x64.zip](https://github.com/dev-newb/yes-dev/releases/download/v1.5.0/YesDev-1.5.0-windows-x64.zip)
and extract the whole folder to a permanent location. Open `YesDev.exe`.
Keep `app` and `runtime` beside the launcher. Python, Tcl/Tk, and the app
dependencies are included. No Python or pip installation is needed.

The package requires Windows 10/11 x64, the normal desktop Explorer shell,
Windows PowerShell 5.1, and .NET Framework 4.x. The launcher is unsigned.
Normal use does not require administrator rights. You can compare the ZIP
against the release's [SHA256SUMS.txt](https://github.com/dev-newb/yes-dev/releases/download/v1.5.0/SHA256SUMS.txt).
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
the [current source ZIP](https://github.com/dev-newb/yes-dev/archive/refs/heads/main.zip)
or clone the repository, then run these commands from the source folder.
The [v1.5.0 source ZIP](https://github.com/dev-newb/yes-dev/archive/refs/tags/v1.5.0.zip)
is a fixed older version and does not include the Windows relay.

```powershell
python -m pip install -r requirements.txt
pythonw yes_dev.pyw
```

If `python` resolves to a Windows Store alias, use the full path to the installed
interpreter. For a Git installation, quit the tray and run `git pull --ff-only`,
then install requirements again and start the app. A source ZIP update replaces
the old source files; it does not require deleting the user data folder.

## Keep one Chrome connection open

Current `main` includes an optional relay for sequential Chrome debugging
clients. The published v1.5.0 portable app does not include it. Use current
source or a portable build made from `main`.

1. Enable Chrome remote debugging at `chrome://inspect/#remote-debugging`.
2. Select **Options > Keep one Chrome connection open** in the tray.
3. Connect your browser tool to `http://127.0.0.1:9333`.

The default Chrome User Data directory is
`%LOCALAPPDATA%\Google\Chrome\User Data`. It must contain Chrome's
`DevToolsActivePort` file. For another directory or port, use **Open config**,
quit Yes, Dev, change `relay_profile` or `relay_port`, then restart it.

The first client can need an approval. Later sequential clients reuse that
connection. A concurrent client gets a separate connection and can need another
approval. The relay is off by default and stops on Off, pause, Observe only,
Quit, or loss of its parent tray process. It leaves Chrome running.

Use the status row and **Open relay log** for connection errors. Quiet focus,
Fast focus, and launch-time connection opening remain macOS features. The
Windows relay does not change an existing Chrome DevTools MCP bridge or its
scheduled task. [Client examples, settings, and limits](windows-relay.md)

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

Version 1.5.0 supports English and Simplified Chinese consent titles and Allow
labels. The current source keeps that approval behavior and adds the optional
Windows relay described above.

Cloud notices use the primary monitor and have limited multi-monitor/DPI test
coverage. Overlay positioning does not control dialog matching. See
[build instructions](../packaging/README.md) for package internals and
[validation notes](../tests/README.md) for isolated test commands and limits.
