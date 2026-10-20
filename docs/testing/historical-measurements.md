# Historical measurements

[Home](../../README.md) | [Architecture](../architecture.md)

These are records from earlier versions and named test conditions. They are not
current reliability guarantees for version 1.5.0 or new browser versions.

## Earlier Windows field record

The earlier source build recorded 454 approvals over eight days, with one failed
click (99.8% in that record). The old README also recorded recovery after reboot.
The Chrome 151 example compared about 35 seconds of human waiting for four
parallel connections with 2.4–4.4 seconds for automatic approval. It is not a
benchmark of the new portable build.

Version 1.0.0's idle Accessibility scan leaked native memory: the earlier field
incident reached 51.5 GB private memory after ten days. It could also leave an
engine alive after the tray stopped. Those repairs were recorded in version
1.1.0; see the [changelog](../../CHANGELOG.md). Current Windows release evidence
is in the [2026-09-30](windows-validation-2026-09-30.md),
[stall recovery](windows-stall-recovery-2026-10-17.md), and
[independent startup](windows-independent-start-2026-10-18.md) reports.

## macOS focus comparisons

The earlier README reported four real Chrome 154 comparisons: Quiet focus had a
434.5 ms median interruption, and the early-signal prototype had a 72.5 ms median.
The older 13 ms result used a stand-in window and did not include Chrome's blocking
sheet lookup. These figures must not be presented as a universal current latency.
See [test notes](../../tests/README.md) and the [changelog](../../CHANGELOG.md) for
later native validation and its limits. An activation can still send input to
Chrome or switch Spaces during that interval.
