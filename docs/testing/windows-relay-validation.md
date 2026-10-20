# Windows held relay validation

Checked on 19 October 2026 with a separate checkout. No installed app file or
setting was changed. The existing Yes, Dev tray, Chrome, Edge, and resident MCP
bridge stayed running. The test did not switch the input desktop.

## Results

- 133 selected Python tests passed with full Windows CPython 3.14.5. These cover
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
- Seven additional live fault cases passed against the Windows host: binary
  and escaped close messages from a concurrent client, forced helper failure
  and retry, 40 rapid setting changes, an occupied port that later became free,
  a partial Chrome endpoint file, and five disable/restart cycles with an active
  client. These also verified real helper startup from a Windows virtual
  environment. All owned helper processes stopped after each case.
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

## Adversarial follow-up

The additional tests reproduced and repaired these faults:

- The concurrent-client close guard checked a literal text substring. Binary
  JSON and an escaped method name bypassed it. With old core code and private
  real Chrome, binary JSON failed at Chrome's protocol layer; an escaped
  `Browser.close` exited the test browser. The guard now decodes both forms and
  disconnects only that client. The 20 new transport tests also check malformed
  messages, repeated IDs, client limits, header duplication, and disconnect storms.
- A borrower could disconnect before an attach or context response arrived.
  The old session/context then appeared while the next borrower was active.
  Such unresolved state-changing calls now retire the socket. A cancelled reset
  also retires it before it can be reused. Boolean IDs are rejected as invalid
  CDP integer IDs.
- Status reads could delay controls and shutdown. A separate status sampler
  now updates a run-bound cache. Lifecycle control never waits for that read.
  Tests hold the read blocked and require the owned helper to stop before the
  read is released. Other tests cover stale state, stop-file denial, wait-handle
  failures, and parent identity checks.
- Windows virtual-environment Python uses a redirector with a different PID
  from the host. The manager now starts the base interpreter directly and gives
  only that child the virtual environment's dependency path. Real fault tests
  passed from the QA virtual environment after this change.

One browser-client rerun failed because the test read `DevToolsActivePort` while
Chrome held its write lock. The test now waits for readable, valid content; all
seven original real-client groups passed on the final rerun. The fault tests
shorten retry delays only on the test manager instance. They do not change app
defaults. No macOS native run or long-duration soak was performed.

## Scope

The native consent tests use fresh profiles, unique approval-engine mutexes,
owned relay processes, and separate desktops. The headless tests use a fresh
profile and an unnamed Windows job for their own Chrome tree. Neither test uses
the current Chrome endpoint or changes the resident MCP bridge.

This is short functional validation. It does not establish a long-duration soak,
Windows focus restoration, or compatibility with every CDP client. Windows has
no launch-time connection opening in this change. See [test commands](../../tests/README.md)
and [user setup](../windows-relay.md).
