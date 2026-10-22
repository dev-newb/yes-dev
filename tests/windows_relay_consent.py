"""Real Chrome consent through the Windows relay, on an isolated desktop only.

Run through run-private-desktop.ps1 and windows-relay-consent-private.ps1.
The test refuses the input desktop and uses only owned processes/new profiles.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback
import uuid

from websockets.asyncio.client import connect


def private_desktop():
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    name = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    desktop = user.GetThreadDesktop(kernel.GetCurrentThreadId())
    if not user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not name.value.startswith('YesDevTest_'):
        raise RuntimeError('Use the private-desktop runner; refusing this desktop')
    return name.value


async def eventually(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(.05)
    raise TimeoutError('Test condition did not become true')


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}


async def call(ws, method='Browser.getVersion', params=None, id=1):
    await ws.send(json.dumps({'id': id, 'method': method, 'params': params or {}}))
    while True:
        reply = json.loads(await asyncio.wait_for(ws.recv(), 25))
        if reply.get('id') == id:
            if 'error' in reply:
                raise AssertionError(reply)
            return reply['result']


async def run(args, report):
    report['desktop'] = private_desktop()
    sys.path.insert(0, str(args.repo))
    from windows_runtime import Native
    from windows_relay_live import ChildJob
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    profile = root / (args.browser + '-profile')
    profile.mkdir()  # Require a new directory. Never use a current browser profile.
    (profile / 'Local State').write_text(json.dumps({
        'devtools': {'remote_debugging': {'user-enabled': True}}, 'intl': {'app_locale': 'en-US'}}))
    (profile / 'DevToolsActivePort').write_text('0\n')
    copy = root / 'approval-engine'
    copy.mkdir()
    supervisor_text = (args.repo / 'watcher_supervisor.py').read_text(encoding='utf-8-sig')
    marker = 'Global\\\\YesDevEngine'
    assert marker in supervisor_text
    supervisor_text = supervisor_text.replace(marker, 'Global\\\\YesDevRelayTest_' + uuid.uuid4().hex)
    (copy / 'watcher_supervisor.py').write_text(supervisor_text, encoding='utf-8')
    (copy / 'watcher.ps1').write_bytes((args.repo / 'watcher.ps1').read_bytes())
    chrome_path = (Path(os.environ['PROGRAMFILES']) / 'Google/Chrome/Application/chrome.exe'
                   if args.browser == 'chrome' else
                   Path(os.environ['PROGRAMFILES(X86)']) / 'Microsoft/Edge/Application/msedge.exe')
    browser = supervisor = helper = None
    browser_job = None
    handles = []
    clients = []
    watcher_log = root / 'watcher.log'
    status_path = root / 'relay-status.json'
    stop_path = root / 'relay-stop.json'
    run_id = uuid.uuid4().hex
    def launch(command, label, suspended=False):
        stream = (root / (label + '-stderr.txt')).open('w', encoding='utf-8')
        handles.append(stream)
        return subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW | (0x4 if suspended else 0),
                                stdout=stream, stderr=stream)
    def actions():
        try:
            return sum('[ACTION]' in line for line in watcher_log.read_text(encoding='utf-8-sig').splitlines())
        except OSError:
            return 0
    async def client():
        ws = await connect(f'ws://127.0.0.1:{port}/devtools/browser/yesdev', proxy=None,
                           open_timeout=30, close_timeout=2)
        clients.append(ws)
        return ws
    async def idle():
        await eventually(lambda: read_json(status_path).get('connections') == 0)
        # Connection count changes before held.release cleanup in some paths.
        await asyncio.sleep(.2)
    try:
        supervisor = launch([sys.executable, str(copy / 'watcher_supervisor.py'),
                             '-IntervalMs', str(args.interval), '-LogPath', str(watcher_log),
                             '-BrowserProcess', args.browser, '-ParentPid', str(os.getpid())], 'supervisor')
        browser = launch([str(chrome_path), '--user-data-dir=' + str(profile), '--lang=en-US',
                          '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
                          '--disable-sync', '--disable-component-update',
                          '--enable-features=DevToolsAcceptDebuggingConnections', 'about:blank'], args.browser, suspended=True)
        browser_job = ChildJob(browser)
        resume = ctypes.WinDLL('ntdll').NtResumeProcess
        resume.argtypes = [wintypes.HANDLE]
        resume.restype = ctypes.c_long
        assert resume(wintypes.HANDLE(int(browser._handle))) == 0
        def endpoint_ready():
            try:
                lines = (profile / 'DevToolsActivePort').read_text().splitlines()
                return len(lines) >= 2 and int(lines[0]) > 0 and lines[1].startswith('/devtools/browser/')
            except (OSError, ValueError):
                return False
        await eventually(endpoint_ready)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        native = Native()
        parent_creation = native.creation(native.kernel.GetCurrentProcess())
        helper = launch([sys.executable, str(args.repo / 'relay_windows.py'), '--profile', str(profile),
                         '--port', str(port), '--status-path', str(status_path),
                         '--log-path', str(root / 'relay.log'), '--parent-pid', str(os.getpid()),
                         '--parent-creation', str(parent_creation), '--stop-path', str(stop_path),
                         '--run-id', run_id, '--hold', *(['--multiplex'] if args.multiplex else [])], 'relay')
        await eventually(lambda: read_json(status_path).get('state') == 'listening')
        products = []
        for number in range(3):
            ws = await client()
            products.append((await call(ws))['product'])
            await ws.close()
            await idle()
        await eventually(lambda: actions() >= 1)
        assert actions() == 1, f'Expected one initial approval, got {actions()}'
        assert read_json(status_path).get('held') is True
        report['checks'].append('three sequential clients reuse one approved browser connection')
        first = await client()
        await call(first)
        second = await client()
        await call(second)
        expected = 1 if args.multiplex else 2
        if args.multiplex:
            third = await client()
            await call(third)
            await third.close()
            await asyncio.sleep(.5)
        else:
            await eventually(lambda: actions() >= 2)
        assert actions() == expected, f'Expected {expected} approvals, got {actions()}'
        await second.close()
        await first.close()
        await idle()
        report['checks'].append('three concurrent clients reuse one approval' if args.multiplex else
                                'concurrent client gets one separate approved connection')
        ws = await client()
        await call(ws, 'Browser.close')
        await ws.wait_closed()
        await idle()
        assert endpoint_ready()
        assert actions() == expected
        ws = await client()
        await call(ws)
        await ws.close()
        await idle()
        report['checks'].append('Browser.close ends its client and the browser stays usable')
        stop_path.write_text(json.dumps({'run_id': run_id, 'reason': 'native_consent_test_complete'}))
        await eventually(lambda: helper.poll() is not None)
        assert helper.returncode == 0, helper.returncode
        assert endpoint_ready()
        assert supervisor.poll() is None
        report['checks'].append('graceful relay stop preserves browser and approval supervisor')
        report.update(passed=True, products=products, approval_count=actions(), interval=args.interval,
                      relay_exit=helper.returncode, final_status=read_json(status_path),
                      browser=args.browser, multiplex=args.multiplex)
    finally:
        for ws in clients:
            await ws.close()
        for process in (helper, supervisor):
            if process is not None and process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 8)
        if browser_job is not None:
            await browser_job.terminate()
        if browser is not None and browser.poll() is None:
            browser.terminate()
            await asyncio.to_thread(browser.wait, 8)
        for stream in handles:
            stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--interval', type=int, choices=(150, 250, 750), default=250)
    parser.add_argument('--browser', choices=('chrome', 'msedge'), default='chrome')
    parser.add_argument('--multiplex', action='store_true')
    args = parser.parse_args()
    report = {'passed': False, 'checks': [], 'scope': 'real consent UI on an isolated Windows desktop'}
    try:
        asyncio.run(run(args, report))
    except Exception:
        report['error'] = traceback.format_exc()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'relay-consent-results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
