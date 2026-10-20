"""Manager work-count and recovery checks using only owned temporary files."""
from collections import Counter
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_relay_windows import FakeChild, ROOT, windows


class ManagerPerformanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.profile = str(self.base / "User Data")
        self.children = []
        self.manager = windows.RelayManager(ROOT, self.base / "data")
        popen = patch.object(windows.subprocess, "Popen", side_effect=self.launch)
        self.popen = popen.start()
        self.addCleanup(popen.stop)
        parent = patch.object(windows, "parent_creation", return_value=123)
        parent.start()
        self.addCleanup(parent.stop)
        self.addCleanup(self.stop)

    def launch(self, command, **kwargs):
        child = FakeChild(command, 700 + len(self.children))
        self.children.append(child)
        return child

    def settle(self):
        deadline = time.monotonic() + 3
        while True:
            with self.manager._lock:
                threads = [t for t in (self.manager._worker, self.manager._sampler) if t]
            if not threads:
                return
            for thread in threads:
                thread.join(.01)
            if time.monotonic() > deadline:
                self.fail("Manager work did not finish")

    def stop(self):
        self.manager.stop("test end")
        self.settle()

    def sync(self, enabled=True, port=9333):
        self.manager.sync(enabled, self.profile, port)
        self.settle()

    def publish(self, state="listening", **extra):
        child = self.children[-1]
        windows.write_json(Path(child.option("--status-path")), dict(
            run_id=child.option("--run-id"), pid=child.pid, state=state, **extra))
        self.sync()

    def test_healthy_sync_samples_without_lifecycle_thread(self):
        self.sync()
        self.publish()
        original = threading.Thread
        counts = Counter()
        def counted(*args, **kwargs):
            counts[kwargs.get("name")] += 1
            return original(*args, **kwargs)
        with patch.object(windows.threading, "Thread", side_effect=counted):
            for _ in range(40):
                self.sync()
        self.assertEqual(counts, {"YesDev relay status": 40})
        self.assertEqual(len(self.children), 1)

    def test_default_off_starts_no_threads_or_files(self):
        with patch.object(windows.threading, "Thread") as thread:
            for _ in range(500):
                self.sync(False)
            thread.assert_not_called()
        self.assertFalse(self.manager.data_dir.exists())

    def test_lost_status_after_ready_does_not_apply_startup_timeout(self):
        self.sync()
        self.publish()
        child = self.children[0]
        path = Path(child.option("--status-path"))
        self.manager._started -= self.manager.START_TIMEOUT + 1
        for raw in ('{"state":', '{}', '{"run_id":"foreign","state":"listening"}'):
            path.write_text(raw, encoding="utf-8")
            self.sync()
            self.sync()  # The lifecycle decision now sees the missing cached state.
            self.assertIsNone(child.returncode)
            self.assertEqual(len(self.children), 1)
        path.unlink()
        self.sync()
        self.sync()
        self.assertIsNone(child.returncode)
        self.publish(held=True)
        self.assertEqual(self.manager.status_text(), "Relay: held")

    def test_ready_child_exit_still_detected_with_missing_status(self):
        self.sync()
        self.publish()
        child = self.children[0]
        Path(child.option("--status-path")).unlink()
        child.returncode = 9
        self.sync()
        self.assertIsNone(self.manager._process)
        self.assertIn("exited", self.manager.status_text())
        self.assertEqual(len(self.children), 1)

    def test_config_change_resets_startup_readiness(self):
        self.sync()
        self.publish()
        self.sync(port=9444)
        self.assertFalse(self.manager._ready)
        self.manager._started -= self.manager.START_TIMEOUT + 1
        self.sync(port=9444)
        self.assertIsNotNone(self.children[-1].returncode)
        self.assertIn("startup timed out", self.manager.status_text())

    def test_retry_delay_resets_only_after_sustained_healthy_samples(self):
        self.sync()
        self.manager._delay = self.manager.RETRY_MAX
        self.publish()
        self.assertEqual(self.manager._delay, self.manager.RETRY_MAX)
        self.manager._healthy_since -= self.manager.RETRY_RESET_AFTER + 1
        self.publish(last_error="temporary connection failure")
        self.assertIsNone(self.manager._healthy_since)
        self.assertEqual(self.manager._delay, self.manager.RETRY_MAX)
        self.publish()
        self.assertEqual(self.manager._delay, self.manager.RETRY_MAX)
        self.manager._healthy_since -= self.manager.RETRY_RESET_AFTER + 1
        self.publish()
        self.assertEqual(self.manager._delay, self.manager.RETRY_MIN)
        before = time.monotonic()
        self.children[-1].returncode = 1
        self.sync()
        self.assertGreaterEqual(self.manager._retry_at, before + self.manager.RETRY_MIN)
        self.assertLess(self.manager._retry_at, before + self.manager.RETRY_MIN + 1)

    def test_missing_status_restarts_healthy_recovery_window(self):
        self.sync()
        self.manager._delay = self.manager.RETRY_MAX
        self.publish()
        self.manager._healthy_since -= self.manager.RETRY_RESET_AFTER + 1
        Path(self.children[0].option("--status-path")).unlink()
        self.sync()
        self.assertIsNone(self.manager._healthy_since)
        self.publish()
        self.assertEqual(self.manager._delay, self.manager.RETRY_MAX)

    def test_permanent_failure_does_not_add_workers_or_runs_during_backoff(self):
        self.popen.side_effect = OSError("launch denied")
        self.sync()
        runs = self.manager.data_dir / "relay-runs"
        first_runs = list(runs.iterdir())
        with patch.object(windows.threading, "Thread") as thread:
            for _ in range(500):
                self.sync()
            thread.assert_not_called()
        self.assertEqual(list(runs.iterdir()), first_runs)
        self.assertEqual(self.popen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
