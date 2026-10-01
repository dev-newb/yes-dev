"""Focus decisions only: no macOS UI actions or permission changes."""
import unittest

from quiet_focus import FocusHistory


class FocusHistoryTests(unittest.TestCase):
    def setUp(self):
        self.history = FocusHistory(['chrome'])
        self.history.observe(1, 'editor', True, 10, 0)

    def chrome(self, now=11, last_intent=0):
        self.history.observe(2, 'chrome', True, now, last_intent)

    def test_recent_prompt_restores_previous_app_once(self):
        self.chrome()
        self.assertEqual(self.history.take_target(2), 1)
        self.assertIsNone(self.history.take_target(2))

    def test_chrome_already_foreground_has_no_restore_target(self):
        history = FocusHistory(['chrome'])
        history.observe(2, 'chrome', True, 10, 0)
        self.assertIsNone(history.take_target(2))

    def test_click_or_switch_shortcut_before_activation_cancels(self):
        self.chrome(last_intent=10.9)
        self.assertIsNone(self.history.take_target(2))

    def test_interaction_after_activation_cancels(self):
        self.chrome()
        self.chrome(now=11.2, last_intent=11.1)
        self.assertIsNone(self.history.take_target(2))

    def test_new_app_selection_cancels(self):
        self.chrome()
        self.history.observe(3, 'terminal', True, 11.1, 0)
        self.assertIsNone(self.history.take_target(2))

    def test_old_activation_is_not_used_for_later_prompt(self):
        self.chrome()
        self.chrome(now=13.1)
        self.assertIsNone(self.history.take_target(2))

    def test_prompt_in_another_browser_does_not_move_focus(self):
        self.chrome()
        self.assertIsNone(self.history.take_target(99))

    def test_transient_system_app_is_not_a_target(self):
        self.history.observe(3, 'dock', False, 10.5, 0)
        self.chrome()
        self.assertIsNone(self.history.take_target(2))

    def test_no_prompt_means_no_restore_request(self):
        self.chrome()
        self.chrome(now=14)
        self.assertIsNone(self.history.take_target(2))

    def test_queued_browser_activation_can_restore_again(self):
        self.chrome()
        self.assertEqual(self.history.take_target(2), 1)
        self.history.observe(1, 'editor', True, 11.1, 0)
        self.chrome(now=12)
        self.assertEqual(self.history.take_target(2), 1)


if __name__ == '__main__':
    unittest.main()
