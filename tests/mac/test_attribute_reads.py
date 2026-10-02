"""Preserve approval safety when reading AX values through an owned array."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import watcher_mac as watcher
from ApplicationServices import kAXErrorAttributeUnsupported, kAXErrorCannotComplete


class AttributeReadTests(unittest.TestCase):
    def test_false_zero_and_empty_string_are_values(self):
        for value in (False, 0, ""):
            with self.subTest(value=value), patch.object(
                    watcher, "AXUIElementCopyMultipleAttributeValues", return_value=(0, [value])) as read:
                error, actual = watcher._copy_attribute("element", "AXValue")
                self.assertEqual(error, 0)
                self.assertIs(actual, value)
                read.assert_called_once_with("element", ["AXValue"],
                                             watcher.kAXCopyMultipleAttributeOptionStopOnError, None)

    def test_nested_array_is_not_flattened(self):
        children = [object(), object()]
        with patch.object(watcher, "AXUIElementCopyMultipleAttributeValues", return_value=(0, [children])):
            self.assertIs(watcher._attr("element", "AXChildren"), children)

    def test_failed_read_never_exposes_partial_value(self):
        with patch.object(watcher, "AXUIElementCopyMultipleAttributeValues",
                          return_value=(kAXErrorAttributeUnsupported, ["Allow"])):
            self.assertEqual(watcher._copy_attribute("element", "AXTitle"),
                             (kAXErrorAttributeUnsupported, None))
            self.assertIsNone(watcher._attr("element", "AXTitle"))

    def test_missing_result_is_safe(self):
        for values in (None, []):
            with self.subTest(values=values), patch.object(
                    watcher, "AXUIElementCopyMultipleAttributeValues", return_value=(0, values)):
                self.assertIsNone(watcher._attr("element", "AXTitle"))

    def test_unavailable_or_raising_read_does_not_prove_dismissal(self):
        with patch.object(watcher, "AXUIElementCopyMultipleAttributeValues",
                          return_value=(kAXErrorCannotComplete, None)):
            self.assertIsNone(watcher._ref_alive("element"))
        with patch.object(watcher, "AXUIElementCopyMultipleAttributeValues", side_effect=RuntimeError):
            self.assertIsNone(watcher._ref_alive("element"))
            self.assertIsNone(watcher._attr("element", "AXRole"))

    def test_invalid_reference_proves_dismissal(self):
        with patch.object(watcher, "AXUIElementCopyMultipleAttributeValues",
                          return_value=(watcher.kAXErrorInvalidUIElement, None)):
            self.assertIs(watcher._ref_alive("element"), False)

    def test_successful_role_read_is_alive(self):
        with patch.object(watcher, "AXUIElementCopyMultipleAttributeValues", return_value=(0, ["AXButton"])):
            self.assertIs(watcher._ref_alive("element"), True)


if __name__ == "__main__":
    unittest.main()
