"""Exercise stalls and recovery without Windows desktop access or live processes."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from watcher_supervisor import Supervisor


class Process:
    def __init__(self, pid):
        self.pid = pid
        self.code = None
        self.killed = False

    def poll(self):
        return self.code

    def kill(self):
        self.killed = True

    def wait(self, timeout):
        self.code = -1
        return self.code


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.started = []
        self.logs = []

        def launch(key, path, legacy_first):
            process = Process(len(self.started) + 1)
            self.started.append((key, path, legacy_first, process))
            return process

        self.supervisor = Supervisor(self.temp.name, launch, self.logs.append)

    def heartbeat(self, index, stamp):
        path = self.started[index][1]
        path.write_text('progress')
        os.utime(path, ns=(stamp, stamp))

    def test_stalled_worker_does_not_block_other_dialogs(self):
        first, second, third = (1, 10), (2, 20), (3, 30)
        self.supervisor.tick([first, second], 0)
        self.heartbeat(0, 1_000_000_000)
        self.heartbeat(1, 1_000_000_000)
        self.supervisor.tick([first, second], 1)
        self.heartbeat(1, 2_000_000_000)
        self.supervisor.tick([first, second, third], 6)
        self.assertTrue(self.started[0][3].killed)
        self.assertFalse(self.started[1][3].killed)
        self.assertEqual(self.started[2][0], third)
        # A killed process still occupies its slot until Windows reports exit.
        self.supervisor.tick([first, second, third], 6.1)
        self.assertEqual(len(self.started), 3)
        self.started[0][3].code = -1
        self.supervisor.tick([first, second, third], 6.2)
        self.supervisor.tick([first, second, third], 6.5)
        self.assertEqual(self.started[3][0], first)
        self.assertTrue(self.started[3][2], 'Retry should use the native legacy method first')

    def test_startup_is_bounded_even_before_first_heartbeat(self):
        self.supervisor.tick([(1, 10)], 0)
        self.supervisor.tick([(1, 10)], 9.9)
        self.assertFalse(self.started[0][3].killed)
        self.supervisor.tick([(1, 10)], 10)
        self.assertTrue(self.started[0][3].killed)

    def test_heartbeat_keeps_a_busy_worker_alive(self):
        for second in range(20):
            if self.started:
                self.heartbeat(0, (second + 1) * 1_000_000_000)
            self.supervisor.tick([(1, 10)], second)
        self.assertEqual(len(self.started), 1)
        self.assertFalse(self.started[0][3].killed)

    def test_queue_is_bounded_and_waiting_dialog_precedes_crash_retry(self):
        dialogs = [(n, n) for n in range(1, 9)]
        self.supervisor.tick(dialogs, 0)
        self.assertEqual(len(self.started), 4)
        self.started[0][3].code = 1
        self.supervisor.tick(dialogs, 1)
        self.assertEqual(self.started[4][0], (5, 5))
        self.assertEqual(len(self.supervisor.workers), 4)

    def test_reused_handle_has_a_separate_owner_identity(self):
        self.supervisor.tick([(1, 10)], 0)
        self.supervisor.tick([(1, 20)], 1)
        self.assertEqual([entry[0] for entry in self.started], [(1, 10), (1, 20)])

    def test_closed_dialog_is_not_retried_and_state_is_removed(self):
        self.supervisor.tick([(1, 10)], 0)
        self.started[0][3].code = 0
        self.supervisor.tick([], 1)
        self.assertFalse(self.supervisor.workers)
        self.assertFalse(self.supervisor.retry_at)
        self.assertFalse(self.started[0][1].exists())

    def test_shutdown_stops_all_owned_workers(self):
        self.supervisor.tick([(1, 10), (2, 20)], 0)
        self.supervisor.close()
        self.assertTrue(all(entry[3].killed for entry in self.started))
        self.assertFalse(self.supervisor.workers)


if __name__ == '__main__':
    unittest.main()
