# macOS setup and focus options

[Home](../README.md) | [Architecture](architecture.md) | [Troubleshooting](troubleshooting.md)

## Install and update

macOS uses the Python source package; the Windows portable ZIP does not run here.
Install Python 3.9+ with Tk, then run these commands from the extracted source:

```bash
python3 -m pip install -r requirements.txt
python3 yes_dev_mac.py
```

A python.org build or a Homebrew Python with Tk can be used. The dependencies
include rumps, Pillow, and the Cocoa, ApplicationServices, and Quartz bindings.
Quartz supplies the keyboard fallback. Use **Quit** before replacing source files.
Settings stay in `~/Library/Application Support/YesDev/`. If the interpreter or
app path changes, check Accessibility permission and recreate **Start at login**.

Grant **Accessibility** to the Python interpreter in System Settings > Privacy &
Security > Accessibility. The menu's **Accessibility** item opens the permission
page. The app cannot approve prompts without this permission. A source app grants
access to that interpreter; there is no signed macOS `.app` in this release.
**Start at login** writes a per-user LaunchAgent.

The recorded native checks used macOS 26 and Chrome 152, 153, and 154. Version
1.5.0 includes the previously merged macOS source changes; the Windows portable
release did not add a new macOS native test run. See [test notes](../tests/README.md).

## Focus behavior

Open **Settings… > Focus & connection**. All focus options are off by default.

| Option | Behavior |
|---|---|
| Off | Approve the prompt without trying to return focus. |
| Quiet focus | Detect a Chrome activation with a matching consent sheet and return focus to the previous app. |
| Fast focus via local relay | Prepare a short focus-return window before a client connection triggers the prompt. Clients must use the relay address. |
| Keep one Chrome connection open | With Fast focus, hold an approved Chrome connection and lend it to sequential clients. |

The focus helpers run only while approval is active. Turning approval off,
pausing, or choosing Observe only stops the relay and disconnects its clients.
Quitting the menu-bar app stops its helpers. The relay is an app-managed resident
process, not a separately installed system service. Clients must reconnect after
it restarts. This held-connection option first shipped in [v1.4.0 on October 3,
2026](https://github.com/dev-newb/yes-dev/releases/tag/v1.4.0).

Quiet focus checks for a recent absence of keyboard or mouse input before it
returns focus. Input after Fast focus is armed cancels the return. These controls
reduce an interruption; they do not prevent Chrome from activating or guarantee
where an in-flight keystroke lands. With a full-screen app, the activation can
also switch Spaces. See [focus test limits](../tests/README.md).

### Fast focus through the local relay (macOS, experimental)

In **Settingsâ€¦ â†’ Focus & connection**, choose **Fast focus via local relay**,
select Chrome's data folder, and Save. The data folder contains `Local State`
and `DevToolsActivePort` (normally `~/Library/Application Support/Google/Chrome`),
rather than its `Default` or `Profile 1` subfolder. Enable Remote Debugging in
Chrome at `chrome://inspect/#remote-debugging`.

Use the saved connection address shown in Settings, normally
`http://127.0.0.1:9333`, for the client's browser URL. For example, Playwright's
Python API connects with:

```python
browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9333")
```

Clients that accept a browser websocket can use
`ws://127.0.0.1:9333/devtools/browser/yesdev`. HTTP discovery at `/json/version`
returns this address without opening a connection to Chrome. The relay binds
only to loopback and rejects website Origin headers and unrelated Host headers.

With held-connection reuse off, each client gets its own upstream Chrome socket,
so normal CDP messages and session IDs remain independent. Only handshakes are queued: the focus guard
acknowledges a short-lived request immediately before the relay opens Chrome's
socket. Existing clients then continue concurrently. Disconnects close their
upstream socket; a later connection rereads Chrome's endpoint after a restart.

Fast focus applies to connections through this relay. Direct Chrome connections
still work, but do not get a focus restore while Fast focus is selected. The
normal guard and fast guard are mutually exclusive to avoid competing restores.
Turning approvals off, choosing Observe only, or pausing the app stops the relay
and disconnects its clients. The relay resumes with approval; clients reconnect.

This is an experimental shortcut based on request timing, not proof that a
particular activation belongs to a consent sheet. Input after arming cancels the
restore; recent input permits the connection without returning focus.

On its own, fast focus still creates one consent prompt per client connection.
Tick **Keep one Chrome connection open** in the same tab to stop that: the
relay keeps the first approved connection open and lends it to one client at a
time, reducing repeated prompts during sequential use. It also picks when: as soon as you open
Chrome yourself and it is in front with your hands off the keyboard and mouse
for two seconds, the relay asks for the connection then, so the one prompt
lands while you are already in Chrome and no agent triggers it later. If Chrome
starts in the background, or you are busy in it for the first minute, the first
agent opens the connection instead, as before. A Chrome that was already
running when Yes, Dev started is never prompted out of turn. All of this
removes the repeated prompts, not every reason Chrome comes forward: a tool
that opens a page in the foreground still brings Chrome to the front, as it
would over any connection.

Message ids are renumbered per client, so nothing in flight for a client that
left reaches the next one. When a client leaves, the relay undoes what it
changed in the browser: target discovery and auto-attach, download behavior,
request interception, certificate errors and permissions, then its sessions.
Browser contexts are disposed exactly when Chrome itself would dispose them on
a disconnect, which is when they were created with `disposeOnDetach: true`
(Playwright's are). Tabs it opened stay open. If Chrome refuses any of that
cleanup, or does not answer, the relay closes the held connection rather than
hand the next client a browser in an unknown state; the next client then costs
one prompt. While hold is on, `Browser.close` from any client ends that client,
never your Chrome. A client that connects while the connection is in use gets
its own connection and its own prompt. Chrome shows its "controlled by
automated test software" banner for as long as the connection is held. Clients
that need to work at the same time on one connection need the multiplexed proxy
in [proxy design notes](planning/cdp-proxy.md), which is still a plan.

## Known limitations

- The held connection is for Chrome and sequential reuse. A simultaneous client
  gets a separate upstream connection and may need another approval. Direct
  connections bypass the relay and do not get its reuse behavior.
- The relay can close a held socket if client-state cleanup fails, so the next
  connection can need a new prompt. This is not a guarantee of one prompt forever.
- **Toast card** can fall back to `tray.log` for the unbundled source app.
  Floating puffs do not depend on Notification Center permission.
- The keyboard fallback requires System Settings > Keyboard > **Keyboard
  navigation**. `defaults read -g AppleKeyboardUIMode` returning 2 or 3 indicates
  that it is enabled. Without it, the engine skips that fallback and retries.
- Login startup has not been verified across a real logout/login in the recorded
  macOS checks. No signed app bundle is provided.
- Clouds use the screen with the menu bar. Approval matching does not depend on
  the overlay's coordinates.

See [architecture](architecture.md#finding-the-dialog-on-macos) for sheet matching,
activation delay, keyboard checks, and rendering details. Historical focus timings
are kept with [validation history](testing/historical-measurements.md).
