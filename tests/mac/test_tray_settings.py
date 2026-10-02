"""Settings application and helper ownership without starting native UI."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import yes_dev_mac as tray
from settings_model import DEFAULTS


class TraySettingsTests(unittest.TestCase):
    def setUp(self):
        self.app = tray.YesDev.__new__(tray.YesDev)
        self.app.cfg = dict(DEFAULTS)
        self.app.proc = self.app.guard = self.app.relay = None
        self.app._relay_retry_at = 0
        self.app.paused_reason = self.app.resume_at = self.app.allow_until = None
        self.app.disarm_at = None
        self.app.recent = []
        self.app.refresh = Mock()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "config.json"
        for patcher in (patch.object(tray, "CONFIG_PATH", self.path),
                        patch.object(tray.platform_mac, "autostart_enabled", return_value=False),
                        patch.object(tray, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_save_failure_does_not_change_config_or_helpers(self):
        original = dict(self.app.cfg)
        with patch.object(tray, "save_atomic", side_effect=OSError("Disk full")), \
             patch.object(self.app, "stop_engine") as stopped:
            with self.assertRaises(OSError):
                self.app.apply_settings({**original, "enabled": False}, False)
        self.assertEqual(self.app.cfg, original)
        stopped.assert_not_called()

    def test_off_stops_all_helpers(self):
        watcher, guard, relay = Mock(), Mock(), Mock()
        for process in (watcher, guard, relay):
            process.poll.return_value = None
        self.app.proc, self.app.guard, self.app.relay = watcher, guard, relay
        self.app.apply_settings({**self.app.cfg, "enabled": False}, False)
        for process in (watcher, guard, relay):
            process.terminate.assert_called_once()
        self.assertIsNone(self.app.proc)
        self.assertIsNone(self.app.guard)
        self.assertIsNone(self.app.relay)

    def test_relay_and_normal_guard_never_run_together(self):
        self.app.cfg.update(quiet_focus=True, relay_enabled=True)
        self.app.proc = Mock()
        self.app.proc.poll.return_value = None
        self.app.guard = Mock()
        self.app.guard.poll.return_value = None
        old_guard = self.app.guard
        with patch.object(self.app, "start_relay") as start:
            self.app.sync_guard()
        old_guard.terminate.assert_called_once()
        start.assert_called_once()
        self.assertIsNone(self.app.guard)

    def test_observe_mode_does_not_restore_focus_or_start_relay(self):
        self.app.cfg.update(quiet_focus=True, relay_enabled=True, observe_only=True)
        self.app.proc = Mock()
        self.app.proc.poll.return_value = None
        with patch.object(self.app, "start_relay") as relay, patch.object(self.app, "start_guard") as guard:
            self.app.sync_guard()
        relay.assert_not_called()
        guard.assert_not_called()

    def test_cosmetic_change_does_not_restart_engine_or_extend_timer(self):
        self.app.proc = Mock()
        self.app.proc.poll.return_value = None
        timer = object()
        self.app.disarm_at = timer
        with patch.object(self.app, "start_engine") as start, patch.object(self.app, "stop_engine") as stop:
            self.app.apply_settings({**self.app.cfg, "notify_style": "none"}, False)
        start.assert_not_called()
        stop.assert_not_called()
        self.assertIs(self.app.disarm_at, timer)

    def test_hold_is_passed_to_the_relay_only_when_set(self):
        for hold in (False, True):
            with self.subTest(hold=hold):
                self.app.relay = None
                self.app._relay_retry_at = 0
                self.app.cfg.update(relay_hold=hold)
                with patch.object(tray.subprocess, "Popen") as popen:
                    self.app.start_relay()
                self.assertEqual("--hold" in popen.call_args.args[0], hold)

    def test_changing_hold_restarts_the_relay(self):
        relay = Mock()
        relay.poll.return_value = None
        self.app.relay = relay
        with patch.object(self.app, "sync_guard"):
            self.app.apply_settings({**self.app.cfg, "relay_hold": True}, False)
        relay.terminate.assert_called_once()
        self.assertIsNone(self.app.relay)


if __name__ == "__main__":
    unittest.main()
