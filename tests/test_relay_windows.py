"""Owned helper lifecycle tests. No browser, tray, or installed app data."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import relay_windows as windows


class ParentIdentityTests(unittest.TestCase):
    def native(self, creation=100, wait=258):
        native = Mock()
        native.kernel.OpenProcess.return_value = 71
        native.kernel.WaitForSingleObject.return_value = wait
        native.creation.return_value = creation
        return native

    def test_retains_original_handle_and_observes_parent_exit(self):
        native = self.native()
        parent = windows.ParentProcess(51, 100, native=native)
        self.assertTrue(parent.alive())
        native.kernel.WaitForSingleObject.return_value = 0
        self.assertFalse(parent.alive())
        self.assertEqual(native.kernel.OpenProcess.call_count, 1)
        parent.close()
        native.kernel.CloseHandle.assert_called_once_with(71)

    def test_reused_pid_or_exited_parent_is_refused(self):
        for native in (self.native(creation=101), self.native(wait=0)):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                windows.ParentProcess(51, 100, native=native)
            native.kernel.CloseHandle.assert_called_once_with(71)


class FakeChild:
    def __init__(self, command, pid=77):
        self.pid = pid
        self.command = command
        self.returncode = None
        self.actions = []
        self.graceful = True
        self.term_works = True

    def option(self, name):
        return self.command[self.command.index(name) + 1]

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.actions.append(("wait", timeout))
        if self.graceful and Path(self.option("--stop-path")).exists():
            self.returncode = 0
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode

    def terminate(self):
        self.actions.append("terminate")
        if self.term_works:
            self.returncode = 1

    def kill(self):
        self.actions.append("kill")
        self.returncode = 2


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.profile = str(self.base / "User Data")
        self.children = []
        self.popen = patch.object(windows.subprocess, "Popen", side_effect=self.launch).start()
        self.addCleanup(patch.stopall)
        patch.object(windows, "parent_creation", return_value=123456).start()
        self.manager = windows.RelayManager(ROOT, self.base / "data")
        self.addCleanup(self.stop)

    def launch(self, command, **kwargs):
        child = FakeChild(command, 77 + len(self.children))
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
                thread.join(.05)
            if time.monotonic() > deadline:
                self.fail("Relay lifecycle worker did not finish")

    def stop(self):
        self.manager.stop("test end")
        self.settle()

    def enable(self, port=9333):
        self.manager.sync(True, self.profile, port)
        self.settle()

    def publish(self, **state):
        child = self.children[-1]
        windows.write_json(Path(child.option("--status-path")), dict(
            run_id=child.option("--run-id"), pid=child.pid, **state))
        self.enable()

    def test_unchanged_sync_keeps_one_child_and_status_is_run_bound(self):
        self.assertEqual(self.manager.status_text(), "Relay: off")
        self.enable()
        self.enable()
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.manager.status_text(), "Relay: starting")
        self.publish(state="listening", held_state="empty", held=False)
        self.assertEqual(self.manager.status_text(), "Relay: listening")
        self.publish(state="listening", held_state="held", held=True)
        self.assertEqual(self.manager.status_text(), "Relay: held")
        self.publish(state="listening", connections=1)
        self.assertEqual(self.manager.status_text(), "Relay: client active")
        path = Path(self.children[0].option("--status-path"))
        windows.write_json(path, dict(run_id="someone-else", pid=77, state="listening"))
        self.enable()
        self.assertEqual(self.manager.status_text(), "Relay: starting")

    def test_config_restart_stops_only_owned_child_and_uses_new_run(self):
        self.enable()
        first = self.children[0]
        self.enable(9444)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(len(self.children), 2)
        self.assertNotEqual(first.option("--run-id"), self.children[1].option("--run-id"))
        request = windows.read_json(first.option("--stop-path"))
        self.assertEqual(request["reason"], "settings changed")
        self.assertEqual(request["run_id"], first.option("--run-id"))
        self.assertNotIn("terminate", first.actions)

    def test_stop_escalates_only_its_popen_and_never_spawns_again(self):
        self.enable()
        child = self.children[0]
        child.graceful = child.term_works = False
        self.manager.stop("app quit")
        self.settle()
        self.assertIn("terminate", child.actions)
        self.assertIn("kill", child.actions)
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.manager.status_text(), "Relay: off")

    def test_failed_launch_has_backoff_and_can_retry(self):
        self.popen.side_effect = OSError("test launch denied")
        self.enable()
        self.assertIn("test launch denied", self.manager.status_text())
        attempts = self.popen.call_count
        self.enable()
        self.assertEqual(self.popen.call_count, attempts)
        self.manager._retry_at = 0
        self.popen.side_effect = self.launch
        self.enable()
        self.assertEqual(len(self.children), 1)

    def test_failed_child_reports_status_and_does_not_restart_each_tick(self):
        self.enable()
        self.publish(state="error", last_error="port already in use")
        self.children[0].returncode = 1
        self.enable()
        self.assertIn("port already in use", self.manager.status_text())
        for _ in range(3):
            self.enable()
        self.assertEqual(len(self.children), 1)

    def test_invalid_settings_stop_owned_child(self):
        self.enable()
        self.manager.sync(True, self.profile, 80)
        self.settle()
        self.assertEqual(self.children[0].returncode, 0)
        self.assertIn("1024 to 65535", self.manager.status_text())

    def test_startup_timeout_stops_owned_child_and_uses_retry_backoff(self):
        self.enable()
        self.manager._started -= self.manager.START_TIMEOUT + 1
        self.enable()
        self.assertEqual(self.children[0].returncode, 0)
        self.assertIn("startup timed out", self.manager.status_text())
        self.enable()
        self.assertEqual(len(self.children), 1)

    def test_sync_does_not_wait_for_slow_launch_and_stop_is_not_lost(self):
        entered, release = threading.Event(), threading.Event()
        def launch(command, **kwargs):
            entered.set()
            release.wait(2)
            return self.launch(command, **kwargs)
        self.popen.side_effect = launch
        self.manager.sync(True, self.profile, 9333)
        self.assertTrue(entered.wait(1))
        start = time.monotonic()
        self.manager.stop("stop during launch")
        self.assertLess(time.monotonic() - start, .1)
        release.set()
        self.settle()
        self.assertEqual(self.children[0].returncode, 0)

    def test_never_enabled_does_not_import_transport(self):
        for _ in range(3):
            self.manager.sync(False, "", 9333)
        self.settle()
        self.assertEqual(len(self.children), 0)
        self.assertIsNone(self.manager._worker)


class StatusTests(unittest.TestCase):
    def test_import_works_without_optional_transport(self):
        command = [sys.executable, "-E", "-S", "-c", "import relay_windows; assert 'cdp_relay' not in __import__('sys').modules; assert 'websockets' not in __import__('sys').modules"]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_stop_file_is_scoped_to_one_run_and_status_is_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "status.json"
            status = windows.Status(path, "one")
            status(state="error", last_error="test error")
            value = windows.read_json(path)
            self.assertEqual(value["run_id"], "one")
            self.assertEqual(value["pid"], os.getpid())
            self.assertEqual(windows.stop_reason(path, "two"), None)
            self.assertEqual(windows.stop_reason(path, "one"), "owned stop request")
            self.assertEqual(list(Path(temp).glob("*.tmp")), [])


@unittest.skipUnless(sys.platform == "win32", "Windows parent process handles")
class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.profile = self.base / "User Data"
        self.profile.mkdir()
        self.children = []

    async def asyncTearDown(self):
        for child, stop, run_id in self.children:
            windows.write_json(stop, dict(run_id=run_id, reason="test cleanup"))
            for _ in range(100):
                if child.poll() is not None:
                    break
                await asyncio.sleep(.05)
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
        self.temp.cleanup()

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def launch(self, port=None, parent_pid=None, creation=None, no_site=False):
        runtime = self.base / f"run-{len(self.children)}"
        runtime.mkdir()
        run_id = runtime.name
        status, stop = runtime / "status.json", runtime / "stop.json"
        # Ignore PYTHONPATH as well as site packages in the missing-dependency
        # fixture, even when the test runner uses a separate QA dependency path.
        command = [sys.executable] + (["-E", "-S"] if no_site else []) + [str(ROOT / "relay_windows.py"),
            "--profile", str(self.profile), "--port", str(port or self.free_port()),
            "--status-path", str(status), "--log-path", str(runtime / "relay.log"),
            "--parent-pid", str(parent_pid or os.getpid()),
            "--parent-creation", str(creation if creation is not None else windows.parent_creation()),
            "--stop-path", str(stop), "--run-id", run_id]
        with (runtime / "stderr.log").open("wb") as stderr:
            child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append((child, stop, run_id))
        return child, status, stop, run_id

    async def wait_status(self, path, predicate):
        for _ in range(200):
            value = windows.read_json(path)
            if predicate(value):
                return value
            await asyncio.sleep(.03)
        self.fail(f"Host status timed out: {windows.read_json(path)}")

    async def test_missing_optional_transport_writes_error_status_and_stderr(self):
        child, status, _, _ = self.launch(no_site=True)
        value = await self.wait_status(status, lambda s: s.get("state") == "error")
        self.assertIn("websockets", value["last_error"])
        await asyncio.to_thread(child.wait, timeout=3)
        self.assertIn("websockets", (status.parent / "stderr.log").read_text())
        self.assertIn("Relay startup", (status.parent / "relay.log").read_text())

    async def test_parent_creation_mismatch_is_reported_before_transport_import(self):
        child, status, _, _ = self.launch(creation=1)
        value = await self.wait_status(status, lambda s: s.get("state") == "error")
        self.assertIn("identity changed", value["last_error"])

    @unittest.skipUnless(importlib.util.find_spec("websockets"), "optional transport not installed")
    async def test_port_collision_does_not_close_existing_listener(self):
        with socket.socket() as owner:
            owner.bind(("127.0.0.1", 0))
            owner.listen()
            child, status, _, _ = self.launch(port=owner.getsockname()[1])
            value = await self.wait_status(status, lambda s: s.get("state") == "error")
            self.assertTrue(value["last_error"])
            self.assertGreater(owner.fileno(), 0)
            probe = socket.create_connection(owner.getsockname(), timeout=1)
            probe.close()

    @unittest.skipUnless(importlib.util.find_spec("websockets"), "optional transport not installed")
    async def test_fake_upstream_is_demand_open_held_and_closed_on_owned_stop(self):
        from websockets.asyncio.client import connect
        from websockets.asyncio.server import serve
        from websockets.exceptions import ConnectionClosed
        opened, closed = [], []
        async def upstream(ws):
            opened.append(ws)
            try:
                async for raw in ws:
                    data = json.loads(raw)
                    await ws.send(json.dumps(dict(id=data["id"], result={})))
            except ConnectionClosed:
                pass
            finally:
                closed.append(ws)
        server = await serve(upstream, "127.0.0.1", 0)
        try:
            upstream_port = server.sockets[0].getsockname()[1]
            (self.profile / "DevToolsActivePort").write_text(f"{upstream_port}\n/devtools/browser/fake\n")
            child, status, stop, run_id = self.launch()
            state = await self.wait_status(status, lambda s: s.get("state") == "listening")
            await asyncio.sleep(.25)
            self.assertEqual(opened, [])
            uri = state["url"].replace("http:", "ws:") + "/devtools/browser/yesdev"
            for _ in range(2):
                async with connect(uri, proxy=None) as client:
                    await client.send(json.dumps(dict(id=1, method="Browser.getVersion")))
                    self.assertEqual(json.loads(await asyncio.wait_for(client.recv(), 2))["id"], 1)
                await asyncio.sleep(.1)
            self.assertEqual(len(opened), 1)
            windows.write_json(stop, dict(run_id="another-run", reason="wrong stop"))
            await asyncio.sleep(.3)
            self.assertIsNone(child.poll())
            windows.write_json(stop, dict(run_id=run_id, reason="test owned stop"))
            final = await self.wait_status(status, lambda s: s.get("state") == "stopped" and s.get("exit_reason"))
            self.assertEqual(final["exit_reason"], "test owned stop")
            self.assertEqual(len(closed), 1)
        finally:
            server.close()
            await server.wait_closed()

    @unittest.skipUnless(importlib.util.find_spec("websockets"), "optional transport not installed")
    async def test_child_exits_when_bound_parent_exits(self):
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            from windows_runtime import Native
            creation = Native().creation(parent._handle)
            child, status, _, _ = self.launch(parent_pid=parent.pid, creation=creation)
            await self.wait_status(status, lambda s: s.get("state") == "listening")
            parent.terminate()
            await asyncio.to_thread(parent.wait, timeout=3)
            final = await self.wait_status(status, lambda s: s.get("state") == "stopped")
            self.assertEqual(final["exit_reason"], "parent exited")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
