"""Tray integration tests with private settings and no native app processes."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == 'win32', 'Windows tray integration')
class RelayTrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo = Path(__file__).resolve().parents[1]
        loader = importlib.machinery.SourceFileLoader('yesdev_relay_tray_test', str(repo / 'yes_dev.pyw'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.module = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'path', [str(repo), *sys.path]):
            loader.exec_module(cls.module)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yesdev-relay-tray-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.paths = patch.multiple(self.module, DATA_DIR=root, CONFIG_PATH=root / 'config.json',
                                    LOG_PATH=root / 'watcher.log', TRAY_LOG=root / 'tray.log')
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.manager = Mock()
        self.edge_manager = Mock()
        self.manager.status_text.return_value = 'Chrome relay: off'
        factory = patch.object(self.module, 'RelayManager', side_effect=[self.manager, self.edge_manager])
        factory.start()
        self.addCleanup(factory.stop)
        self.app = self.module.YesDev()

    def test_default_off_and_menu_toggle_does_not_restart_approvals(self):
        self.assertFalse(self.app.cfg['relay_hold'])
        menu = self.app.build_menu()
        options = next(item for item in menu.items if item.text == 'Options').submenu
        checkbox = next(item for item in options.items if item.text == 'Keep one Chrome connection open')
        self.assertFalse(checkbox.checked)
        with patch.object(self.app, 'restart_engine') as restart:
            checkbox(None)
        restart.assert_not_called()
        self.assertTrue(checkbox.checked)
        self.assertTrue(json.loads(self.module.CONFIG_PATH.read_text())['relay_hold'])
        self.manager.sync.assert_called_with(True, self.app.cfg['relay_profile'], 9333)

    def test_effective_gate_covers_off_observe_pause_and_quit(self):
        self.app.cfg['relay_hold'] = True
        for name in ('off', 'observe', 'pause', 'quitting', 'enabled'):
            with self.subTest(name=name):
                self.app.cfg['enabled'] = name != 'off'
                self.app.cfg['observe_only'] = name == 'observe'
                self.app.paused_reason = 'test pause' if name == 'pause' else None
                self.app._quitting = name == 'quitting'
                self.app.sync_relay()
                self.assertIs(self.manager.sync.call_args.args[0], name == 'enabled')

    def test_observe_action_stops_relay_immediately(self):
        self.app.cfg['relay_hold'] = True
        with patch.object(self.app, 'restart_engine') as restart:
            self.app.toggle('observe_only', restart=True)()
        restart.assert_called_once()
        self.assertFalse(self.manager.sync.call_args.args[0])

    def test_pause_stops_relay_and_quit_cannot_restart_it(self):
        self.app.cfg['relay_hold'] = True
        with patch.object(self.app, 'stop_engine') as stop:
            self.app._pause('burst guard')
            self.assertFalse(self.manager.sync.call_args.args[0])
            self.app.quit()
        self.manager.stop.assert_called_once_with('tray_quit')
        self.assertTrue(self.app._quitting)
        self.manager.reset_mock()
        with patch.object(self.app, 'start_engine') as start:
            self.app._tick()
        start.assert_not_called()
        self.manager.sync.assert_not_called()

    def test_relay_exception_does_not_stop_tick_or_engine(self):
        self.app.cfg['relay_hold'] = True
        self.manager.sync.side_effect = RuntimeError('test collision')
        with patch.object(self.app, 'engine_running', return_value=True), \
                patch.object(self.app, '_read_log'), patch.object(self.app, 'stop_engine') as stop:
            self.app._tick()
            self.app._tick()
        stop.assert_not_called()
        self.assertEqual(self.module.TRAY_LOG.read_text().count('test collision'), 1)

    def test_invalid_relay_config_disarms_only_relay(self):
        for value in ('9333', True, 80, 65536):
            with self.subTest(port=value):
                self.module.CONFIG_PATH.write_text(json.dumps({'relay_hold': True, 'relay_port': value}))
                cfg = self.module.Config()
                self.assertFalse(cfg['relay_hold'])
                self.assertEqual(cfg['relay_port'], 9333)
                self.assertTrue(cfg['enabled'])
        self.module.CONFIG_PATH.write_text(json.dumps({'relay_hold': True, 'relay_profile': []}))
        self.assertFalse(self.module.Config()['relay_hold'])

    def test_both_relays_have_independent_controls_and_stop_together(self):
        self.assertFalse(self.app.cfg['edge_relay_hold'])
        self.app.cfg.update(relay_hold=True, edge_relay_hold=True)
        self.app.sync_relay()
        self.manager.sync.assert_called_with(True, self.app.cfg['relay_profile'], 9333)
        self.edge_manager.sync.assert_called_with(True, self.app.cfg['edge_relay_profile'], 9334)
        self.app.cfg['edge_relay_hold'] = False
        self.app.sync_relay()
        self.assertTrue(self.manager.sync.call_args.args[0])
        self.assertFalse(self.edge_manager.sync.call_args.args[0])
        self.app.stop_relay('test')
        self.manager.stop.assert_called_once_with('test')
        self.edge_manager.stop.assert_called_once_with('test')

    def test_edge_validation_and_port_collision(self):
        self.module.CONFIG_PATH.write_text(json.dumps({'relay_hold': True, 'edge_relay_hold': True,
                                                      'relay_port': 9333, 'edge_relay_port': 9333}))
        cfg = self.module.Config()
        self.assertTrue(cfg['relay_hold'])
        self.assertFalse(cfg['edge_relay_hold'])
        for port in (True, '9334', 80, 65536):
            self.module.CONFIG_PATH.write_text(json.dumps({'edge_relay_hold': True, 'edge_relay_port': port}))
            self.assertFalse(self.module.Config()['edge_relay_hold'])

    def test_chrome_control_failure_does_not_prevent_edge_control(self):
        self.app.cfg['edge_relay_hold'] = True
        self.manager.sync.side_effect = RuntimeError('Chrome failure')
        self.app.sync_relay()
        self.assertTrue(self.edge_manager.sync.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
