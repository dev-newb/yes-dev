# Windows held relay validation

Checked on 19 October 2026 with a separate checkout. No installed app file or
setting was changed. The existing Yes, Dev tray, Chrome, Edge, and resident MCP
bridge stayed running. The test did not switch the input desktop.

## Results

- 98 selected Python tests passed with full Windows CPython 3.14.5. These cover
  shared transport, held connection cleanup, the launch primer core, Windows
  helper/manager, tray controls, packaging, approval scheduling, speed actions,
  independent launch, and exit records.
- Seven real headless Chrome test groups passed with Chrome 155.0.8059.40,
  Playwright 1.62.1, Puppeteer 25.14.0, and websockets 15.0.1. Sequential clients
  used the same upstream socket. Sessions detached, auto-attach reset, and a
  new client could open a page and evaluate JavaScript after each library left.
  Concurrent clients, `Browser.close`, Chrome restart, Host/Origin rejection,
  port conflict, and relay shutdown were also checked.
- Real native Chrome consent passed on separate Windows desktops at all three
  intervals: 150, 250, and 750 ms. Each run recorded one approval for three
  sequential clients, then one additional approval for a concurrent client.
  `Browser.close` kept Chrome usable. Graceful helper stop left the test Chrome
  and approval supervisor running.
- A local portable QA ZIP passed extraction, all 1,883 manifest file checks,
  isolated runtime imports, and exact dependency versions. The extracted
  runtime passed the real consent test at 250 ms. App source hashes matched
  the tested checkout. This was a dirty-tree QA build with a 1.6.0 candidate
  version; no release or tag was published.

## Test environment findings

Initial broad discovery included the existing macOS settings test that asserts
POSIX mode `0600`. Windows reports mode `0666` for that file. The macOS settings
module's seven tests were excluded from the selected Windows run; that source
and its tests were not changed.

The existing Explorer launch fixtures failed with a virtual environment's
`pythonw.exe` redirector. All 12 launch/runtime tests passed with the full
CPython interpreter. A new missing-dependency fixture initially inherited the
QA `PYTHONPATH`; it now uses `-E -S` to test the absent dependency correctly.

## Scope

The native consent tests use fresh profiles, unique approval-engine mutexes,
owned relay processes, and separate desktops. The headless tests use a fresh
profile and an unnamed Windows job for their own Chrome tree. Neither test uses
the current Chrome endpoint or changes the resident MCP bridge.

This is short functional validation. It does not establish a long-duration soak,
Windows focus restoration, or compatibility with every CDP client. Windows has
no launch-time connection opening in this change. See [test commands](../../tests/README.md)
and [user setup](../windows-relay.md).
