# Concurrent relay test record — 2026-10-22

The feature was built in a separate checkout on Windows. Tests used new
browser profiles, private process jobs, and separate relay ports. Native
consent tests used private Windows desktops without switching the input desktop.

## Results

| Check | Result |
| --- | --- |
| Windows regression set | 171 tests passed |
| Chrome 155.0.8059.85 | Concurrent raw CDP, Playwright, and Puppeteer passed with one upstream |
| Edge 155.0.4283.62 | Concurrent raw CDP, Playwright, and Puppeteer passed with one upstream |
| Both browsers running together | Separate relays worked at the same time |
| Browser restart | Chrome reconnected; the Edge client remained usable |
| Native Chrome consent | One approval for sequential and three concurrent clients |
| Native Edge consent | One approval for sequential and three concurrent clients |
| Chrome DevTools MCP 1.11.0 | Four independent stdio servers passed: two per browser, one upstream per browser |
| Portable build | Private runtime check and ZIP integrity check passed |
| Existing app, browsers, and bridge | All 14 recorded process identities remained; all three recorded installed/configuration file hashes matched |

The MCP checks called `list_pages`, `new_page`, `evaluate_script` (result 42),
and `close_page`. They used fresh local MCP server processes. They did not send
work to a Claude or Codex model or change either application's settings.

The protocol tests check overlapping client request IDs, independent discovery
and auto-attach subscriptions, separate sessions on a shared page, rejected
foreign session IDs, context disposal, idle reuse, `Browser.close`, malformed
traffic, slow-client queue limits, upstream failure, cleanup failure, and
reconnect cleanup across connection generations. Host, tray, packaging,
approval-supervisor, launch-primer, and lifecycle regression tests also passed.

## Additional fault and load checks

On 29 October, connecting the standard MCP server to the live profiles exposed
a startup-event burst bug. A healthy client could hit the 256-message output
queue while the relay reader consumed buffered events faster than its writer
was scheduled. The reader now gives writers a turn when the queue starts to
fill. The writer uses an in-task timeout, so fast sends drain without spawning
a task for every message. The original limits are unchanged. A 1,200-event
test failed before the fix and passes after it; all 171 Windows regression
tests pass. Chrome's live profile also passed page listing and a constant
expression with the standard MCP 1.12.0 server through an isolated fixed relay.
The installed fix then passed four concurrent standard MCP 1.12.0 clients:
two on Chrome with 57 listed pages and two on Edge with 14 listed pages.
Edge's stock server initially stalled during initialization. Reactivating its
inactive tab targets cleared that failure without a browser restart or page
reload. This is an observed recovery, not proof of the upstream root cause.

The second pass found and fixed three bugs:

- `Page.setDownloadBehavior` bypassed the shared download-settings check.
  Private Chrome and Edge both accepted the command before the fix. It is now
  rejected; the caller can use the browser command with its own context.
- Disposed context IDs stayed in the client's ownership set. The private
  browser probes retained 20 IDs after 20 disposals. Both browsers now complete
  100 create/dispose cycles with zero disposed IDs retained. Failed disposal
  keeps the ID until a successful retry.
- If a browser-session attachment reply was lost, the relay timed out without
  closing the shared socket. An unknown session could remain attached. The
  scripted-browser test now verifies that timeout retires the connection and
  tells clients to reconnect. Shutdown during attachment is also bounded.

The added live load test passed on Chrome and Edge together, with one upstream
connection per browser throughout. It checked 1,280 pipelined page commands with
replies returned out of order, 600 downstream reconnects, 200 context disposals,
the 16-client limit, and a client that stopped reading. A separate boundary test
filled all 4,096 pending-call slots and disconnected one client; the other
clients kept their original upstream connection.

With `--site-per-process`, two clients per browser also received their own
sessions for a worker, a nested worker, and a cross-origin iframe. Foreign
worker session IDs were rejected. A paused page loaded after one debugger
client disconnected and the other resumed it. The four-server MCP check was
repeated after the fixes and passed.

## Run the checks again

Use a full Windows Python interpreter for lifecycle tests. Install the pinned
dependencies from `packaging/windows-requirements.txt` into a separate QA
environment. In the following examples, replace the paths with your QA paths.

```powershell
$env:PYTHONPATH = "$PWD\tests;C:\QA\venv\Lib\site-packages"
$modules = Get-ChildItem tests -Filter 'test_*.py' |
    Where-Object { $_.BaseName -ne 'test_settings_model' } |
    ForEach-Object BaseName
& 'C:\Python314\python.exe' -m unittest @modules -v
```

The test run used Python 3.14.5 and websockets 15.0.1. `test_settings_model`
tests macOS settings and POSIX file permissions and is outside this Windows
run. No native macOS validation was performed. Initial runs through a virtual
environment's Windows Python redirector timed out in lifecycle fixtures; the
full-interpreter run above passed. A broad exploratory discovery run was stopped;
the latest 171-test run completed in about 56 seconds. The launch-primer tests
also passed separately on both the original release source and the new source.

The live library tests used Playwright 1.62.1 and Puppeteer Core 25.14.0.
Install those clients in separate QA directories, then run:

```powershell
& 'C:\QA\venv\Scripts\python.exe' tests/windows_multiplex_live.py `
  --output C:/QA/multiplex-live.json --node C:/node/node.exe `
  --playwright-modules C:/QA/playwright/node_modules `
  --puppeteer-modules C:/QA/puppeteer/node_modules

& 'C:\QA\venv\Scripts\python.exe' tests/windows_multiplex_mcp.py `
  --output C:/QA/multiplex-mcp.json --node C:/node/node.exe `
  --mcp-bin C:/QA/mcp/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js

& 'C:\QA\venv\Scripts\python.exe' tests/windows_multiplex_stress.py `
  --output C:/QA/multiplex-stress.json
```

For each native consent run, create a new output directory containing this
`config.json`. Set `browser` to `chrome` or `msedge` and run once for each:

```json
{
  "python": "C:\\QA\\venv\\Scripts\\python.exe",
  "interval": 250,
  "browser": "chrome",
  "multiplex": true
}
```

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/run-private-desktop.ps1 `
  -ScriptPath tests/windows-relay-consent-private.ps1 -SourcePath yes_dev.pyw `
  -ResultDirectory C:/QA/consent-chrome -TimeoutSeconds 180
```

The native test refuses the normal desktop. It creates a fresh profile and a
test copy of the approval supervisor with a separate mutex. It reports the
approval count and graceful helper exit. Each browser used the 250 ms approval
interval. This does not establish native consent results at every interval.
The harness waits for a window before starting its supervisor. An earlier
private Edge run failed during enumeration of the new desktop before any
approval; the run with this start order passed.

Build a private QA package with `packaging/build_windows.py`, using a new output
directory and a numeric QA version. `--allow-dirty` is for local checks only.
The package's `--check-runtime` path tests imports without starting the tray.
No package from this work was installed, tagged, or published as a release.

## Limits

The library tests establish protocol behavior on the two recorded browser
versions. Native consent checks establish approval reuse on private desktops.
They do not prove focus restoration, every browser version, every MCP tool,
or WSL stdio routing. Tabs remain shared. Browser-wide tracing and the other
unsupported commands listed in the [guide](../concurrent-relay.md) require a
direct connection. Chrome DevTools MCP's upstream support statement covers
Chrome; the Edge result here covers the recorded basic operations.
