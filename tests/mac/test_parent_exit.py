"""Helpers stop when their supervisor goes, even if it went before they started.

On 2026-10-02 two engines outlived their parent for half an hour: the parent
exited while each engine was still importing, so getppid() already returned
launchd when the engine recorded it, and a parent that never changes is never
noticed as gone. The supervisor now passes its own pid.
"""
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import watcher_mac
import focus_guard_mac

GONE = 999_999   # no live parent of ours has this pid


class EngineParentTests(unittest.TestCase):
    def engine(self, **kwargs):
        engine = watcher_mac.Engine(log_path=Path(os.devnull), exit_with_parent=True, **kwargs)
        engine.log = Mock()
        engine.sweep = Mock()
        return engine

    def test_a_supervisor_gone_before_startup_stops_the_engine_at_once(self):
        engine = self.engine(parent_pid=GONE)
        with patch.object(watcher_mac, "is_trusted", return_value=True):
            self.assertEqual(engine.run(), 0)
        engine.sweep.assert_not_called()
        self.assertIn("parent process is gone", engine.log.call_args.args[0])

    def test_a_live_supervisor_keeps_it_running(self):
        engine = self.engine(parent_pid=os.getppid())
        sweeps = []
        engine.sweep = Mock(side_effect=lambda: sweeps.append(1) or (len(sweeps) >= 2 and (_ for _ in ()).throw(SystemExit)))
        with patch.object(watcher_mac, "is_trusted", return_value=True), \
             patch.object(watcher_mac.time, "sleep"), self.assertRaises(SystemExit):
            engine.run()
        self.assertEqual(len(sweeps), 2)

    def test_without_a_pid_it_falls_back_to_getppid(self):
        self.assertEqual(self.engine()._parent_pid, os.getppid())


class GuardParentTests(unittest.TestCase):
    def guard(self, parent_pid):
        opts = SimpleNamespace(log_path=os.devnull, watch_pid=[GONE], include_edge=False,
                               quiet_s=0.5, require_sheet=True, exit_with_parent=True,
                               parent_pid=parent_pid)
        return focus_guard_mac.Guard(opts)

    def test_the_guard_uses_the_supervisors_pid(self):
        self.assertEqual(self.guard(GONE).parent_pid, GONE)
        self.assertEqual(self.guard(0).parent_pid, os.getppid())

    def test_a_supervisor_gone_before_startup_stops_the_guard(self):
        guard = self.guard(GONE)
        guard.log = Mock()
        with patch.object(focus_guard_mac.os, "_exit", side_effect=SystemExit) as leave, \
             self.assertRaises(SystemExit):
            guard.tick()
        leave.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
