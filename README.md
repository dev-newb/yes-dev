<p align="center">
  <img src="docs/logo.svg" alt="Yes, Dev" width="460">
</p>

<p align="center">
  Approve Chrome and Edge remote-debugging prompts from your tray or menu bar.
</p>

Yes, Dev answers matching **Allow remote debugging?** prompts so local automation
tools can connect without waiting for a manual click. It runs on Windows and
macOS. Windows has a portable ZIP; macOS uses the Python source app.

**[Download Windows x64 v1.6.0](https://github.com/dev-newb/yes-dev/releases/download/v1.6.0/YesDev-1.6.0-windows-x64.zip)**
· [Release notes and checksums](https://github.com/dev-newb/yes-dev/releases/tag/v1.6.0)
· [Source ZIP](https://github.com/dev-newb/yes-dev/archive/refs/tags/v1.6.0.zip)

Auto-approval lets local programs request debugging access to your signed-in
browser. Yes, Dev matches the consent dialog; it does not identify or authorize
the tool requesting access. Enable it only when that tradeoff suits your machine.

![Floating approval notices](docs/puffs.png)

## Start on Windows

1. Download the Windows ZIP and **extract the whole folder** to a permanent location.
2. Open `YesDev.exe`. Keep the `app` and `runtime` folders beside it.
3. Use the tray menu to enable Edge, change settings, or choose **Start at login**.

Python, Tcl/Tk, and the app dependencies are included. You do not need to install
Python or pip. The package requires Windows 10/11 x64, Windows PowerShell 5.1,
.NET Framework 4.x, and the normal desktop Explorer shell. The launcher is unsigned.

The Windows app checks its launch context and starts the tray through desktop
Explorer when needed, independently of a caller such as Codex. Version 1.5.0 also
fixes all three speed choices, isolates stalled approval work, and records startup
and exit details. [Windows setup and source installation](docs/windows.md)

## Start on macOS

Install Python 3.9+ with Tk. Extract the source ZIP or clone this repository,
then run these commands from the source folder:

```bash
python3 -m pip install -r requirements.txt
python3 yes_dev_mac.py
```

Grant **Accessibility** to that interpreter in System Settings > Privacy &
Security > Accessibility. The app's **Accessibility** menu item opens the page.
Use **Settings…** for configuration. A signed macOS `.app` is not included.

The recorded native tests used macOS 26 and Chrome 152–154. Version 1.6.0
includes shared relay fixes tested on Windows; no new macOS native test run
was performed. [macOS setup, permissions, and limits](docs/macos.md)

## Main controls

The Windows tray exposes approval settings directly. On macOS, they are in
**Settings…**; the menu keeps status, the master switch, permissions, logs, and Quit.

| Control | Behavior |
|---|---|
| **On** | Enable or stop automatic approval. |
| **Start at login** | Create a Windows Startup shortcut or macOS LaunchAgent. |
| **Stay on for** | Set a disarm timer or choose **Until I turn it off**. |
| **Speed** / check interval | Windows choices are 150, 250, or 750 ms. |
| **Approve notice** | Floating puffs, toast, or silent. |
| **Pause on burst** | Set a rate limit and choose whether to ask or pause silently. |
| **Observe only** | Log matching dialogs without approving them. |
| **Include Microsoft Edge** | Include Edge consent dialogs. |
| **Open startup/exit records** | Windows lifecycle diagnostics. |

### Keep one Chrome connection open on Windows

**Windows v1.6.0 includes the relay** in both the ready-to-run ZIP and source
package. Download the release above, extract it, and start `YesDev.exe`.

Enable **Options > Keep one Chrome connection open**, then point your CDP tool
at `http://127.0.0.1:9333`. The first client opens the Chrome connection; later
clients can reuse it. A concurrent client gets a separate connection and can
need another approval. Direct Chrome connections do not use the relay.

The option is off by default. It stops on Off, pause, Observe only, or Quit, and
leaves Chrome running. Use the relay status row and **Open relay log** to check
it. Windows focus restoration is not included.
[Profile setup, client examples, and limits](docs/windows-relay.md)

### Approval defaults

Defaults on both platforms: approval on, Observe only off, Edge off, 250 ms
checks, floating puffs, no disarm timer, and a 60-approvals-per-minute burst limit.
Existing installations retain their saved settings.

The default burst action asks for **Stop** or **Allow for one hour**. If the
five-second question expires, approval stops until enabled again. With **Stop
silently**, it pauses and resumes after about a minute. A speed setting controls
the polling interval; it is not a promise of end-to-end approval latency.

### macOS focus and connection options

In **Settings… > Focus & connection**, choose:

- **Quiet focus**: return focus to your previous app after Chrome activates with
  a consent sheet.
- **Fast focus via local relay**: prepare the focus return before a client
  connection triggers the prompt. Tools must connect through the relay.
- **Keep one Chrome connection open**: with Fast focus, retain an approved
  Chrome connection and lend it to sequential clients. A concurrent client can
  need another connection and prompt.

These options are off by default. Quiet focus and Fast focus remain
**macOS-only**. Windows connection reuse is included in v1.6.0, as described
above. Input can cancel the macOS focus return;
a brief interruption or Space switch can still occur.
The relay stays resident while enabled and stops with the app, a pause, or
Observe mode. It is an app-managed helper, not a separate system service.
[Setup and reuse limits](docs/macos.md#focus-behavior)

## Update

Choose **Quit** in the old app first. Chrome and Edge can stay open. A second
copy exits without replacing the active tray, and replacing files does not make
the old process load new code.

- **Windows ZIP:** extract the new release to a permanent folder and open its
  `YesDev.exe`. If the folder changed, turn **Start at login** off, then on.
- **Source:** replace the source files, or run `git pull --ff-only` from a clean
  checkout. Install requirements again and start the platform's entry point.

Settings stay in `%LOCALAPPDATA%\YesDev\` on Windows and
`~/Library/Application Support/YesDev/` on macOS. They survive an update.
Do not delete the data folder to update the app.

## If approval stops

Check that the tray is present, **On** is enabled, and the app is not observing
or paused. Check the timer and Edge setting. With no disarm timer, inactivity
should not close the app.

Windows records worker stalls in `supervisor.log` and session details under
`%LOCALAPPDATA%\YesDev\lifecycle`. Use **Open startup/exit records** to find the
latest ready session, heartbeat, exception, and exit code. A separate exit watcher
can record forced termination. If both processes disappear, the next start marks
the missing exit record as unknown. Records cannot always name an external killer.

Independent startup does not mean a terminated tray automatically restarts.
Start it again if absent. A successful runtime check also does not prove browser
approval: `YesDev.exe --check-runtime` checks imports without starting the tray.
[Troubleshooting and complete log map](docs/troubleshooting.md)

## Support and limits

Yes, Dev handles the consent-based connection mode enabled through
`chrome://inspect/#remote-debugging`. Command-line remote debugging is a
different mode. Windows v1.5.0 matches English and Simplified Chinese dialogs;
other languages or changed browser titles can need code changes. A prompt that
fails title matching may not appear in the approval log.

The Windows release passed isolated Chrome/Edge connection checks, startup/exit
tests, and forced-stall recovery tests. Those checks are not a guarantee of
unattended stability on every machine. macOS permission, keyboard fallback,
focus, and held-connection limits are in the [macOS guide](docs/macos.md).

## More documentation

- [Windows guide](docs/windows.md) and [macOS guide](docs/macos.md)
- [Windows held-connection relay](docs/windows-relay.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture, process layout, and rendering](docs/architecture.md)
- [Tests and validation limits](tests/README.md)
- [Historical measurements](docs/testing/historical-measurements.md)
- [Portable package build](packaging/README.md)
- [Changelog](CHANGELOG.md)

## License

[MIT](LICENSE). The portable ZIP includes the bundled runtime and dependency licenses.
