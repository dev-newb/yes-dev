"""The early-focus helper sleeps until something happens: run-loop wiring checks.

These use a real run loop and real Unix sockets, but no browser, no app
activation and no input. The helper's old loop woke 200 times a second to poll
its control socket; these pin the event-driven replacement.
"""
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import early_focus_guard_mac as early
from focus_protocol import AppIdentity, ArmGate
from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop


def run_until(condition, seconds=1.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        NSRunLoop.currentRunLoop().runMode_beforeDate_(
            NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.05))
    return condition()


class Hooks:
    """The two watch hooks, wired to real run-loop sources as the helper does."""

    def __init__(self, server_ref):
        self.server_ref = server_ref
        self.opened, self.closing = [], []
        self.client_watch = None

    def client_opened(self, sock):
        self.opened.append(sock.fileno())
        self.client_watch = early.FdWatch(sock.fileno(), lambda: self.server_ref().ready())

    def client_closing(self, sock):
        self.closing.append(sock.fileno())
        if self.client_watch is not None:
            self.client_watch.close()
            self.client_watch = None


class RunLoopControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ydev-", dir="/tmp")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "c.sock"
        self.messages, self.drops = [], []
        self.hooks = Hooks(lambda: self.server)
        self.server = early.ControlServer(self.path, "t", self.handle,
                                          lambda: self.drops.append(True), watch=self.hooks)
        self.listen_watch = early.FdWatch(self.server.listener.fileno(), self.server.ready)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.listen_watch.close()
        self.server.close()

    def handle(self, message):
        self.messages.append(message["op"])
        return {"request_id": message["request_id"], "status": "ok"}

    def frame(self, op, request_id=None):
        return (json.dumps({"op": op, "token": "t", "request_id": request_id or str(uuid.uuid4())})
                + "\n").encode()

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1)
        client.connect(str(self.path))
        self.addCleanup(client.close)
        return client

    def test_accept_and_message_are_serviced_by_the_run_loop_alone(self):
        client = self.connect()
        self.assertTrue(run_until(lambda: self.hooks.opened), "accept was not serviced")
        client.sendall(self.frame("ARM"))
        self.assertTrue(run_until(lambda: self.messages == ["ARM"]), "message was not serviced")
        self.assertEqual(json.loads(client.recv(4096))["status"], "ok")

    def test_two_lines_in_one_read_are_both_handled(self):
        client = self.connect()
        request_id = str(uuid.uuid4())
        client.sendall(self.frame("ARM", request_id) + self.frame("DONE", request_id))
        self.assertTrue(run_until(lambda: self.messages == ["ARM", "DONE"]))

    def test_disconnect_closes_the_watch_before_the_socket(self):
        client = self.connect()
        self.assertTrue(run_until(lambda: self.hooks.opened))
        client.close()
        self.assertTrue(run_until(lambda: self.drops == [True]))
        self.assertEqual(self.hooks.closing, self.hooks.opened)
        self.assertIsNone(self.hooks.client_watch)

    def test_idle_run_loop_does_no_work(self):
        calls = []
        real = self.server.poll
        self.server.poll = lambda: (calls.append(1), real())
        for _ in range(3):
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.1))
        self.assertEqual(calls, [])

    def test_hold_limit_drops_a_silent_client(self):
        self.connect()
        self.assertTrue(run_until(lambda: self.hooks.opened))
        with patch.object(early.time, "monotonic", return_value=time.monotonic() + early.HOLD_LIMIT_S + 1):
            self.server.ready()
        self.assertEqual(self.drops, [True])
        self.assertIsNone(self.hooks.client_watch)


class PendingTimerTests(unittest.TestCase):
    """The fast tick exists only while a request is armed."""

    def setUp(self):
        self.guard = object.__new__(early.EarlyGuard)
        self.guard.gate = ArmGate(AppIdentity(101, 1.0))
        self.guard.bridge = object()
        self.guard.pending_timer = None
        self.guard.log = Mock()
        self.timers = []
        patcher = patch.object(early, "NSTimer")
        nstimer = patcher.start()
        self.addCleanup(patcher.stop)
        def schedule(*args):
            timer = Mock()
            self.timers.append((args, timer))
            return timer
        nstimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_.side_effect = schedule

    def arm(self, now=10.0):
        return self.guard.gate.arm("r", AppIdentity(202, 2.0), AppIdentity(101, 1.0), now, 5.0)

    def test_no_timer_while_idle(self):
        self.guard.sync_pending_timer()
        self.assertEqual(self.timers, [])

    def test_arming_starts_one_timer_and_finishing_stops_it(self):
        self.assertEqual(self.arm(), "armed")
        self.guard.sync_pending_timer()
        self.guard.sync_pending_timer()
        self.assertEqual(len(self.timers), 1)
        args, timer = self.timers[0]
        self.assertEqual(args[0], early.PENDING_TICK_S)
        self.assertEqual(args[2], "pendingTick:")
        self.guard.gate.finish("r")
        self.guard.sync_pending_timer()
        timer.invalidate.assert_called_once_with()
        self.assertIsNone(self.guard.pending_timer)

    def test_expiry_on_the_tick_stops_the_timer(self):
        self.arm(now=10.0)
        self.guard.sync_pending_timer()
        _, timer = self.timers[0]
        with patch.object(early.time, "monotonic", return_value=10.0 + 2.1), \
             patch.object(early, "idle_age", return_value=60.0):
            self.guard.pending_tick()
        self.assertIsNone(self.guard.gate.pending)
        timer.invalidate.assert_called_once_with()
        self.guard.log.assert_called_with("request expired", "AUDIT")

    def test_input_after_arm_on_the_tick_cancels_and_stops_the_timer(self):
        self.arm(now=10.0)
        self.guard.sync_pending_timer()
        _, timer = self.timers[0]
        with patch.object(early.time, "monotonic", return_value=10.3), \
             patch.object(early, "idle_age", return_value=0.1):
            self.guard.pending_tick()
        self.assertIsNone(self.guard.gate.pending)
        timer.invalidate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
