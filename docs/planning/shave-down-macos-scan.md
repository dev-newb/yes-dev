# Shave down the macOS scan

**Repo:** yes-dev (crimsonsunset fork of dev-newb/yes-dev)
**Branch:** main (direct commit, matching this repo's existing history - no per-change branches here)
**Status:** done, verified against a live prompt
**Effort:** ~1 hour, single file

## Flow state

| Field | Value |
|---|---|
| Gate | 5 (shipped) |
| Ticket | n/a - personal repo, no tracker |
| Branch | main |
| Repos | yes-dev |
| Isolation | in-place |
| QA mode | manual (trigger a real dialog, read the log, `ps`) |
| Gates | all |
| Updated | 2026-09-01 |

## Overview

The macOS engine (`watcher_mac.py`) approves Chrome's "Allow remote debugging?"
sheet correctly - that got fixed this session (verify-after-click, CGEvent
fallback, `24482c4`). What's still wrong is *how it looks* for the dialog:
`find_dialog_hosts()` walks the entire Accessibility tree of every Chrome
process, twelve levels deep, four times a second, forever - whether or not a
dialog is anywhere on screen.

Diagnostics added this session confirm the cost directly:

```
diagnostic AX pid=15453 hosts=0 (155ms)
diagnostic AX pid=15453 hosts=0 (161ms)
```

~150-160ms per Chrome process per sweep. At a 250ms interval that's 60-65% of
one core's wall time spent walking a tree that is, almost always, empty. It
lines up with the 9-17% CPU and the multi-GB RSS growth over a long run: the
walk isn't just slow, it's descending into the AX-exposed DOM of every loaded
tab, which is enormous on modern pages, on every single poll.

This is not a "add a cheaper detection layer" problem. It's a "the walk is
scoped way too broad" problem. The fix is to scope it correctly, not to bolt
on a pre-filter.

**This is not a rewrite.** The click/verify/CGEvent-fallback logic from this
session's earlier fix does not change. The tray, the menu, the burst guard,
the puffs, the config schema - none of it changes. One function gets scoped
down to where the dialog can actually be.

## Decisions

| Decision | Rationale |
|---|---|
| Scope `find_dialog_hosts()` to `app.AXWindows` + each window's `AXSheets` **and** `AXChildren`, one level deep, dropping the depth-12 recursion | First cut checked `AXSheets` only, per the module docstring's confirmed shape. Verifying against a real live prompt showed why that wasn't enough: `AXSheets` came back with 14 entries, every one an invalid/stale ref (`AXError -25202`), while the actual sheet was sitting in plain `AXChildren` instead. Chrome is inconsistent about which attribute exposes it, which is exactly why the original `_children()` helper always checked both. The fix keeps both, but only one level deep - a window's direct `AXChildren` is a handful of top-level regions (toolbar, tab strip, the sheet if present), not the page's own AX-exposed DOM, which only shows up if you recurse *into* the web-area child. This still turns the sweep from O(entire accessible DOM) into O(window count) |
| Port the shape from `watcher.ps1`, not a new native API | Windows already solved this: `RootElement.Children` (top-level windows) → exactly two more `Children` levels, matching by name/class at each step, before ever calling `FindAll(Descendants)`. No UIA-notification rewrite there either - just a walk that stops where the dialog structurally has to be. macOS gets the same shape, in AX terms |
| No `CGWindowList` pre-check, no `AXObserver` event-driven rewrite | Investigated both mid-session. Once the walk is correctly scoped the per-sweep cost drops to a handful of AX attribute reads (near-zero), which is the actual goal - adding a second detection subsystem on top would be solving an already-solved problem |
| `_sheet_still_up()` needs no separate fix | It calls `find_dialog_hosts()` too, so it inherits the scoped walk for free |
| `find_approve_button()` / `_button_labels()` keep their existing recursive `_children()` walk | That recursion is bounded to one already-matched dialog's small subtree (a few buttons deep), not the page. It was never the cost driver - the diagnostics timing is entirely inside `find_dialog_hosts()` |
| Drop `MAX_TREE_DEPTH` | Only consumer was the depth-12 guard in the old `find_dialog_hosts()`. Dead once that function no longer recurses arbitrarily |
| No macOS engine-level mutex (Windows has `Global\YesDevEngine`, macOS has none) | Real gap, but it's a reliability nice-to-have, not a perf fix - the tray's own `flock` single-instance guard plus `--exit-with-parent` already cover the practical failure mode. Out of scope for a shave-down; revisit separately if an orphan engine is ever actually observed |
| Keep `--diagnostics` as a hidden flag, default off | Cheap to keep, and it's exactly what proves the fix: the same `diagnostic AX pid=... hosts=... (Xms)` line reads single-digit ms after, not 150+ |
| Diagnostics line now also logs `windows=N candidates=N` | The live-prompt debugging above needed an ad-hoc probe script to see *why* detection failed - counts alone wouldn't have shown the AXSheets-vs-AXChildren split, but would have at least shown something was being read. Cheap enough to leave in permanently rather than write another throwaway probe next time something's off |

## Scope

**In:**
- Rewrite `find_dialog_hosts()` to the two-level windows-then-sheets scan
- Remove the now-dead `MAX_TREE_DEPTH` constant and its recursion
- Update the module docstring / inline comments that describe the old full-tree
  walk and the menu-item/heading false-positive guard (structurally impossible
  once the walk never touches the menu bar or descends past a matched sheet)
- Verify with `--diagnostics`: scan time per pid should fall from ~150-160ms to
  low single digits
- Verify against a real "Allow remote debugging?" prompt: still gets clicked,
  `[ACTION] APPROVED via ...` still lands in the log
- Verify sustained resource use (CPU, RSS) over a run against this session's
  baseline (9-17% CPU, ~1.3-2GB RSS and climbing)
- Commit and push to `crimsonsunset/yes-dev`

**Out:**
- `watcher.ps1` (Windows build) - already does the shallow walk, nothing to
  change
- Any CGWindowList / AXObserver / notification-based detection - superseded by
  the scoping fix, see Decisions
- Engine-level single-instance mutex - deferred, see Decisions
- Tray UI, menu options, puffs, burst guard, config schema - unchanged by design

## Architecture

Before (`find_dialog_hosts`, recursive, unbounded within 12 levels):

```
app
 └─ walks AXChildren + AXSheets + AXWindows of every element
     └─ ...down to 12 levels, across every tab's entire AX-exposed page
```

After:

```
app.AXWindows                       # top-level windows only, one AX read
  ├─ window itself: title match?    # standalone AXDialog case
  └─ window.AXSheets + window.AXChildren   # one level deep, one AX read each
        └─ candidate: title match?  # covers both the documented AXSheets
                                     # case and the AXChildren case a live
                                     # prompt actually showed
```

No recursion into any candidate's own children. A window's direct
`AXChildren` is toolbar/tab-strip/sheet-level, not the page - the DOM only
shows up if you recurse *into* the web-area child, which this never does.
`find_approve_button()` still recurses, but only inside a host this scan
already found - a handful of nodes, not the whole app.

## Files to modify

| File | Change |
|---|---|
| [`watcher_mac.py`](../../watcher_mac.py) | Rewrite `find_dialog_hosts()`; drop `MAX_TREE_DEPTH`; update docstring/comments describing the walk |

That's the entire diff. Everything else - `platform_mac.py`, `yes_dev_mac.py`,
`puffs.py`, the config schema, the log contract - is untouched.

## Phasing

### Phase 1: Rewrite the scan (~20 min) - done

- Replaced `find_dialog_hosts()` body with the windows→(sheets+children) scan
- Removed `MAX_TREE_DEPTH`
- Updated the module docstring's description of the walk and the
  false-positive guard (menu item / heading) to match the new, narrower reality

**Outcome (measured):** idle scan time (no dialog open) fell from
~150-160ms/pid to 1-4ms/pid, confirmed via `--diagnostics` against the live
daemon.

### Phase 2: Live verification (~20 min) - done

- Restarted the engine (killed the old process; the tray relaunches it,
  `--exit-with-parent`-supervised, picking up the new code)
- Triggered a real consent prompt against a freshly-launched Chrome, via
  `jsg-chrome-mcp`'s `chrome-devtools_list_pages` forcing a new CDP client
  attach
- First cut of the scan (AXSheets-only) missed the live dialog entirely -
  found via a live probe that `AXSheets` returned 14 invalid/stale refs while
  the real sheet was in `AXChildren`. Fixed by checking both (see Decisions),
  re-verified: `hosts=1`, approved, `_sheet_still_up()` confirmed gone
- Sampled CPU/RSS over ~80s idle post-fix

**Outcome (measured):**
- Dialog detected and auto-approved (`[ACTION] APPROVED via CGEvent`),
  confirmed gone via the same scan that verifies every click
- CPU: 0.4-0.7% sustained, down from 9-18%
- RSS: ~61MB, moving by <200KB over 80s, down from climbing into the GBs over
  hours

### Phase 3: Ship (~5 min) - done

- Committed to `main`, pushed to `crimsonsunset/yes-dev`

**Outcome:** fork's `main` has the fix; upstream `dev-newb/yes-dev` untouched.

## Key files referenced

| File | Note |
|---|---|
| [`watcher_mac.py`](../../watcher_mac.py) | The file being changed; also documents the confirmed dialog AX shape in its module docstring |
| [`watcher.ps1`](../../watcher.ps1) | Windows engine - source of the two-level walk pattern being ported |
| [`platform_mac.py`](../../platform_mac.py) | Paths, trust check, single-instance lock - unchanged, just context |
| [`docs/mac/ax_probe.py`](../mac/ax_probe.py) | Live AX tree probe that originally shaped the dialog's documented tree |
| `~/Library/Application Support/YesDev/yes-dev.log` | Where the before/after diagnostic timing numbers come from |

## Related documentation

- This session's earlier fix: verify-after-click + CGEvent fallback (`24482c4`)
- No Jira/ticket tracker for this repo - personal fork, tracked here only
