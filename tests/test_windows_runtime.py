"""Use no-UI fixtures for launch independence and exit diagnostics."""
import ctypes
from ctypes import wintypes
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from windows_runtime import Native, independent, read_json


class LaunchPolicyTests(unittest.TestCase):
    def test_desktop_parent_with_a_nonterminating_job_is_allowed(self):
        self.assertTrue(independent(dict(desktop_pid=10, parent_pid=10, parent_image="explorer.exe",
                                         in_job=True, current_job_limit_flags=0x800)))

    def test_job_breakaway_alone_does_not_prove_independence(self):
        for parent in (10, 20):
            self.assertFalse(independent(dict(desktop_pid=10, parent_pid=parent, parent_image="explorer.exe",
                                              in_job=True, current_job_limit_flags=0x2000)))

    def test_unknown_job_or_wrong_parent_requires_the_desktop_broker(self):
        for context in (
            dict(desktop_pid=10, parent_pid=20, parent_image="python.exe", in_job=False, current_job_limit_flags=None),
            dict(desktop_pid=10, parent_pid=10, parent_image="explorer.exe", in_job=True, current_job_limit_flags=None),
            dict(desktop_pid=0, parent_pid=0, parent_image=None, in_job=False, current_job_limit_flags=None),
        ):
            self.assertFalse(independent(context))


@unittest.skipUnless(sys.platform == "win32", "Windows process and Explorer APIs")
class RuntimeProcessTests(unittest.TestCase):
    def setUp(self):
        from watcher_supervisor import Windows, WorkerJob
        self.temp = tempfile.TemporaryDirectory(prefix="yesdev-runtime-test-")
        self.root = Path(self.temp.name)
        self.data = self.root / "diagnostics"
        self.job = WorkerJob(Windows().kernel)
        self.native = Native()
        self.children = []
        self.launchers = []

    def tearDown(self):
        (self.root / "stop").touch()
        self.job.close()
        for pid, creation in self.children:
            if self.native.alive(pid, creation):
                self.terminate(pid, creation, 99)
        for process in self.launchers:
            process.wait(timeout=5)
        # The exit watcher keeps its process handle until the fixture exits.
        for state in self.states():
            deadline = time.monotonic() + 5
            while self.native.alive(state.get("exit_watcher_pid", 0), state.get("exit_watcher_creation", 0)):
                if time.monotonic() > deadline:
                    self.fail("Test exit watcher did not stop")
                time.sleep(0.05)
        result = self._outcome.result
        failed = any(test is self for test, _ in result.failures + result.errors)
        if failed and os.environ.get("YESDEV_TEST_ARTIFACTS"):
            destination = Path(os.environ["YESDEV_TEST_ARTIFACTS"]) / self._testMethodName
            shutil.copytree(self.root, destination, dirs_exist_ok=True)
        self.temp.cleanup()

    def states(self):
        return [read_json(path) for path in self.data.glob("sessions/*/state.json")]

    def wait_state(self, predicate):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            for state in self.states():
                if state.get("pid") and (state["pid"], state["creation_time"]) not in self.children:
                    self.children.append((state["pid"], state["creation_time"]))
                if predicate(state):
                    return state
            time.sleep(0.05)
        self.fail(f"Timed out: {self.states()}")

    def launch(self, mode="wait", hold_launcher=False):
        entry = self.root / "entry.py"
        entry.write_text(f'''
from pathlib import Path
import os, sys, time, threading
sys.path.insert(0, {str(REPO)!r})
from windows_runtime import run
ROOT = Path({str(self.root)!r})
if __name__ == "__main__":
    code = run(Path(__file__), data_dir=ROOT / "diagnostics")
    if {hold_launcher!r} and "--yesdev-brokered" not in sys.argv:
        (ROOT / "launcher-waiting").touch()
        while not (ROOT / "stop").exists(): time.sleep(0.05)
    raise SystemExit(code)
if {mode!r} == "import_error":
    raise ImportError("fixture import failure")
def main(lifecycle=None):
    lifecycle.ready()
    if {mode!r} == "thread_error":
        def failed_thread(): raise RuntimeError("fixture thread failure")
        thread = threading.Thread(target=failed_thread, name="fixture thread")
        thread.start()
        thread.join()
    if {mode!r} == "exception": raise RuntimeError("fixture main failure")
    if {mode!r} == "native_exit": os._exit(23)
    while not (ROOT / "stop").exists(): time.sleep(0.05)
    lifecycle.request_exit("fixture_quit")
    return 0
''', encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(entry)], creationflags=subprocess.CREATE_NO_WINDOW,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.launchers.append(process)
        self.job.add(process)
        return process

    def terminate(self, pid, creation, code):
        k = self.native.kernel
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        handle = k.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        self.assertTrue(handle)
        try:
            self.assertEqual(self.native.creation(handle), creation)
            self.assertTrue(k.TerminateProcess(handle, code))
            self.assertEqual(k.WaitForSingleObject(handle, 5000), 0)
        finally:
            k.CloseHandle(handle)

    def events(self, state):
        path = self.data / "sessions" / state["session_id"] / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_tray_survives_termination_of_the_callers_job(self):
        launcher = self.launch(hold_launcher=True)
        state = self.wait_state(lambda row: row.get("status") == "ready")
        self.assertTrue(independent(state))
        self.assertNotEqual(state["pid"], launcher.pid)
        deadline = time.monotonic() + 5
        while not (self.root / "launcher-waiting").exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.assertIsNone(launcher.poll())
        self.job.close()
        launcher.wait(timeout=5)
        self.assertFalse((self.root / "stop").exists())
        self.assertTrue(self.native.alive(state["pid"], state["creation_time"]))
        heartbeat = state["last_heartbeat"]
        self.wait_state(lambda row: row.get("last_heartbeat") != heartbeat and row.get("status") == "ready")
        (self.root / "stop").touch()
        ended = self.wait_state(lambda row: row.get("actual_exit_code") == 0)
        self.assertEqual(ended["exit_reason"], "fixture_quit")
        self.assertIn("process_exit", [event["event"] for event in self.events(ended)])

    def test_forced_termination_records_actual_code_without_claiming_a_cause(self):
        self.launch()
        state = self.wait_state(lambda row: row.get("status") == "ready")
        self.terminate(state["pid"], state["creation_time"], 77)
        ended = self.wait_state(lambda row: row.get("actual_exit_code") == 77)
        self.assertEqual(ended["status"], "exit_without_cleanup")
        self.assertEqual(ended["actual_exit_hex"], "0x0000004D")
        self.assertIn("unknown", ended["exit_reason"])

    def test_exit_bypassing_python_cleanup_is_observed(self):
        self.launch("native_exit")
        ended = self.wait_state(lambda row: row.get("actual_exit_code") == 23)
        self.assertEqual(ended["status"], "exit_without_cleanup")

    def test_main_exception_has_a_traceback_and_exit_code(self):
        self.launch("exception")
        ended = self.wait_state(lambda row: row.get("actual_exit_code") == 1)
        self.assertEqual(ended["exit_reason"], "unhandled_exception")
        error = next(event for event in self.events(ended) if event["event"] == "unhandled_exception")
        self.assertIn("fixture main failure", error["traceback"])

    def test_import_failure_is_captured_before_the_tray_loads(self):
        self.launch("import_error")
        ended = self.wait_state(lambda row: row.get("actual_exit_code") == 1)
        error = next(event for event in self.events(ended) if event["event"] == "unhandled_exception")
        self.assertEqual(error["exception_type"], "ImportError")

    def test_background_thread_exception_is_logged_without_guessing_an_exit(self):
        self.launch("thread_error")
        state = self.wait_state(lambda row: row.get("status") == "ready")
        deadline = time.monotonic() + 5
        while not any(event["event"] == "thread_exception" for event in self.events(state)):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.assertTrue(self.native.alive(state["pid"], state["creation_time"]))

    def test_previous_missing_exit_is_marked_unknown_on_next_start(self):
        old = self.data / "sessions" / "old"
        old.mkdir(parents=True)
        (old / "state.json").write_text(json.dumps(dict(session_id="old", status="ready", pid=0,
                                                       creation_time=0, last_heartbeat="prior-run")))
        self.launch()
        self.wait_state(lambda row: row.get("status") == "ready" and row.get("session_id") != "old")
        self.assertEqual(read_json(old / "state.json")["status"], "exit_unrecorded")


@unittest.skipUnless(sys.platform == "win32", "Windows tray message handlers")
class TrayLifecycleTests(unittest.TestCase):
    def test_missing_bootstrap_module_writes_a_startup_error(self):
        with tempfile.TemporaryDirectory(prefix="yesdev-missing-runtime-") as folder:
            root = Path(folder)
            entry = root / "yes_dev.pyw"
            entry.write_bytes((REPO / "yes_dev.pyw").read_bytes())
            env = dict(os.environ, LOCALAPPDATA=folder, PYTHONPATH="")
            result = subprocess.run([sys.executable, str(entry)], cwd=folder, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("windows_runtime", (root / "YesDev" / "bootstrap-error.log").read_text(encoding="utf-8"))

    def test_session_end_and_tray_stop_are_recorded_without_sending_os_messages(self):
        loader = importlib.machinery.SourceFileLoader("tray_lifecycle_test", str(REPO / "yes_dev.pyw"))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        life = Mock()

        class Icon:
            def __init__(self, *args):
                self._message_handlers = {0x040A: lambda *_: None}

            def stop(self):
                self._message_handlers[0x040A](0, 0)

            def run(self, setup=None):
                setup(self)
                self._message_handlers[0x0011](1, 0)
                self._message_handlers[0x0016](0, 0)
                self._message_handlers[0x0016](1, 0)

        with patch.object(module.pystray, "Icon", Icon), patch.object(module.threading, "Thread"):
            app = module.YesDev.__new__(module.YesDev)
            app.cfg = Mock()
            app.make_image = Mock()
            app.build_menu = Mock()
            app.monitor = Mock()
            app.lifecycle = life
            app.run()
        life.ready.assert_called_once_with()
        self.assertIn((("windows_session_end",), {}), life.request_exit.call_args_list)
        self.assertIn((("tray_loop_stopped",), {}), life.request_exit.call_args_list)


if __name__ == "__main__":
    unittest.main()
