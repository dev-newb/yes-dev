# Keep one Chrome connection open on Windows

The Windows relay keeps one approved Chrome debugging connection open. One
client at a time can use that connection. This reduces repeated approval prompts
when clients connect in sequence. The feature is off by default.

Included in **Windows v1.6.0**. [Download the ready-to-run ZIP](https://github.com/dev-newb/yes-dev/releases/download/v1.6.0/YesDev-1.6.0-windows-x64.zip),
extract the whole folder, and open `YesDev.exe`. The private runtime and relay
dependency are included. Older v1.5.0 packages do not include this feature.

[Windows installation](windows.md) | [Troubleshooting](troubleshooting.md)

## Set up

1. Enable remote debugging in Chrome at `chrome://inspect/#remote-debugging`.
2. In the Yes, Dev tray menu, select **Options → Keep one Chrome connection open**.
3. Point your browser tools at `http://127.0.0.1:9333`.

For example, with Playwright:

```python
browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:9333")
```

With Puppeteer:

```javascript
const browser = await puppeteer.connect({browserURL: 'http://127.0.0.1:9333'});
await browser.disconnect();
```

Raw CDP clients can read `/json/version` and use its `webSocketDebuggerUrl`.
Clients that connect directly to Chrome do not use the held connection.

The first client opens the Chrome connection. Chrome can show its normal
permission dialog, which the approval engine handles. The relay keeps the
connection open after that client disconnects. A second client that arrives
while the held connection is busy gets a separate Chrome connection and can
cause another prompt. Chrome restart, connection loss, or failed cleanup can
also require a new approval.
A client that disconnects with state-changing calls still pending also requires
a fresh connection, so the next client does not inherit unfinished session state.
The held connection accepts up to 4,096 unanswered calls. At that limit, new
calls receive an error; wait for replies or reconnect before sending more work.

## Select the Chrome directory and port

The default directory is `%LOCALAPPDATA%\Google\Chrome\User Data`. It must contain
the `DevToolsActivePort` file that Chrome publishes. Use the **User Data root**,
not its `Default` or `Profile 1` subfolder. The relay does not start Chrome or
change its settings.

For another directory or port, use **Open config**, quit Yes, Dev, then edit
these values in `%LOCALAPPDATA%\YesDev\config.json`. Keep the other settings:

```json
{
  "relay_hold": true,
  "relay_profile": "C:\\Users\\YOUR_NAME\\AppData\\Local\\Google\\Chrome\\User Data",
  "relay_port": 9333
}
```

Start Yes, Dev again. Set client URLs to the selected relay port. The relay port
must differ from Chrome's debugging port. If another program uses the relay
port, Yes, Dev reports an error; it does not stop that program. Invalid relay
settings disable the relay without disabling normal prompt approvals.

## Status and lifetime

The status row in **Options** shows whether the relay is starting, listening,
holding a Chrome connection, serving a client, or in an error state. **Open relay
log** opens `%LOCALAPPDATA%\YesDev\relay.log`. Per-run status and helper errors are
under `relay-runs`. Startup and exit reasons are recorded in the relay log.

The tray owns the helper. It stops the helper when you clear the relay option,
turn Yes, Dev off, pause it, select Observe only, or quit. The helper also exits
if the tray process ends. Relay shutdown closes its connections and leaves
Chrome running. An unexpected helper exit triggers a retry with a delay of up
to 60 seconds. Relay errors do not stop the approval engine.

This helper is separate from the resident Chrome DevTools MCP bridge. It does
not change that bridge, its scheduled task, or Codex's browser connection. To
use this relay, a tool must be configured to connect to its URL.

## Limits

- This Windows feature does not include macOS Quiet focus, Fast focus, or
  launch-time connection opening. Chrome can still bring a prompt to the front.
- The relay accepts local applications only. It rejects requests with an
  Origin header or an unexpected Host header. It is not an authentication
  boundary between programs that run under your Windows account.
- The relay resets supported browser settings, sessions, and temporary browser
  contexts before reusing a connection. It does not undo arbitrary page edits
  or close every tab opened by a client.
- With the relay enabled, `Browser.close` disconnects that client and keeps the
  shared Chrome process open. Use the browser's own controls to exit Chrome.
