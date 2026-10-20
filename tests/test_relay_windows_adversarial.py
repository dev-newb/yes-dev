"""Adversarial helper tests. All process and file fixtures belong to the test."""
from pathlib import Path
import subprocess
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import relay_windows as windows


class Child:
    def __init__(self, command, pid):
        self.command, self.pid = command, pid
        self.returncode = None
        self.actions = []
        self.wait_error = None

    def option(self, name):
        return self.command[self.command.index(name) + 1]

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.actions.append("wait")
        if self.wait_error is not None:
            raise self.wait_error
        if Path(self.option("--stop-path")).exists():
            self.returncode = 0
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode

    def terminate(self):
        self.actions.append("terminate")
        self.returncode = 1

    def kill(self):
        self.actions.append("kill")
        self.returncode = 2


class ManagerAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.children = []
        self.manager = windows.RelayManager(ROOT, self.base / "data")
        self.popen = patch.object(windows.subprocess, "Popen", side_effect=self.launch).start()
        self.addCleanup(patch.stopall)
        patch.object(windows, "parent_creation", return_value=1).start()
        self.addCleanup(self.cleanup_manager)

    def launch(self, command, **kwargs):
        child = Child(command, 500 + len(self.children))
        self.children.append(child)
        return child

    def settle(self):
        deadline = time.monotonic() + 3
        while True:
            with self.manager._lock:
                threads = [thread for thread in (self.manager._worker, self.manager._sampler) if thread is not None]
            if not threads:
                return
            for thread in threads:
                thread.join(.01)
            if time.monotonic() > deadline:
                self.fail("Owned lifecycle worker did not finish")

    def cleanup_manager(self):
        for child in self.children:
            child.wait_error = None
        self.manager.stop("test cleanup")
        self.settle()

    def enable(self, port=9333):
        self.manager.sync(True, str(self.base / "User Data"), port)
        self.settle()

    def publish(self, **state):
        child = self.children[-1]
        path = Path(child.option("--status-path"))
        windows.write_json(path, dict(run_id=child.option("--run-id"), pid=child.pid, **state))
        self.enable()

    def test_unexpected_wait_error_still_terminates_owned_child(self):
        self.enable()
        child = self.children[0]
        child.wait_error = RuntimeError("wait handle fault")
        self.manager.stop("disable")
        self.settle()
        self.assertIsNotNone(child.returncode, "Disable left the owned helper alive")
        self.assertIn("terminate", child.actions)
        self.assertIsNone(self.manager._process)

    def test_slow_status_read_does_not_block_disable(self):
        self.enable()
        self.manager._started -= self.manager.START_TIMEOUT + 1
        entered, release, disabled = threading.Event(), threading.Event(), threading.Event()
        original = windows.read_json
        def read(path):
            if Path(path).name == "status.json":
                entered.set()
                release.wait(2)
            return original(path)
        with patch.object(windows, "read_json", side_effect=read):
            start = time.monotonic()
            self.manager.sync(True, str(self.base / "User Data"), 9333)
            self.assertLess(time.monotonic() - start, .1, "sync() waited for status file I/O")
            self.assertTrue(entered.wait(1))
            stopper = threading.Thread(target=lambda: (self.manager.stop("disable during I/O"), disabled.set()))
            stopper.start()
            try:
                self.assertTrue(disabled.wait(.2), "Status file I/O held the manager lock")
            finally:
                release.set()
                stopper.join(3)
                self.settle()
        self.assertIsNotNone(self.children[0].returncode)

    def test_slow_status_text_read_does_not_block_disable(self):
        self.enable()
        self.publish(state="listening", held=True)
        with patch.object(windows, "read_json", side_effect=AssertionError("Public status must use cached state")):
            start = time.monotonic()
            self.assertEqual(self.manager.status_text(), "Relay: held")
            self.assertLess(time.monotonic() - start, .1)

    def test_blocked_status_read_does_not_keep_disabled_child_alive(self):
        self.enable()
        child = self.children[0]
        entered, release = threading.Event(), threading.Event()
        original = windows.read_json
        def read(path):
            if Path(path).name == "status.json":
                entered.set()
                release.wait()
            return original(path)
        with patch.object(windows, "read_json", side_effect=read):
            self.manager.sync(True, str(self.base / "User Data"), 9333)
            self.assertTrue(entered.wait(1))
            try:
                self.manager.stop("disable during blocked status read")
                deadline = time.monotonic() + .5
                while child.returncode is None and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertIsNotNone(child.returncode, "Status read prevented owned child shutdown")
                self.assertFalse(release.is_set(), "Test released the read before shutdown")
            finally:
                release.set()
                self.settle()

    def test_old_status_read_cannot_replace_new_run_cache(self):
        self.enable()
        self.publish(state="listening", held=True)
        old_path = Path(self.children[0].option("--status-path"))
        original = windows.read_json
        old_state = original(old_path)
        entered_old, release_old = threading.Event(), threading.Event()
        entered_new, release_new = threading.Event(), threading.Event()
        def read(path):
            if Path(path) == old_path:
                entered_old.set()
                release_old.wait()
                return old_state
            entered_new.set()
            release_new.wait()
            return original(path)
        with patch.object(windows, "read_json", side_effect=read):
            self.manager.sync(True, str(self.base / "User Data"), 9333)
            self.assertTrue(entered_old.wait(1))
            try:
                self.manager.sync(True, str(self.base / "User Data"), 9444)
                deadline = time.monotonic() + 1
                while len(self.children) < 2 and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertEqual(len(self.children), 2)
                self.assertIsNotNone(self.children[0].returncode)
                release_old.set()
                self.assertTrue(entered_new.wait(1))
                with self.manager._lock:
                    self.assertEqual(self.manager._state, {}, "An old run changed the new run cache")
            finally:
                release_old.set()
                release_new.set()
                self.settle()

    def test_default_off_starts_no_lifecycle_or_status_thread(self):
        for _ in range(30):
            self.manager.sync(False, "", 9333)
            self.assertEqual(self.manager.status_text(), "Relay: off")
        self.assertIsNone(self.manager._worker)
        self.assertIsNone(self.manager._sampler)
        self.assertEqual(self.children, [])

    def test_stop_file_denial_still_terminates_only_owned_child(self):
        self.enable()
        with patch.object(windows, "write_json", side_effect=PermissionError("denied")):
            self.manager.stop("disable")
            self.settle()
        self.assertIn("terminate", self.children[0].actions)
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.manager.status_text(), "Relay: off")

    def test_slow_launch_rapid_config_aba_then_disable(self):
        entered, release = threading.Event(), threading.Event()
        def launch(command, **kwargs):
            entered.set()
            release.wait(2)
            return self.launch(command, **kwargs)
        self.popen.side_effect = launch
        self.manager.sync(True, str(self.base), 9333)
        self.assertTrue(entered.wait(1))
        for enabled, port in ((False, 9333), (True, 9444), (True, 9333), (False, 9333)):
            self.manager.sync(enabled, str(self.base), port)
        release.set()
        self.settle()
        self.assertEqual(len(self.children), 1)
        self.assertIsNotNone(self.children[0].returncode)
        self.assertIsNone(self.manager._process)

    def test_permanent_launch_failure_is_bounded_and_disable_clears_it(self):
        self.popen.side_effect = OSError("permanent launch denial")
        self.enable()
        for _ in range(30):
            self.enable()
        self.assertEqual(self.popen.call_count, 1)
        self.manager.stop("disable")
        self.settle()
        self.assertEqual(self.manager.status_text(), "Relay: off")
        self.manager.sync(True, str(self.base), 9444)
        self.settle()
        self.assertEqual(self.popen.call_count, 2)

    def test_partial_oversize_foreign_and_invalid_status_are_not_healthy(self):
        self.enable()
        path = Path(self.children[0].option("--status-path"))
        for raw in ('{"state":', 'x' * 65537, '[]', '{"run_id":"foreign","pid":500,"state":"listening","held":true}'):
            with self.subTest(raw=raw[:40]):
                path.write_text(raw, encoding="utf-8")
                self.enable()
                self.assertEqual(self.manager.status_text(), "Relay: starting")

    def test_terminal_status_does_not_report_healthy_from_old_fields(self):
        self.enable()
        self.publish(state="stopped", held=True, held_state="held", connections=1)
        self.assertNotIn(self.manager.status_text(), ("Relay: held", "Relay: client active", "Relay: listening"))


class ParentOpenAdversarialTests(unittest.TestCase):
    def test_dead_parent_open_is_rejected_without_closing_invalid_handle(self):
        native = Mock()
        native.kernel.OpenProcess.return_value = 0
        with self.assertRaisesRegex(RuntimeError, "Cannot open"):
            windows.ParentProcess(10, 123, native=native)
        native.kernel.CloseHandle.assert_not_called()
        native.creation.assert_not_called()

    def test_parent_creation_exception_closes_owned_handle(self):
        native = Mock()
        native.kernel.OpenProcess.return_value = 71
        native.creation.side_effect = OSError("creation query failed")
        with self.assertRaisesRegex(OSError, "creation query failed"):
            windows.ParentProcess(10, 123, native=native)
        native.kernel.CloseHandle.assert_called_once_with(71)


class ChildInterpreterTests(unittest.TestCase):
    def test_venv_redirector_is_avoided_and_dependency_path_is_private(self):
        original = dict(os.environ)
        with patch.object(windows.sys, 'platform', 'win32'), \
                patch.object(windows.sys, 'prefix', r'C:\qa\venv'), \
                patch.object(windows.sys, 'base_prefix', r'C:\Python'), \
                patch.object(windows.sys, '_base_executable', r'C:\Python\python.exe'), \
                patch.dict(os.environ, {'PYTHONPATH': 'prior-path'}):
            executable, environment = windows.child_python()
            self.assertEqual(executable, r'C:\Python\python.exe')
            self.assertEqual(environment['PYTHONPATH'].split(os.pathsep),
                             [r'C:\qa\venv\Lib\site-packages', 'prior-path'])
            self.assertEqual(os.environ['PYTHONPATH'], 'prior-path')
        self.assertEqual(dict(os.environ), original)

    def test_full_interpreter_is_kept_without_dependency_path_changes(self):
        with patch.object(windows.sys, 'prefix', 'same'), \
                patch.object(windows.sys, 'base_prefix', 'same'):
            executable, environment = windows.child_python()
        self.assertEqual(executable, sys.executable)
        self.assertEqual(environment, dict(os.environ))


if __name__ == "__main__":
    unittest.main()
