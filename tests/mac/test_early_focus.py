"""Policy and private control-channel checks for the early-focus prototype."""
import json
from pathlib import Path
import socket
import tempfile
import unittest
import uuid

from early_focus_protocol import AppIdentity, ArmGate
from early_focus_guard import ControlServer


class ArmGateTests(unittest.TestCase):
    def setUp(self):
        self.browser = AppIdentity(101, 1.0)
        self.editor = AppIdentity(202, 2.0)
        self.gate = ArmGate(self.browser)

    def arm(self):
        self.assertEqual(self.gate.arm("one", self.editor, self.browser, 10.0, 5.0), "armed")

    def test_one_restore_per_request(self):
        self.arm()
        self.assertEqual(self.gate.activation(self.browser, self.editor, 10.1, 5.1), "restore")
        self.assertEqual(self.gate.activation(self.browser, self.editor, 10.5, 5.5), "already-used")

    def test_unarmed_activation_does_nothing(self):
        self.assertEqual(self.gate.activation(self.browser, self.editor, 10, 5), "unarmed")

    def test_expired_request_does_nothing(self):
        self.arm()
        self.assertEqual(self.gate.activation(self.browser, self.editor, 12, 7), "expired")

    def test_input_after_arm_cancels_even_when_it_has_aged(self):
        self.arm()
        self.assertEqual(self.gate.activation(self.browser, self.editor, 11.5, 1.0), "input-after-arm")
        self.assertIsNone(self.gate.pending)

    def test_intervening_app_cancels(self):
        self.arm()
        self.assertEqual(self.gate.activation(AppIdentity(303, 3), self.editor, 10.1, 5.1), "different-app")
        self.assertEqual(self.gate.activation(self.browser, self.editor, 10.2, 5.2), "unarmed")

    def test_browser_pid_reuse_is_rejected(self):
        self.assertEqual(self.gate.arm("one", self.editor, AppIdentity(101, 99), 10, 5),
                         "browser-restarted-or-gone")

    def test_previous_app_identity_must_match(self):
        self.arm()
        self.assertEqual(self.gate.activation(self.browser, AppIdentity(202, 99), 10.1, 5.1),
                         "previous-app-changed")

    def test_recent_input_rejects_arm(self):
        self.assertEqual(self.gate.arm("one", self.editor, self.browser, 10, 0.1), "recent-input")

    def test_busy_request_is_not_overwritten(self):
        self.arm()
        self.assertEqual(self.gate.arm("two", self.editor, self.browser, 10.1, 5.1), "busy")
        self.assertEqual(self.gate.pending.request_id, "one")

    def test_wrong_finish_does_not_cancel_request(self):
        self.arm()
        self.assertEqual(self.gate.finish("other"), "unknown-request")
        self.assertIsNotNone(self.gate.pending)

    def test_finish_and_disconnect_disarm(self):
        self.arm()
        self.assertEqual(self.gate.finish("one"), "finished")
        self.arm()
        self.gate.clear()
        self.assertEqual(self.gate.activation(self.browser, self.editor, 10.1, 5.1), "unarmed")


class ControlServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ydef-", dir="/tmp")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "c.sock"
        self.messages = []
        self.drops = []
        self.server = ControlServer(self.path, "test-token", self.handle, lambda: self.drops.append(True))
        self.addCleanup(self.server.close)
        self.client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.client.close)
        self.client.settimeout(1)
        self.client.connect(str(self.path))
        self.request_id = str(uuid.uuid4())

    def handle(self, message):
        self.messages.append(message)
        return {"request_id": message["request_id"], "status": "armed"}

    def message(self, token="test-token"):
        return (json.dumps({"op": "ARM", "token": token, "request_id": self.request_id}) + "\n").encode()

    def test_ack_only_after_authenticated_handler_runs(self):
        self.client.sendall(self.message())
        self.server.poll()
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(json.loads(self.client.recv(4096))["status"], "armed")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_bad_token_is_rejected_without_arm(self):
        self.client.sendall(self.message("wrong"))
        self.server.poll()
        self.assertEqual(self.messages, [])
        self.assertEqual(self.client.recv(4096), b"")

    def test_partial_frame_waits_for_newline(self):
        message = self.message()
        self.client.sendall(message[:-1])
        self.server.poll()
        self.assertEqual(self.messages, [])
        self.client.sendall(message[-1:])
        self.server.poll()
        self.assertEqual(len(self.messages), 1)

    def test_oversized_message_is_rejected(self):
        self.client.sendall(b"x" * 4097)
        self.server.poll()
        self.assertEqual(self.messages, [])
        self.assertIsNone(self.server.client)

    def test_disconnect_runs_cancellation(self):
        self.server.poll()
        self.client.close()
        self.server.poll()
        self.assertEqual(self.drops, [True])


if __name__ == "__main__":
    unittest.main()
