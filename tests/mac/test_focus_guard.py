"""Guard decision regressions; no app activation or real input is generated."""
from __future__ import annotations

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import focus_guard_mac as guard_module


class InputClock:
    """Input events and slow AX reads share a deterministic monotonic clock."""

    def __init__(self, input_times=()):
        self.now = 100.0
        self.input_times = [90.0, *input_times]

    def advance(self, seconds):
        self.now += seconds

    def idle_age(self, *_args):
        return self.now - max(t for t in self.input_times if t <= self.now)


class FocusGuardInputTests(unittest.TestCase):
    def setUp(self):
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.clock = InputClock()
        self.guard = object.__new__(guard_module.Guard)
        self.guard.opts = SimpleNamespace(quiet_s=0.5, require_sheet=True)
        self.guard.watch_pids = {101}
        self.guard.parent_pid = -1
        self.guard.current = {
            "pid": 202, "name": "Editor", "bundle": "test.editor",
            "policy": guard_module.NSApplicationActivationPolicyRegular,
            "at": 99.0,
        }
        self.guard.restores = 0
        self.guard.skips = 0
        self.guard.bridge = object()
        self.guard.log = Mock()
        self.guard._restore = Mock(return_value="AXFrontmost")
        self.guard._sheet_visible = Mock(side_effect=self.sheet_after_delay)
        self.scan_seconds = 0.4
        app = SimpleNamespace(
            processIdentifier=lambda: 101,
            localizedName=lambda: "Browser",
            bundleIdentifier=lambda: "test.browser",
            activationPolicy=lambda: guard_module.NSApplicationActivationPolicyRegular,
        )
        self.note = SimpleNamespace(userInfo=lambda: {
            guard_module.NSWorkspaceApplicationKey: app,
        })
        self.timer = patches.enter_context(patch.object(guard_module, "NSTimer"))
        patches.enter_context(patch.object(guard_module.time, "monotonic", lambda: self.clock.now))
        patches.enter_context(patch.object(guard_module.time, "sleep", self.clock.advance))
        patches.enter_context(patch.object(
            guard_module, "CGEventSourceSecondsSinceLastEventType", self.clock.idle_age))

    def sheet_after_delay(self, _pid):
        self.clock.advance(self.scan_seconds)
        return True

    def assert_cancelled(self):
        self.guard._restore.assert_not_called()
        self.assertEqual(self.guard.restores, 0)
        self.assertEqual(self.guard.skips, 1)
        self.timer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_.assert_not_called()

    def test_click_during_sheet_scan_cancels_restore(self):
        self.clock.input_times.append(100.33)
        self.guard.on_activated(self.note)
        self.assert_cancelled()

    def test_input_older_than_quiet_threshold_still_cancels_pending_restore(self):
        self.scan_seconds = 1.4
        self.clock.input_times.append(100.1)
        self.guard.on_activated(self.note)
        self.assertGreater(self.clock.idle_age(), self.guard.opts.quiet_s)
        self.assert_cancelled()

    def test_input_between_sheet_retries_cancels_restore(self):
        self.clock.input_times.append(100.02)
        self.guard._sheet_visible.side_effect = [False, True]
        self.guard.on_activated(self.note)
        self.assertEqual(self.guard._sheet_visible.call_count, 2)
        self.assert_cancelled()

    def test_quiet_sheet_scan_restores_previous_app(self):
        self.guard.on_activated(self.note)
        self.guard._restore.assert_called_once()
        self.assertEqual(self.guard._restore.call_args.args[0]["pid"], 202)
        self.assertEqual(self.guard.restores, 1)
        self.assertEqual(self.guard.skips, 0)
        self.timer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_.assert_called_once()

    def test_input_before_activation_does_not_cancel_a_long_quiet_scan(self):
        self.clock.input_times.append(99.0)
        self.scan_seconds = 2.0
        self.guard.on_activated(self.note)
        self.guard._restore.assert_called_once()
        self.assertEqual(self.guard.skips, 0)

    def test_recent_input_at_activation_skips_sheet_scan(self):
        self.clock.input_times.append(99.9)
        self.guard.on_activated(self.note)
        self.guard._sheet_visible.assert_not_called()
        self.assert_cancelled()

    def test_no_consent_sheet_does_not_restore(self):
        self.guard._sheet_visible.side_effect = None
        self.guard._sheet_visible.return_value = False
        self.guard.on_activated(self.note)
        self.assertEqual(self.guard._sheet_visible.call_count, guard_module.SHEET_TRIES)
        self.assert_cancelled()


if __name__ == "__main__":
    unittest.main()
