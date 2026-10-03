import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from settings_model import DEFAULTS, normalize, save_atomic, validate


class SettingsTests(unittest.TestCase):
    def test_legacy_values_migrate_without_losing_unknown_keys(self):
        result = normalize({"notify_on_approve": False, "burst_guard": False, "future_setting": "keep"})
        self.assertEqual(result["notify_style"], "none")
        self.assertEqual(result["burst_limit"], 0)
        self.assertEqual(result["future_setting"], "keep")
        self.assertNotIn("burst_guard", result)

    def test_bad_saved_values_do_not_enable_features(self):
        result = normalize({"relay_enabled": "false", "enabled": [], "poll_ms": -1, "notify_style": []})
        self.assertFalse(result["relay_enabled"])
        self.assertEqual(result["poll_ms"], 250)
        self.assertEqual(result["notify_style"], "puffs")

    def test_invalid_controls_are_rejected_before_save(self):
        for key, value in (("poll_ms", "abc"), ("relay_port", "0"), ("burst_limit", "-1"),
                           ("arm_minutes", "1.5"), ("observe_only", "true")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate({**DEFAULTS, key: value})

    def test_fast_focus_requires_existing_folder_and_quiet_focus(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(validate({**DEFAULTS, "relay_enabled": True, "quiet_focus": True,
                                      "relay_profile": d})["relay_enabled"])
            with self.assertRaises(ValueError):
                validate({**DEFAULTS, "relay_enabled": True, "relay_profile": d})
        with self.assertRaises(ValueError):
            validate({**DEFAULTS, "relay_enabled": True, "quiet_focus": True, "relay_profile": d})

    def test_failed_replace_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            original = b'{"enabled": false, "custom": "preserve exactly"}\n'
            path.write_bytes(original)
            with patch("settings_model.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    save_atomic(path, DEFAULTS)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(d).iterdir()), [path])

    def test_round_trip_preserves_unknown_keys_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            values = {**DEFAULTS, "unknown": {"stay": True}}
            save_atomic(path, values)
            self.assertEqual(json.loads(path.read_text()), values)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_hold_is_off_by_default_and_must_be_a_real_boolean(self):
        self.assertIs(DEFAULTS["relay_hold"], False)
        self.assertFalse(normalize({"relay_hold": "true"})["relay_hold"])
        self.assertTrue(normalize({"relay_hold": True})["relay_hold"])
        with self.assertRaises(ValueError):
            validate({**DEFAULTS, "relay_hold": "yes"})


if __name__ == "__main__":
    unittest.main()
