"""Fault injection against the Windows host and a test-owned headless Chrome.

No tray, installed settings, shared browser endpoint, or current MCP is used.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from relay_windows import RelayManager
from windows_relay_live import Cdp, PrivateChrome
from websockets.asyncio.client import connect


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Run:
    def __init__(self, args):
        self.args = args
        self.results = []

    async def wait(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.manager.sync(self.enabled, str(self.chrome.profile), self.port)
            process = self.manager._process
            if process is not None:
                self.children[process.pid] = process
            if predicate():
                return
            await asyncio.sleep(.02)
        raise AssertionError('Condition timed out: ' + self.manager.status_text())

    async def ready(self):
        await self.wait(lambda: self.manager.status_text() in ('Relay: listening', 'Relay: held'))

    @property
    def url(self):
        return f'ws://127.0.0.1:{self.port}/devtools/browser/yesdev'

    async def version(self):
        async with Cdp(self.url) as client:
            version = await client.call('Browser.getVersion')
        await self.wait(lambda: self.manager._status().get('connections') == 0)
        return version

    async def guarded_close(self, binary):
        async with Cdp(self.url) as owner:
            await owner.call('Browser.getVersion')
            async with connect(self.url, proxy=None, open_timeout=5, close_timeout=1) as second:
                if binary:
                    frame = b'{"id":9,"method":"Browser.close"}'
                else:
                    frame = '{"id":9,"method":"Browser.\\u0063lose"}'
                await second.send(frame)
                reply = json.loads(await asyncio.wait_for(second.recv(), 5))
                assert reply == {'id': 9, 'result': {}}, reply
                await asyncio.wait_for(second.wait_closed(), 5)
                assert second.close_code == 1000, second.close_code
            assert self.chrome.process.poll() is None, 'Concurrent close exited test Chrome'
            await owner.call('Browser.getVersion')
        return {'browser_alive': True, 'frame': 'binary' if binary else 'escaped-text'}

    async def helper_crash(self):
        async with Cdp(self.url) as client:
            await client.call('Browser.getVersion')
            first = self.manager._process
            first.kill()  # Only the Popen created by this test's manager.
            await asyncio.to_thread(first.wait, 5)
            await asyncio.wait_for(client.ws.wait_closed(), 5)
        await self.wait(lambda: self.manager._process is not None
                        and self.manager._process.pid != first.pid
                        and self.manager.status_text() == 'Relay: listening')
        await self.version()
        assert self.chrome.process.poll() is None
        return {'old_helper': first.pid, 'new_helper': self.manager._process.pid, 'browser_alive': True}

    async def rapid_settings(self):
        ports = [free_port() for _ in range(3)]
        for number in range(40):
            self.enabled = number % 3 != 0
            self.port = ports[number % len(ports)]
            self.manager.sync(self.enabled, str(self.chrome.profile), self.port)
            child = self.manager._process
            if child:
                self.children[child.pid] = child
            await asyncio.sleep(.005)
        self.enabled = True
        self.port = ports[-1]
        await self.ready()
        await self.version()
        await self.wait(lambda: sum(p.poll() is None for p in self.children.values()) == 1)
        assert self.chrome.process.poll() is None
        return {'changes': 40, 'surviving_owned_helpers': 1, 'final_port': self.port}

    async def port_collision(self):
        self.enabled = False
        await self.wait(lambda: self.manager._process is None and self.manager._worker is None)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            self.port = listener.getsockname()[1]
            self.enabled = True
            await self.wait(lambda: 'error' in self.manager.status_text())
            with socket.create_connection(('127.0.0.1', self.port), timeout=2):
                pass
        await self.ready()
        await self.version()
        return {'port_owner_preserved': True, 'recovered_after_owner_released_port': True}

    async def partial_endpoint(self):
        endpoint = self.chrome.profile / 'DevToolsActivePort'
        original = endpoint.read_bytes()
        helper_pid = self.manager._process.pid
        try:
            endpoint.write_text('not-a-port\n')
            async with connect(self.url, proxy=None, open_timeout=5, close_timeout=1) as client:
                await asyncio.wait_for(client.wait_closed(), 5)
                assert client.close_code == 1011, client.close_code
        finally:
            endpoint.write_bytes(original)
        await self.version()
        assert self.manager._process.pid == helper_pid
        return {'same_helper_recovered': True, 'browser_alive': self.chrome.process.poll() is None}

    async def stop_active(self):
        for _ in range(5):
            async with Cdp(self.url) as client:
                await client.call('Browser.getVersion')
                self.enabled = False
                await self.wait(lambda: self.manager._process is None and self.manager._worker is None)
                await asyncio.wait_for(client.ws.wait_closed(), 5)
            assert self.chrome.process.poll() is None
            self.enabled = True
            await self.ready()
        await self.version()
        return {'active_stop_restart_cycles': 5, 'browser_alive': True}

    async def case(self, name, action):
        record = {'case': name, 'passed': False}
        self.chrome = PrivateChrome(self.args.chrome)
        self.children = {}
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='yesdev-relay-faults-') as temp:
            self.manager = RelayManager(ROOT, Path(temp))
            # Only test retry waits are shortened; the normal parent code is used.
            self.manager.RETRY_MIN = self.manager._delay = .1
            self.manager.RETRY_MAX = .3
            self.manager.START_TIMEOUT = 3
            self.port = free_port()
            self.enabled = True
            try:
                await self.chrome.start()
                await self.ready()
                record.update(await action())
                record['passed'] = True
            except Exception:
                record['error'] = traceback.format_exc()
                await asyncio.sleep(.15)
                record['browser_alive_before_cleanup'] = self.chrome.process is not None and self.chrome.process.poll() is None
                record['helper_diagnostics'] = {
                    'status': self.manager._status(),
                    'cached_status': self.manager.status_text(),
                    'stderr': {p.parent.name: p.read_text(encoding='utf-8', errors='replace')[-8000:]
                               for p in Path(temp).glob('relay-runs/*/stderr.log')},
                    'log': (Path(temp) / 'relay.log').read_text(encoding='utf-8', errors='replace')[-8000:]
                           if (Path(temp) / 'relay.log').exists() else '',
                }
            finally:
                self.enabled = False
                self.manager.stop('adversarial test end')
                try:
                    await self.wait(lambda: self.manager._process is None and self.manager._worker is None)
                except Exception:
                    record['cleanup_error'] = traceback.format_exc()
                    record['passed'] = False
                for child in self.children.values():
                    if child.poll() is None:
                        child.kill()
                        await asyncio.to_thread(child.wait, 5)
                await self.chrome.cleanup()
                record['all_owned_helpers_stopped'] = all(p.poll() is not None for p in self.children.values())
                record['seconds'] = round(time.monotonic() - started, 3)
        self.results.append(record)

    async def run(self):
        for name, action in (
            ('binary_concurrent_browser_close', lambda: self.guarded_close(True)),
            ('escaped_concurrent_browser_close', lambda: self.guarded_close(False)),
            ('forced_helper_crash_and_retry', self.helper_crash),
            ('rapid_settings_final_value_wins', self.rapid_settings),
            ('occupied_port_then_release', self.port_collision),
            ('partial_endpoint_then_recovery', self.partial_endpoint),
            ('disable_with_active_client', self.stop_active),
        ):
            if self.args.case and name not in self.args.case:
                continue
            await self.case(name, action)
        report = {'passed': all(r['passed'] for r in self.results), 'cases': self.results,
                  'scope': 'test-owned Windows host processes and private headless Chrome profiles',
                  'limits': 'No native consent or focus tests in this harness; retry delays shortened in the test only'}
        self.args.output.parent.mkdir(parents=True, exist_ok=True)
        self.args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2))
        return 0 if report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chrome', type=Path, default=Path(os.environ['PROGRAMFILES']) / 'Google/Chrome/Application/chrome.exe')
    parser.add_argument('--case', action='append', help='run only the named case; repeat as needed')
    return asyncio.run(Run(parser.parse_args()).run())


if __name__ == '__main__':
    raise SystemExit(main())
