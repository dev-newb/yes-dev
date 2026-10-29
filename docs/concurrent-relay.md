# Concurrent Chrome and Edge relay on Windows

Windows **v1.6.2** includes concurrent CDP sharing in the
[ready-to-run ZIP](https://github.com/dev-newb/yes-dev/releases/download/v1.6.2/YesDev-1.6.2-windows-x64.zip)
and source package. Each browser has one upstream WebSocket and its own local
port. Several clients can use each port at the same time. Both options are off
by default. The existing `relay_hold` setting selects concurrent Chrome sharing.
The macOS relay keeps its existing behavior.

## Set up the browser connections

1. Enable remote debugging in each browser that you want to use:
   `chrome://inspect/#remote-debugging` or `edge://inspect/#remote-debugging`.
2. In the tray, select **Options > Keep one Chrome connection open** and/or
   **Options > Keep one Edge connection open**.
3. To let Yes, Dev approve Edge prompts, also select **Include Microsoft Edge**.
   Without this option, approve the first Edge prompt yourself.
4. Configure each client to use its relay address:

| Browser | Default address | User Data directory |
| --- | --- | --- |
| Chrome | `http://127.0.0.1:9333` | `%LOCALAPPDATA%\Google\Chrome\User Data` |
| Edge | `http://127.0.0.1:9334` | `%LOCALAPPDATA%\Microsoft\Edge\User Data` |

Use the User Data root that contains `DevToolsActivePort`, not `Default` or
`Profile 1`. The relay does not start a browser or change its settings.

The first client opens the upstream connection. Later clients share it, even
when other clients are active. Browser restart, upstream loss, relay restart,
or a cleanup failure can require a new approval. A session-attachment timeout
also closes that browser connection, so its clients must reconnect. There is no
fallback that opens an extra upstream connection for a concurrent client.

To change directories or ports, quit Yes, Dev and edit these keys in
`%LOCALAPPDATA%\YesDev\config.json`. Preserve the other settings, then start the
app again. Do this only when you are ready to stop the current relay clients.

```json
{
  "relay_hold": true,
  "relay_profile": "C:\\Users\\YOUR_NAME\\AppData\\Local\\Google\\Chrome\\User Data",
  "relay_port": 9333,
  "edge_relay_hold": true,
  "edge_relay_profile": "C:\\Users\\YOUR_NAME\\AppData\\Local\\Microsoft\\Edge\\User Data",
  "edge_relay_port": 9334
}
```

Use different ports for Chrome and Edge. A port conflict reports an error; it
does not stop the program that owns the port. Invalid Edge settings disable
only the Edge relay. Existing Chrome settings retain their key names.

## Route Codex through the relay

The relay speaks CDP. Use an MCP server such as `chrome-devtools-mcp` to give
Codex browser tools. The following Windows configuration uses version 1.11.0,
which was used for the isolated tests. Add these tables to the appropriate
Codex `config.toml` when you are ready to change client routing:

```toml
[mcp_servers.yesdev_chrome]
command = "cmd"
args = ["/d", "/c", "npx", "-y", "chrome-devtools-mcp@1.11.0", "--browserUrl", "http://127.0.0.1:9333", "--no-usage-statistics", "--no-performance-crux", "--no-category-performance"]
startup_timeout_sec = 30

[mcp_servers.yesdev_edge]
command = "cmd"
args = ["/d", "/c", "npx", "-y", "chrome-devtools-mcp@1.11.0", "--browserUrl", "http://127.0.0.1:9334", "--no-usage-statistics", "--no-performance-crux", "--no-category-performance"]
startup_timeout_sec = 30
```

Node.js and npm must be available to the Windows process. Codex supports
user configuration at `~/.codex/config.toml` and project configuration in a
trusted project's `.codex/config.toml`. See the
[official MCP configuration guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
The performance category is disabled because this relay rejects browser-wide
tracing. Use a separate direct browser connection for those tools.

Use the new `yesdev_chrome` or `yesdev_edge` tools in a new client session.
Adding these entries does not redirect an existing plugin, resident bridge,
or the built-in Codex browser. In particular, a bridge that uses `--autoConnect`
still connects directly. Its own backend arguments must be changed to
`--browserUrl` before it can use this relay. Do not combine `--autoConnect`
with these examples or replace a working bridge during active work.

## Route Claude Code through the relay

Run these commands from a Windows terminal when you are ready to add the
servers. They add separate names; they do not replace an existing server.

```powershell
claude mcp add --transport stdio --scope user yesdev-chrome -- cmd /d /c npx -y chrome-devtools-mcp@1.11.0 --browserUrl http://127.0.0.1:9333 --no-usage-statistics --no-performance-crux --no-category-performance
claude mcp add --transport stdio --scope user yesdev-edge -- cmd /d /c npx -y chrome-devtools-mcp@1.11.0 --browserUrl http://127.0.0.1:9334 --no-usage-statistics --no-performance-crux --no-category-performance
```

For Claude Code in WSL, use `/mnt/c/Windows/System32/cmd.exe` in place of `cmd`.
This starts the MCP server on Windows, so `127.0.0.1` refers to the Windows
relay. A Linux MCP process on WSL's default NAT network has a different
loopback address. The relay intentionally does not listen on a network-facing
interface. WSL routing is a configuration example; the live tests used native
Windows stdio processes.

Use `/mcp` to check server status, then select the tools for the required
browser. See [Claude Code's MCP guide](https://code.claude.com/docs/en/mcp).
The upstream [Chrome DevTools MCP project](https://github.com/ChromeDevTools/chrome-devtools-mcp)
officially supports Chrome. The tested Edge basic operations work, but this
does not establish support for every MCP tool on Edge.

## Direct CDP libraries

For Playwright 1.60 or later, use `noDefaults: true`. The default Playwright
connection changes download settings in the shared default context; the relay
rejects that change. Private contexts created by the client can set their own
download behavior and permissions.

```javascript
const browser = await playwright.chromium.connectOverCDP(
  'http://127.0.0.1:9333', {noDefaults: true});
// Use 9334 for Edge. browser.close() disconnects this Playwright connection.
await browser.close();

const other = await puppeteer.connect({browserURL: 'http://127.0.0.1:9334'});
other.disconnect();
```

See [Playwright's connection options](https://playwright.dev/docs/api/class-browsertype#browser-type-connect-over-cdp).
Raw clients read `/json/version` and use its `webSocketDebuggerUrl`. Use flattened
sessions (`flatten: true`) for attach and auto-attach commands.

## Shared state and limits

- Each client gets a Chromium browser-target session. Request IDs are remapped;
  replies and session events go only to their owner. Discovery and auto-attach
  subscriptions are separate for each client.
- Tabs and page contents remain shared. Clients can navigate or close the same
  tab. Assign separate tabs to agents to avoid conflicting edits. This is not
  a security boundary between clients or other local applications. Cookies and
  cache are shared within each browser context.
- Disconnect detaches the client's sessions and disposes contexts it created.
  `disposeOnDetach` is always enabled for new contexts. Tabs created in the
  shared default context remain open. Page edits are not undone.
- `Browser.close` disconnects only that client. Use browser controls to exit
  Chrome or Edge.
- Browser commands that change shared settings, including default-context
  downloads and permissions, are rejected. Download and permission commands
  are allowed for a context created by the calling client. `Tracing`, `Security`, and `SystemInfo`
  commands require a direct connection. Legacy nested-message transport,
  extra browser-target attachment, remote locations, and protocol exposure
  are also rejected. The deprecated `Page.setDownloadBehavior` command is
  rejected; use `Browser.setDownloadBehavior` with an owned context instead.
  A rejected command returns a CDP error.
- A browser must support `Target.attachToBrowserTarget`. If it does not, the
  client fails with an error. The relay does not open another browser socket.
- Each browser supports up to 16 clients, 256 unanswered calls per client,
  and 4,096 unanswered calls total. Output queues hold up to 256 messages or
  64 MiB per client. A slow client is disconnected without blocking others.
- Only loopback application requests are accepted. Requests with an Origin
  header or an unexpected Host header are rejected.

The two status rows show active client counts. Chrome uses `relay.log` and
`relay-runs` under `%LOCALAPPDATA%\YesDev`. Edge uses the same names under its
`edge-relay` subdirectory. Off, pause, Observe only, and Quit stop both helpers.
Each checkbox stops only its own helper. Browser processes remain running.

## Validation

See [the test record](testing/concurrent-relay.md) for versions, commands,
native consent evidence, and test limits. The feature was built and tested in
a separate checkout. No active user browser, installed Yes, Dev files, or
resident bridge configuration was changed.
