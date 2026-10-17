"""Check real tray speed actions without starting a tray or watcher."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == "win32", "Windows tray menu")
class SpeedMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo = Path(__file__).resolve().parents[1]
        loader = importlib.machinery.SourceFileLoader("yesdev_speed_test", str(repo / "yes_dev.pyw"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(repo), *sys.path]):
            loader.exec_module(cls.module)

    def test_each_speed_saves_an_integer_and_passes_it_to_the_watcher(self):
        module = self.module
        with tempfile.TemporaryDirectory(prefix="yesdev-speed-menu-") as folder:
            root = Path(folder)
            with patch.multiple(module, DATA_DIR=root, CONFIG_PATH=root / "config.json",
                                LOG_PATH=root / "watcher.log", TRAY_LOG=root / "tray.log"):
                app = module.YesDev()
                app.cfg["observe_only"] = True
                menu = app.build_menu()
                choices = next(item for item in menu.items if item.text == "Speed").submenu.items
                self.assertEqual(len(choices), 3)
                for selected, (label, interval) in zip(choices, module.POLL_CHOICES):
                    with self.subTest(speed=label):
                        self.assertEqual(selected.text, label)
                        with patch.object(app, "restart_engine") as restart:
                            # MenuItem supplies both icon and item to two-argument callbacks.
                            selected(None)
                            restart.assert_called_once_with()
                        self.assertIs(type(app.cfg["poll_ms"]), int)
                        self.assertEqual(app.cfg["poll_ms"], interval)
                        saved = json.loads(module.CONFIG_PATH.read_text(encoding="utf-8"))
                        self.assertEqual(saved["poll_ms"], interval)
                        self.assertIs(type(saved["poll_ms"]), int)
                        self.assertEqual([item.checked for item in choices],
                                         [item is selected for item in choices])
                        app.proc = None
                        with patch.object(module.subprocess, "Popen", return_value=Mock(pid=12345)) as launch:
                            app.start_engine()
                        arguments = launch.call_args.args[0]
                        self.assertEqual(arguments[arguments.index("-IntervalMs") + 1], str(interval))
                        self.assertIn("-Observe", arguments)


if __name__ == "__main__":
    unittest.main()
