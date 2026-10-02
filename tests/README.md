# Windows tests

Run these tests with Windows PowerShell 5.1 and Python 3. The tests do not start
the tray app or the production watcher. They do not change installed app files.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/run-windows-tests.ps1
```

Use `-Python` to select a Python executable and `-ResultDirectory` to select a
new, empty result directory. Results include JSON reports and the native test log.

- `watcher-regression.ps1` extracts the original functions and one bounded loop
  body, with desktop access, processes and logging replaced by test data.
- Argument probes run the actual parameter and normalization code through
  `powershell.exe -File`.
- `tray-launch.py` extracts the real launch method and records process arguments.
  It never imports the app or starts a watcher.
- `tray-log.py` tests complete and partial events, event-marker text inside other
  log messages, replacement files, truncation, and bounded partial-line storage.
- `log-writer.ps1` tests 1,000 writes under a held reader, required-write failure
  and recovery, blocked rotation, UTF-8 labels, and console-failure deduplication.
- `legacy-native.ps1` compiles the original C# helper and operates a separate
  test process with real Windows controls and an explicit test UIA provider.
  It verifies native COM actions, both languages, process filtering, observe
  mode, duplicate labels, rejected targets, and unavailable controls.
- `run-private-desktop.ps1` runs native tests on a separate Windows desktop.
  It does not switch the input desktop. Its default 45-second limit applies only
  to the test process tree. `-TimeoutSeconds` can extend it for a memory soak.
  The provider is stopped and desktop handles are closed.

The native controls are test fixtures. To also test the installed Chrome and
Edge browsers in English and Simplified Chinese, add `-IncludeBrowsers`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/run-windows-tests.ps1 -IncludeBrowsers
```

Add `-BurstSize 4` to open four simultaneous connections in each browser/language
and invocation-mode case. All four handshakes must complete, each must answer a
CDP version request, and the watcher must emit exactly four counter events.

The browser tests use fresh profiles under the result directory and separate
Windows desktops. They set the remote-debugging preference only in these test
profiles, request a free loopback port, and require a real consent dialog and a
successful `Browser.getVersion` response. They test both the normal method and
the native fallback. They do not use the user's browser profiles, run a second
production watcher, disable browser sandboxing, or switch the input desktop.

The desktop is UI isolation, not a virtual machine or security sandbox. These
short tests do not prove long-duration memory usage or unattended stability.

## Memory soak

Create a new result directory with a `config.json` containing:

```json
{"mode":"normal","duration_seconds":1800,"idle_seconds":600}
```

Run `run-private-desktop.ps1` with `-ScriptPath tests/memory-soak.ps1`,
`-SourcePath watcher.ps1`, that `-ResultDirectory`, and `-TimeoutSeconds 1900`.
Use a second new result directory with `"mode":"forced-fallback"` to exercise the
native fallback. The two runs can use separate desktops at the same time.

The soak runs the full watcher in a separate process. The test copy has a unique
mutex; fallback mode also injects a failure at the primary invocation call. The
production source is unchanged. Each run opens fresh Chrome and Edge profiles,
waits through the idle phase, and then requests a new CDP connection every five
seconds. Every successful request must produce exactly one counted dismissal.

Samples record watcher private bytes, working set, handles and CPU time every
five seconds. The log observer opens the file with shared read/write/delete
access and counts complete ACTION records, rather than the separate summary
message. `soak-log-reader.ps1` tests this while a writer holds the file open.
The driver stops the watcher and its test browsers at the end.
`analyze-soak.py` evaluates the completed samples. A successful finite soak is
evidence for that workload and duration, not a guarantee for an indefinite run.

# macOS

Run the focus-guard decision regressions with the macOS dependencies installed:

```bash
python3 -m unittest discover -s tests/mac -p 'test_*.py' -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

These tests run the guard's activation handler with a simulated clock, input
events, and delayed sheet reads. They cover input during a scan or retry,
including scans longer than the quiet-input threshold, and preserve the quiet
restore and no-consent behavior. They do not activate apps or generate input.

The transport tests use actual loopback TCP and WebSocket servers to cover
independent client sessions, queued and in-progress cancellation, timeouts,
reconnects, changed Chrome endpoints, shutdown, and HTTP/Origin restrictions.
Settings tests cover migration, validation, failed-write preservation, and tray
ownership of the relay and focus helpers. Neither suite opens Chrome.

`preview_settings.py` displays the real native settings window using a separate
output file and a fake owner; it does not launch an engine, relay, or login item.
`verify_tray_lifecycle.py` exercises real settings application, helper startup,
observe/off shutdown, and crashed focus-helper recovery in a **new** data
directory. It requires a running Chrome profile but opens no CDP connection:

```bash
python3 tests/mac/verify_tray_lifecycle.py \
  --profile /path/to/disposable-profile --data-dir /path/to/new-lifecycle-results
```

For manual app testing, `YESDEV_DATA_DIR=/path/to/isolated-data` isolates macOS
config, locks and logs, including those of child helpers. It does not redirect
the system login item or Accessibility permissions. `python3 yes_dev_mac.py
--settings` opens the settings window at launch. Native UI checks on 2026-10-01
verified invalid interval/port feedback, Save, reload and Cancel, using isolated
storage; the existing user configuration remained byte-for-byte unchanged.

`tests/mac/measure_quiet_focus.py` measures the focus guard behind Quiet focus
against `tests/mac/focus_flasher.py`, a stand-in app that steals focus with the
same two calls Chrome's widget makes, using WindowServer's own frontmost log as
the judge. Run it with your hands off the keyboard and mouse, since a click or a
keypress is read by the guard as a deliberate switch:

```bash
python3 tests/mac/measure_quiet_focus.py --count 10 --interval 2
```

It reports, per flash, how long the stand-in was frontmost and whether focus
came back to the app that had it. No browser and no prompt are involved; the
real-prompt run is a manual test.

## Early-signal experiment (macOS)

`early_focus_guard.py` and `early_focus_client.py` compare a cooperating
client's ARM/ACK handshake with the normal guard. The comparison scripts are
separate from the tray; their shared guard implementation now powers the relay.
The client waits for acknowledgement before opening its CDP websocket. The
experimental guard can then restore the previous app on the matching browser
activation without waiting for an accessibility read of the consent sheet.
The normal watcher must still recognize and approve the real consent prompt.

This substitutes request correlation for confirmed sheet identity. It is an
opt-in experiment for one in-flight request, not support for arbitrary clients
or a proof that every nearby browser activation belongs to the request. The
first matching activation consumes the request; later activations are logged
but never restored again. That deliberately exposes any later focus steal.

The local control socket lives in a new mode-0700 directory, with a mode-0600
session file carrying a random capability. Browser PID and launch identity,
two-second expiry, prior app identity, input since ARM, and control disconnects
bound the request. These permissions isolate other accounts; they do not defend
against hostile code already running as the same user. Do not publish or commit
the session file. The guard deletes the runtime files on orderly shutdown.

The test client needs `websocat` on PATH. Launch a disposable Chrome profile in
normal consent mode first, then run this with its actual PID and profile path:

```bash
python3 tests/mac/measure_early_focus.py \
  --browser-pid PID --profile /path/to/disposable-profile \
  --out /path/to/new-results-directory --count 3
```

The runner alternates the normal and early guards, uses real
`Browser.getVersion` replies, records foreground/input samples, and collects
WindowServer logs. It waits for a regular non-Chrome app and one second without
input before each request; keep hands off during the comparison. It supervises
its guards, watchers and clients, but does not launch or stop Chrome. Results
must stay outside Git. An early restore alone is insufficient: inspect the later
activation log and final foreground state for Chrome reclaiming focus during
sheet presentation.

Measured on 2026-10-01 with Chrome 154.0.8037.59 and TextEdit as the previous
app, using a disposable profile in normal consent mode:

| WindowServer focus interval | Normal guard | Early signal |
| --- | --- | --- |
| First three alternating pairs | 452, 432, 393 ms | 74, 65, 71 ms |
| Verification pair after shutdown fix | 437 ms | 75 ms |
| Median across four pairs | 434.5 ms | 72.5 ms |

All eight connections returned a real CDP version reply. TextEdit was restored
every time, with no second Chrome activation through approximately 3.4 seconds
after each early restore. No input arrived during these trials. The median
focus interval was 83.3% shorter. These measurements establish feasibility on
this Mac, browser version and previous app; they do not establish broad client
compatibility or eliminate the remaining blink.

The initial batch exposed a shutdown bug: `NSApplication.stop_` did not return
from the idle run loop, so the harness terminated the guard and removed its
runtime directory. The prototype now uses bounded run-loop slices. A separate
SIGTERM check and the verification pair both confirmed orderly exit and runtime
cleanup. Policy tests cover intervening input, expiry, identity changes and
disconnects; deliberate user input was not part of this live comparison.

## Held connection (macOS)

`tests/test_cdp_relay_hold.py` runs the relay with `hold=True` against a
scripted fake Chrome on loopback. The fake answers every call, hands out
session and browser-context ids the way Chrome does, can emit events and late
replies on request, and records everything it receives with the ids it saw.
Fifteen tests cover one prompt for sequential clients, per-client id
renumbering, a departed client's late reply and stale session events never
reaching the next client, the undo list and its order (browser settings off
before sessions detach), auto-attached sessions, sessions the client detached
itself, `Browser.close`, a concurrent client falling back to its own
connection, losing Chrome, a Chrome restart between clients, malformed
messages, a client leaving while Chrome is still being asked, status
reporting, and shutdown. Six deliberate breakages of `cdp_relay.py` were each
caught by the matching test on 2026-10-02. None of this involves real Chrome.

`tests/mac/test_early_focus_events.py` covers the early-focus helper's
event-driven loop with a real run loop and real Unix sockets: servicing
without a timer, two messages in one read, the hold limit, closing a socket's
run-loop source before the socket, and the fast tick running only while a
request is armed. `tests/mac/test_approve_no_raise.py` pins the approval path
to a single press, with no window raised.

## Thin relay integration (macOS)

`relay_mac.py` runs the local relay; the tray starts it when Fast focus is
selected and approvals are active. Its connection guard uses the same policy as
the experiment above. Only upstream handshakes are serialized; established
clients retain separate Chrome sockets and run concurrently.

The automated transport suite checks reconnects, simultaneous clients using the
same CDP message ID, one client leaving without affecting another, cancellation
before and during the upstream handshake, and a changed endpoint after restart.
The native lifecycle check also verified real helper startup from settings,
observe/off shutdown, replacement of a killed focus helper, and runtime cleanup.

`verify_relay_live.py` tests raw WebSocket clients and Playwright's actual
`connect_over_cdp` client against a running disposable Chrome in consent mode.
Install Playwright as a test dependency (`pip install playwright`); it connects
to that existing Chrome and requires no Playwright browser download.

```bash
python3 tests/mac/verify_relay_live.py \
  --profile /tmp/yesdev-test-profile --out /path/to/new-relay-results
```

The optional `--restart-disposable-pid PID` restarts that exact test Chrome while
a client is connected and tests a fresh connection through the same relay. It
requires a `yesdev-*` profile under `/tmp` and verifies the process command before
stopping it. The runner owns only its relay and watcher. It leaves Chrome running
afterwards. The restart requests a background launch and waits for a quiet
non-Chrome app before reconnecting. Focus is checked against the app active at
reconnection, which may differ from the app before Chrome restarted. Use
`--only browser-restart-and-reconnect` with the restart option for a focused
rerun. Results include client responses, foreground/input samples and
WindowServer logs; keep them outside Git.

These live tests must be run with a quiet foreground app. Input behavior needs a
separate run with deliberate input; the automated policy tests alone do not
establish that behavior on a live desktop.

`verify_focus_input.py` provides that separate check. Start with a blank TextEdit
window; press Left once when the runner prints ARMED. It verifies an actual input
timestamp after ARM, logged cancellation, a successful CDP response, and zero
focus restores. The test uses a five-second expiry to allow operator input;
the production expiry remains two seconds. Chrome must become foreground with
no automatic restore; a subsequent app switch is allowed only if there is new
input evidence. `--allow-other-foreground` permits any regular non-Chrome app
before ARM. The runner writes `armed.json` after acknowledgement so a UI driver
can time a keypress into a blank TextEdit window; the test still requires an
observed system input timestamp, not merely a successful keypress tool call.

```bash
python3 tests/mac/verify_focus_input.py --browser-pid PID \
  --profile /tmp/yesdev-test-profile --out /path/to/new-input-results
```

### Live integration results, 2026-10-01

Chrome 154.0.8037.93 in normal consent mode passed all six relay scenarios:
first raw connection, raw reconnect, two clients with identical message IDs
and one leaving, Playwright connection, Playwright reconnect, and Chrome
restart/reconnect through the same relay. Each scenario required a real CDP
reply and return to the app active before its connection. The separate live
input check observed input 297.81 ms after ARM, logged cancellation, obtained a
real CDP reply, and recorded zero restores; Chrome remained in front. It used
actual desktop input while another app was active, without a manual Ready step.

The first five quiet scenarios contained six upstream connections. Their
WindowServer foreground intervals were 107, 5, 5, 43, 6 and 27 ms (median 16.5
ms), with ChatGPT as the previous app. These are a different browser build and
previous app from the TextEdit comparison above, so they are not a paired
performance comparison. A visible interruption remains possible.

Two initial test assertions were corrected and the affected tests rerun: the
restart check used the app before restart instead of before reconnect, and the
input check incorrectly rejected a later user-driven app switch. Original
failed-run evidence was retained; the corrected restart and input runs passed.
The automated suites also passed all 47 tests, and six real helper lifecycle
checks passed. Native Save, Cancel, validation and reload checks passed with
isolated app storage. These are bounded integration checks, not a long-running
stability soak or a Windows settings validation.

## Everyday workload soak (macOS)

`soak_everyday.py` runs the real tray event loop and its ordinary supervision,
approval counter, settings application and puff notifications with isolated app
storage. It requires an existing disposable `yesdev-*` Chrome profile under
`/tmp`, with normal consent-mode remote debugging already enabled:

```bash
python3 tests/mac/soak_everyday.py --minutes 30 \
  --profile /tmp/yesdev-test-profile --out /path/to/new-everyday-results
```

The default run has ten minutes of mixed work, ten idle minutes, then ten more
minutes of mixed work. It alternates raw, briefly held, three-client,
Playwright, and canceled connections, and keeps one client attached for several
minutes. It also replaces a killed focus helper, restarts the watcher and relay,
restarts only the disposable Chrome, and cycles Observe and Off through the
real settings application method. No manual Ready response is required.

Every five seconds it records per-process resident memory, CPU time, CPU
percentage and numeric file-descriptor counts. Foreground samples contain app
identity and time since input, never document text or actual keys. Results,
app data and WindowServer logs stay outside Git. The runner stops its tray,
helpers and disposable Chrome, checks for surviving helpers/runtime files, and
compares the real user configuration hash before and after. A finite soak can
find growth and recovery problems; it cannot establish day-long stability.

For macOS physical footprint (including memory that may be compressed), start
the optional collector after the soak has begun. It samples the same helper PIDs
every 30 seconds and stops when the test tray exits:

```bash
python3 tests/mac/analyze_everyday.py /path/to/new-everyday-results --observe
```

After the run, omit `--observe` to write `analysis.json`. The analysis compares
one-minute RSS medians and native footprint within each role's longest-lived
process, reports CPU use and file counts, and checks for duplicate focus
restores and unhandled exceptions. Cleanup allows up to ten seconds for the
puff helper's final fade-out; a helper surviving that deadline fails the run.

`probe_watcher_memory.py` runs the real watcher sweep in observe-only mode with
or without `--pool`, using separate output directories. The paired process
with `--pool` creates a fresh `objc.autorelease_pool()` around each sweep;
production source is unchanged. Both record native physical footprint for four
minutes, plus equal-work sweep counts. `probe_puff_memory.py` similarly compares
per-frame pools around the real puff constructor, animation and destruction.
It uses matching random seeds and offscreen test windows, so its test clouds do
not cover the desktop. The puff probe defaults to two minutes. Use each probe
once with `--pool` and once without, keeping results outside Git.

These are diagnosis experiments. They establish whether local autorelease
scopes reduce accumulation; they bypass the production outer loops, so both
variants remain useful after the repair. Validate the actual loops with normal
consent connections, visible notifications, and a repeat soak. PyObjC documents this
use of [autorelease pools for loops that do not reenter an application event
loop](https://pyobjc.readthedocs.io/en/latest/api/module-objc.html#objc.autorelease_pool).

### Thirty-minute result before repair, 2026-10-01

**Functional checks passed; this run exposed native memory accumulation in the
watcher and puff loops.** The run completed 47 workload
scenarios and 64 successful upstream CDP connections, including eight
three-client batches, eight Playwright attaches, eight cancellation/reconnect
checks, a persistent client, a ten-minute idle period, helper recovery, Chrome
restart recovery, and Observe/Off settings cycles. All test helpers, the
disposable browser and control runtimes stopped cleanly. The real user config
hash was unchanged. Production source was not changed during this validation.

There were 39 armed focus requests and 39 restores, with no duplicate restore
for any request. Eighteen connections skipped restoration because of recent
input; six began with Chrome already active. No post-ARM input cancellation
occurred in this soak; that behavior was covered by the earlier separate live
input test. Two approval attempts logged a handled retry around the forced
browser restart; reconnection succeeded. The tray counted 63 verified sheet
closures for 64 CDP connections: Chrome was deliberately terminated during the
watcher's verification of one successful grant.

Physical footprint, sampled with macOS `footprint`, exposed growth that RSS
alone hid. Within the watcher's longest-lived process, footprint rose from
30.50 to 48.03 MiB; during an 8.53-minute interior segment of the idle phase,
it grew 7.78 MiB (0.912 MiB/min). The puff process rose from 41.99 to 58.19 MiB
under repeated notifications, but stayed flat during idle. Tray, relay and
focus-helper footprint remained essentially flat, as did baseline file counts.
There were no unhandled Python exceptions. Raw evidence contains 353 process
samples and 56 physical-footprint samples.

Matched diagnosis probes support adding local Cocoa autorelease scopes:

| Probe | Equal work per variant | Original growth | With local pool | Reduction |
| --- | --- | --- | --- | --- |
| Watcher, four minutes, observe-only | 901 sweeps | 3.234 MiB | 0.453 MiB | 86.0% |
| Puffs, two minutes, offscreen | 38 identical clouds | 7.156 MiB | 0.344 MiB | 95.2% |

Probe growth is measured after the first 30 seconds. These experiments changed
only the test wrapper; they did not establish a production fix. They motivated
a per-sweep `objc.autorelease_pool()` in `Engine.run` and a per-frame scope
covering creation, animation and teardown in `puffs_mac.serve`.

### Memory repair validation, 2026-10-02

The production watcher now drains a local autorelease pool after each sweep,
including when a sweep raises. The puff helper drains one after each frame,
covering native image/window creation, animation, teardown and its bounded
run-loop slice. Objects needed by later frames remain owned by Python or their
native views; the pool only releases temporary Cocoa ownership.

`verify_puff_lifetime.py` runs the production `serve()` loop with ten visible
notifications and EOF shutdown in an isolated process. It checks that each
live native window remains visible and holds a valid image across frame-pool
drains, then verifies all ten windows close:

```bash
python3 tests/mac/verify_puff_lifetime.py --out /path/to/new-puff-results
```

The native test passed 3,474 frame/image checks and closed all ten notifications,
with no errors. A 30-minute run with these two pool scopes passed all 47 workload
cases and 64 connections. Puff footprint changed by -0.03 MiB. Watcher idle
growth fell from 0.912 to 0.117 MiB/min, but continued to accumulate, prompting a
second diagnosis rather than a stability claim.

Native allocation stacks identified copied Accessibility strings left allocated
by PyObjC 12.2.2's generic CFTypeRef output conversion. In that version,
[the output conversion](https://github.com/ronaldoussoren/pyobjc/blob/v12.2.2/pyobjc-core/Modules/objc/libffi_support.m#L4251-L4257)
only balances transferred output ownership when the converted
value passes `PyObjCObject_Check`; its `pyobjc_unicode` string representation
does not. The watcher now reads each attribute using
`AXUIElementCopyMultipleAttributeValues` with a one-item attribute list and
`kAXCopyMultipleAttributeOptionStopOnError`. The resulting array owns its values,
so normal bridge ownership works for strings as well as other attribute types.
This avoids manual releases and dependency-version-specific reference counting.

The original attribute path leaked 3,380 CFStrings in 338 sweeps; the repaired
path leaked zero CFStrings in 329 sweeps and recognized real consent sheets.
The reports still contain small process-initialization allocations, so these
results are specifically evidence against per-read string accumulation.
`probe_watcher_memory.py --pool --legacy-attribute-copy --leaks` reproduces the
old path; omit `--legacy-attribute-copy` to test the repaired read. Set
`MallocStackLogging=1` for allocation stacks. The report uses `--noContent` and
does not include allocation contents.

`verify_attribute_reads.py --browser-pid PID --out /path/to/new-read-results`
compares the old and repaired APIs on a verified disposable Chrome. Fifteen
native checks passed for strings, booleans, arrays, geometry and unavailable
attributes, including value validity after the enclosing pool is drained.
Only attribute names, types and equality/error results are saved. Seven
regressions also cover false/empty values, nested arrays, failed reads and the
distinction between an invalid reference and a temporarily unavailable one.
All 54 automated tests passed. The final 30-minute soak of the complete repair
also passed all 47 workload scenarios and 64 successful CDP connections, with
353 process samples and 60 native-footprint samples.

| Physical-footprint measurement | Before repair | Complete repair |
| --- | --- | --- |
| Watcher idle growth per minute | 0.912 MiB | 0.000 MiB |
| Watcher change within its longest-lived process (about 21 minutes) | +17.53 MiB | 0.00 MiB |
| Puff change across the 30-minute run, after warmup | +16.20 MiB | +0.44 MiB |

The final watcher's stable process began and ended at 28.88 MiB, peaking at
28.94 MiB. The puff comparison began at a warmed-up 39.92 MiB and ended at
40.36 MiB, with a 40.69 MiB peak; measured growth was about 97% lower than before repair.
All five roles had zero footprint growth during the 511.6-second interior idle
interval. The tray grew 1.59 MiB during active work, with no idle growth;
relay/focus changes were small and descriptor counts remained bounded.

There were no unhandled exceptions or failed workload scenarios. As in the
baseline, two handled approval retries occurred around the forced Chrome
restart, and the counter recorded 63 verified closures for 64 successful CDP
connections because Chrome exited during one verification. Six armed focus
requests restored exactly once each; nineteen connections skipped restoration
for recent input and thirty-eight began with Chrome active. Post-ARM input
cancellation was not exercised in this soak; the earlier dedicated test covers
that path.

All owned helpers, the disposable browser and control runtimes stopped cleanly.
The tested production-source hashes matched afterward, the user's configuration
still matched the pre-settings backup exactly, and the regular Chrome remained
running. These results resolve the observed accumulation for this bounded
workload on macOS 26.5 and PyObjC 12.2.2. The final run's installed-app manifest
records Chrome 154.0.8037.97; the earlier relay and pool-only runs recorded
154.0.8037.93. The soak checked live version responses but did not retain their
product strings per connection. These are not day-long endurance results or a
claim that system libraries allocate no memory. The user accepted the completed
validation as sufficient on 2026-10-02 and explicitly declined a full-day trial.
