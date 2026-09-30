# Windows approval and memory validation — 2026-09-30

All 143 checks passed. Both 30-minute watcher memory runs passed their pre-set limits. The tests left the installed app and existing browser profiles unchanged.

Validated code: `618001d29d3ebf8ee8f16799a792259308ab14eb`. Watcher Git blob: `7bf617076daae4221e4537831fc963151b5f580b`.
The JSON report also records the SHA-256 of the tested Windows checkout.

## Counter and browser results

The tests cover process-list parsing, both observed Chinese consent titles, native COM fallback, retries, delayed dismissal, queued prompts that share an HWND, partial log records and log rotation. Pending records hold values only. The tray keeps at most 256 bytes from an unfinished line and counts complete ACTION-level records.

The writer uses shared append access and bounded retries. A confirmation stays pending until its ACTION event is saved. Tests saved 1,000 events while a compatible reader held the file open, verified recovery from an incompatible lock, retained records when rotation was blocked, and prevented console failures from replaying saved events.

Four simultaneous connections were tested in each browser/language/method case: Chrome and Edge, English and zh-CN, normal and forced fallback. All 32 connections reached OPEN, returned `Browser.getVersion`, and produced exactly 32 counter events. One Chrome fallback burst used 14 action attempts for four connections and correctly counted four dialogs.

The real-browser versions were Chrome 154.0.8037.58 and Edge 154.0.4258.37, on Windows 11 with Windows PowerShell 5.1.26100.9168.

## Full-process memory runs

Each run used 10 minutes of idle time followed by 20 minutes of new browser connections, alternating Chrome and Edge at a five-second target interval. The watcher ran at its normal 250 ms scan interval with its 400 MiB ceiling. One run used normal invocation; the other injected a primary-method failure to exercise the native fallback. The test copies used unique mutex names. Both copies retained the normal parent-process lifetime check.

| Method | Successful connections | Counted dialogs | Active early private MiB | Active late private MiB | Sampled peak private MiB | Active slope MiB/min | Active handle change |
|---|---:|---:|---:|---:|---:|---:|---:|
| normal | 240 | 240 | 80.95 | 83.76 | 83.86 | 0.209 | +2 |
| fallback | 240 | 240 | 83.48 | 86.72 | 86.82 | 0.245 | +7 |

All connections succeeded. Each completed connection had one counter event. No idle approval events or watcher restarts occurred.

Memory was sampled every five seconds. The first five minutes of each phase were excluded from trend checks. Early and late private-memory values are two-minute medians within the remaining phase. Limits were set before the runs: at most 16 MiB median growth, at most 0.5 MiB/min fitted growth, and at most 64 additional handles in each measured phase. Both processes also had to remain below the sampled 400 MiB ceiling, retain one PID, and complete at least 100 successful connections.

See [windows-validation-2026-09-30.json](windows-validation-2026-09-30.json) for checks and metrics, and [raw memory samples](windows-memory-samples-2026-09-30.csv) for the time series. The reusable commands and test boundaries are in [tests/README.md](../../tests/README.md).

## Evidence boundary

Preliminary, incomplete runs exposed both an observer sharing problem and a Windows PowerShell `Add-Content` sharing problem. These were reproduced and corrected before the complete runs reported above. Those interrupted runs are not counted as successful memory tests.

These finite runs provide evidence for this workload and duration. They do not establish indefinite stability. The production counter confirms disappearance of the original dialog/button after an approval action; it cannot inspect an unrelated client's protocol session. The browser tests confirm protocol success independently.

The native-control tests use a test UIA provider. Browser tests use the real installed browsers with fresh profiles on separate Windows desktops. A separate desktop isolates UI activity; it is not a VM or a security sandbox. No test disabled browser sandboxing or switched the user's input desktop.
