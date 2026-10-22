# Concurrent relay test record — 2026-10-22

The feature was built in a separate checkout on Windows. Tests used new
browser profiles, private process jobs, and separate relay ports. Native
consent tests used private Windows desktops without switching the input desktop.

## Results

| Check | Result |
| --- | --- |
| Windows regression set | 164 tests passed |
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
the final 164-test run completed in about 49 seconds. The launch-primer tests
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
