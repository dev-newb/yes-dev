"""The approval path presses the button and nothing else: no window is raised."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import watcher_mac as watcher
from ApplicationServices import kAXErrorCannotComplete


class ApproveWithoutRaiseTests(unittest.TestCase):
    def setUp(self):
        self.engine = object.__new__(watcher.Engine)
        self.engine.log = Mock()
        self.engine._log_press_state = Mock()
        self.engine._key_approve = Mock(return_value="Space")
        self.actions = []
        patcher = patch.object(watcher, "AXUIElementPerformAction", side_effect=self.perform)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch.object(watcher.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.press_error = watcher.kAXErrorSuccess

    def perform(self, element, action):
        self.actions.append((element, action))
        return self.press_error

    def test_press_only_targets_the_button(self):
        self.engine._sheet_still_up = Mock(return_value=False)
        how = self.engine._approve(101, "sheet", "allow")
        self.assertEqual(how, "AXPress")
        self.assertEqual(self.actions, [("allow", "AXPress")])

    def test_no_raise_before_the_keyboard_fallback(self):
        self.engine._sheet_still_up = Mock(return_value=True)
        how = self.engine._approve(101, "sheet", "allow")
        self.assertEqual(how, "Space")
        self.assertEqual(self.actions, [("allow", "AXPress")])
        self.engine._key_approve.assert_called_once_with(101, "sheet", "allow", "AXPress")

    def test_dismissal_without_a_successful_press_is_not_credited_to_a_press(self):
        self.press_error = kAXErrorCannotComplete
        self.engine._sheet_still_up = Mock(return_value=False)
        self.assertEqual(self.engine._approve(101, "sheet", "allow"), "AlreadyDismissed")

    def test_failed_press_label_reaches_the_keyboard_path(self):
        self.press_error = kAXErrorCannotComplete
        self.engine._sheet_still_up = Mock(return_value=True)
        self.engine._approve(101, "sheet", "allow")
        self.engine._key_approve.assert_called_once_with(101, "sheet", "allow", "AlreadyDismissed")


if __name__ == "__main__":
    unittest.main()
