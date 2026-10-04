"""Live consent-mode Chrome evidence; never run against a personal profile.

The real tray, watcher and relay are used. Child wrappers restrict watcher
discovery to the verified disposable PID and record upstream frames unchanged.
No approval, priming, focus or relay decision is replaced. For a manual Cancel
trial only, an explicit file gate temporarily hides the disposable PID from the
watcher; it does not suppress Chrome's prompt or change the relay.

All runtime output, traces, fixtures and helper configuration stay outside Git.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
from contextlib import suppress
from datetime import datetime, timedelta
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from verify_hold_live import CHROME, Cdp, disposable_pid, write_json

PROFILE = Path('/tmp/yesdev-test-profile')
SOURCE = ('cdp_relay.py', 'relay_mac.py', 'yes_dev_mac.py', 'watcher_mac.py',
          'early_focus_guard_mac.py', 'settings_model.py')


def append(path, value):
    with Path(path).open('a') as stream:
        stream.write(json.dumps({'t': time.time(), **value}) + '\n')


def watcher_child():
    import watcher_mac
    profile = Path(os.environ['YESDEV_TEST_PROFILE'])
    gate = Path(os.environ['YESDEV_TEST_GATE'])
    def only_disposable(_self):
        if gate.exists():
            return []
        try:
            return [disposable_pid(profile)]
        except (OSError, ValueError, AssertionError, subprocess.CalledProcessError):
            return []
    watcher_mac.Engine.chrome_pids = only_disposable
    watcher_mac.Engine.workspace_chrome_pids = only_disposable
    sys.argv = [str(ROOT / 'watcher_mac.py'), *sys.argv[2:]]
    raise SystemExit(watcher_mac.main())


def relay_child():
    import cdp_relay
    import relay_mac
    from websockets.asyncio.client import ClientConnection
    trace = Path(os.environ['YESDEV_TEST_TRACE'])
    evidence_dir = Path(os.environ['YESDEV_TEST_OUT'])
    label_context = contextvars.ContextVar('recording_client', default='unlabelled')
    real_handle = cdp_relay.Relay.handle
    async def labelled_handle(self, downstream):
        try:
            label = json.loads((evidence_dir / 'client-label.json').read_text())['label']
        except (OSError, ValueError, KeyError):
            label = 'unlabelled'
        assert re.fullmatch(r'[A-Za-z0-9_-]+', label), label
        token = label_context.set(label)
        append(evidence_dir / 'socket-events.jsonl', {'event': 'downstream_connected',
            'client': label, 'socket': str(downstream.id), 'remote': downstream.remote_address})
        try:
            return await real_handle(self, downstream)
        finally:
            append(evidence_dir / 'socket-events.jsonl', {'event': 'downstream_closed',
                'client': label, 'socket': str(downstream.id), 'code': downstream.close_code})
            label_context.reset(token)
    cdp_relay.Relay.handle = labelled_handle
    class TraceConnection(ClientConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.label = label_context.get()
            self.waiting_sessions = {}
            self.deferred_sends = set()
            self.drop_monitor = asyncio.create_task(self.watch_drop())
            append(evidence_dir / 'socket-events.jsonl', {'event': 'upstream_created',
                'client': self.label, 'socket': str(self.id)})
        async def watch_drop(self):
            while self.close_code is None:
                marker = evidence_dir / ('drop-' + self.label)
                if marker.exists():
                    marker.unlink()
                    append(evidence_dir / 'socket-events.jsonl', {'event': 'controlled_upstream_drop',
                        'client': self.label, 'socket': str(self.id)})
                    await self.close(1011, 'Controlled reconnaissance disconnect')
                    return
                await asyncio.sleep(.1)
        def record(self, direction, payload):
            try:
                message = json.loads(payload)
            except (TypeError, ValueError):
                message = str(payload)
            frame = {'client': self.label, 'socket': str(self.id), 'direction': direction, 'message': message}
            if direction == 'received' and isinstance(message, dict) and message.get('method') == 'Target.attachedToTarget':
                params = message.get('params', {})
                if params.get('waitingForDebugger'):
                    self.waiting_sessions[params['sessionId']] = {'t': time.time(), 'target': params.get('targetInfo')}
            append(trace, frame)
            append(evidence_dir / ('trace-' + self.label + '.jsonl'), frame)
        async def send(self, message, *args, **kwargs):
            # Optional causal probe only: defer actual debugger-resume delivery
            # for newly paused sessions until each client's release file says so.
            # Ordinary recon has no gate and every payload passes unchanged.
            try:
                decoded = json.loads(message)
                gate = json.loads((evidence_dir / 'resume-gate.json').read_text())
            except (ValueError, TypeError, OSError):
                decoded, gate = {}, {}
            waiting = self.waiting_sessions.get(decoded.get('sessionId'), {})
            target = waiting.get('target') or {}
            eligible_target = (not gate.get('active_tab_only') or (
                target.get('type') == 'tab' and target.get('embedderData', {}).get('tabActive') is True))
            if (decoded.get('method') == 'Runtime.runIfWaitingForDebugger'
                and eligible_target and self.label in gate.get('clients', [])
                and waiting.get('t', 0) >= gate.get('after', float('inf'))):
                self.record('resume_delivery_delayed', message)
                async def deliver():
                    while (evidence_dir / 'resume-gate.json').exists():
                        try:
                            released = json.loads((evidence_dir / 'resume-released.json').read_text())['clients']
                        except (OSError, ValueError, KeyError):
                            released = []
                        if self.label in released:
                            break
                        await asyncio.sleep(.02)
                    self.record('sent', message)
                    await super(TraceConnection, self).send(message, *args, **kwargs)
                # Do not stall Relay.forward: later setup commands still reach
                # Chrome, isolating the resume command from queue backpressure.
                task = asyncio.create_task(deliver())
                self.deferred_sends.add(task)
                task.add_done_callback(self.deferred_sends.discard)
                return
            self.record('sent', message)
            return await super().send(message, *args, **kwargs)
        async def recv(self, *args, **kwargs):
            message = await super().recv(*args, **kwargs)
            self.record('received', message)
            return message
        def connection_lost(self, exc):
            self.drop_monitor.cancel()
            for task in self.deferred_sends:
                task.cancel()
            append(evidence_dir / 'socket-events.jsonl', {'event': 'upstream_closed',
                'client': self.label, 'socket': str(self.id), 'error': repr(exc) if exc else None})
            return super().connection_lost(exc)
    cdp_relay.connect = functools.partial(cdp_relay.connect, create_connection=TraceConnection)
    sys.argv = [str(ROOT / 'relay_mac.py'), *sys.argv[2:]]
    raise SystemExit(relay_mac.main())


def tray_child():
    import yes_dev_mac as tray
    real_popen = subprocess.Popen
    def launch(command, *args, **kwargs):
        original = [str(x) for x in command]
        replacements = {str(tray.WATCHER): '--watcher-child', str(tray.RELAY): '--relay-child'}
        if len(original) > 1 and original[1] in replacements:
            command = [original[0], str(Path(__file__).resolve()), replacements[original[1]], *original[2:]]
        proc = real_popen(command, *args, **kwargs)
        append(Path(os.environ['YESDEV_TEST_OUT']) / 'launches.jsonl',
               {'original_command': original, 'actual_command': command, 'pid': proc.pid})
        return proc
    tray.subprocess.Popen = launch
    sys.argv = [str(ROOT / 'yes_dev_mac.py')]
    raise SystemExit(tray.main())


def windowserver(out, started):
    result = subprocess.run(['/usr/bin/log', 'show', '--info', '--debug', '--style', 'compact',
        '--start', (started - timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S'),
        '--end', (datetime.now() + timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S'),
        '--predicate', 'process == "WindowServer" AND eventMessage CONTAINS "Deferring events from frontmost process"'],
        capture_output=True, text=True, timeout=30)
    (out / 'windowserver.log').write_text(result.stdout)
    (out / 'windowserver-stderr.log').write_text(result.stderr)


class LaunchRun:
    def __init__(self, args):
        self.args = args
        self.out = args.out.resolve()
        if self.out.is_relative_to(ROOT):
            raise ValueError('Live evidence must be written outside the repository')
        self.out.mkdir(parents=True, exist_ok=False)
        self.profile = PROFILE
        self.started = datetime.now()
        self.tray = None
        self.pid = None
        self.case_out = None
        self.cases = []
        self.processes = []
        self.handles = []
        self.initial_runtimes = set(Path('/tmp').glob('yesdev-relay-*'))
        self.url = f'ws://127.0.0.1:{args.port}/devtools/browser/yesdev'
        self.address = f'http://127.0.0.1:{args.port}'
        self.normal_config = Path.home() / 'Library/Application Support/YesDev/config.json'
        self.initial_config = self.normal_config.read_bytes() if self.normal_config.exists() else None
        self.source_hash = {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in SOURCE}
        self.sampling = True
        snapshots = self.out / 'driver-source'
        snapshots.mkdir()
        for name in ('record_cdp_traffic.py', 'recon_cdp_live.py', 'traffic_puppeteer_client.mjs',
                     'native_input_checks.py'):
            path = Path(__file__).with_name(name)
            if path.exists():
                (snapshots / name).write_bytes(path.read_bytes())
        write_json(self.out / 'manifest.json', {
            'started': self.started.isoformat(), 'profile': str(self.profile),
            'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'source_sha256': self.source_hash,
            'normal_config_sha256': hashlib.sha256(self.initial_config).hexdigest() if self.initial_config else None,
            'instrumentation': __doc__})

    def event(self, name, **fields):
        append(self.case_out / 'events.jsonl', {'event': name, **fields})

    async def sample(self):
        from relay_mac import front_pid_with_window, idle_seconds
        while self.sampling:
            try:
                append(self.out / 'foreground.jsonl', {
                    'front_pid_with_window': await front_pid_with_window(),
                    'idle': idle_seconds(), 'case': str(self.case_out)})
            except Exception as exc:
                append(self.out / 'foreground.jsonl', {'error': repr(exc)})
            await asyncio.sleep(.05)

    async def wait_for(self, predicate, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            await asyncio.sleep(.1)
        raise TimeoutError(f'Condition not reached within {seconds}s')

    def status(self):
        try:
            return json.loads((self.case_out / 'app-data/relay-status.json').read_text())
        except (OSError, ValueError):
            return {}

    def relay_text(self):
        path = self.case_out / 'app-data/relay.log'
        return path.read_text() if path.exists() else ''

    def approvals(self):
        path = self.case_out / 'app-data/yes-dev.log'
        return len(re.findall(r'\[ACTION\].*APPROVED via ', path.read_text() if path.exists() else ''))

    async def start_tray(self):
        from settings_model import DEFAULTS, save_atomic
        data = self.case_out / 'app-data'
        data.mkdir(exist_ok=True)
        save_atomic(data / 'config.json', {**DEFAULTS, 'enabled': True, 'quiet_focus': True,
            'relay_enabled': True, 'relay_hold': getattr(self, 'hold', True), 'relay_port': self.args.port,
            'relay_profile': str(self.profile), 'diagnostics': True,
            'notify_style': 'none', 'burst_limit': 0})
        env = {**os.environ, 'YESDEV_DATA_DIR': str(data), 'YESDEV_TEST_PROFILE': str(self.profile),
               'YESDEV_TEST_TRACE': str(self.case_out / 'upstream.jsonl'),
               'YESDEV_TEST_GATE': str(self.case_out / 'inhibit-approval'),
               'YESDEV_TEST_OUT': str(self.case_out)}
        handle = (self.case_out / 'tray-stdout.log').open('w')
        self.handles.append(handle)
        self.tray = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--tray-child'],
            cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)
        self.processes.append(self.tray)
        await self.wait_for(lambda: self.status().get('state') == 'listening')
        assert self.tray.poll() is None
        self.event('tray_listening', pid=self.tray.pid, status=self.status())
        # Ensure LaunchPrimer's constructor has taken its initial endpoint snapshot.
        await asyncio.sleep(1.2)

    async def stop_tray(self):
        if self.tray and self.tray.poll() is None:
            self.tray.terminate()
            try:
                await asyncio.to_thread(self.tray.wait, 8)
            except subprocess.TimeoutExpired:
                self.tray.kill()
                await asyncio.to_thread(self.tray.wait)
        self.tray = None

    def find_browser(self):
        try:
            return disposable_pid(self.profile)
        except (OSError, ValueError, AssertionError, subprocess.CalledProcessError):
            return None

    async def stop_browser(self):
        found = self.find_browser()
        if found:
            if self.pid is not None:
                assert found == self.pid
            os.kill(found, signal.SIGTERM)
            await self.wait_for(lambda: not subprocess.run(
                ['ps', '-p', str(found), '-o', 'pid='], capture_output=True).stdout.strip(), 15)
        self.pid = None
        # Only the already validated, reserved disposable profile's stale endpoint.
        (self.profile / 'DevToolsActivePort').unlink(missing_ok=True)

    async def launch(self, background=False):
        assert self.find_browser() is None
        self.event('launch_requested', background=background)
        background_flag = '-gjna' if self.args.hidden_background else '-gna'
        subprocess.run(['open', background_flag if background else '-na', 'Google Chrome', '--args',
                        '--user-data-dir=' + str(self.profile), '--no-first-run',
                        getattr(self.args, 'launch_url', 'about:blank')], check=True)
        self.pid = await self.wait_for(self.find_browser)
        self.event('endpoint_available', pid=self.pid, hidden_background=background and self.args.hidden_background)
        if not background:
            from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
            NSRunningApplication.runningApplicationWithProcessIdentifier_(self.pid).activateWithOptions_(
                NSApplicationActivateIgnoringOtherApps)
            self.event('foreground_launch_activation', pid=self.pid)

    async def textedit(self):
        from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
        subprocess.run(['open', '-a', 'TextEdit'], check=True)
        await asyncio.sleep(.5)
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_('com.apple.TextEdit')
        assert apps
        apps[0].activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        await asyncio.sleep(2.2)
        from relay_mac import front_pid_with_window
        actual = await front_pid_with_window()
        expected = int(apps[0].processIdentifier())
        self.event('textedit_foreground', pid=actual, expected=expected)
        assert actual == expected, 'TextEdit needs a normal visible window'
        return expected

    async def raw_work(self, name):
        assert disposable_pid(self.profile) == self.pid
        self.event(name + '_start')
        async with Cdp(self.url, self.case_out / 'raw-client.jsonl', name) as client:
            version = await client.call('Browser.getVersion')
            targets = await client.call('Target.getTargets')
        self.event(name + '_complete', version=version, target_count=len(targets['targetInfos']))
        await asyncio.sleep(1)
        return version

    async def pw_work(self):
        from playwright.async_api import async_playwright
        self.event('playwright_start')
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(self.address, timeout=20000)
            page = await browser.contexts[0].new_page()
            await page.goto('data:text/html,<title>YesDev Launch Playwright</title><p>42</p>')
            value = await page.evaluate('({title:document.title,value:6*7})')
            assert value['value'] == 42
            await page.close()
            await browser.close()
        self.event('playwright_complete', value=value)
        await asyncio.sleep(1)

    async def primed(self):
        await self.wait_for(lambda: self.status().get('held'), 20)
        await asyncio.sleep(1)
        self.event('held_verified', status=self.status(), approvals=self.approvals())
        assert self.approvals() == 1
        await self.raw_work('raw_after_prime')
        await self.pw_work()
        assert self.approvals() == 1

    async def ui(self, action, **details):
        result_path = self.case_out / 'ui-result.json'
        # A readiness acknowledgment must not satisfy a later observation step.
        result_path.unlink(missing_ok=True)
        request = {'action': action, 'result_path': str(result_path), **details}
        write_json(self.case_out / 'ui-request.json', request)
        print('UI_STEP ' + json.dumps(request), flush=True)
        await self.wait_for(lambda: (self.case_out / 'ui-result.json').exists(), self.args.ui_timeout)
        answer = json.loads((self.case_out / 'ui-result.json').read_text())
        assert answer.get('completed'), answer
        self.event('human_confirmation', answer=answer)
        return answer

    async def scenario(self, name):
        self.case_out = self.out / name
        self.case_out.mkdir()
        case = {'case': name, 'start': time.time(), 'passed': False}
        try:
            await self.stop_browser()
            if name == 'A3':
                await self.launch(background=True)
                await self.start_tray()
                from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
                NSRunningApplication.runningApplicationWithProcessIdentifier_(self.pid).activateWithOptions_(
                    NSApplicationActivateIgnoringOtherApps)
                self.event('existing_chrome_returned_to_foreground', pid=self.pid)
                await asyncio.sleep(8)
                assert self.approvals() == 0 and not self.status().get('held')
                self.event('no_startup_prompt_verified')
                await self.textedit()
                await self.raw_work('first_client')
                assert self.approvals() == 1
            elif name == 'A2':
                await self.start_tray()
                previous_pid = await self.textedit()
                await self.launch(background=True)
                if self.args.restore_background_app:
                    from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
                    NSRunningApplication.runningApplicationWithProcessIdentifier_(previous_pid).activateWithOptions_(
                        NSApplicationActivateIgnoringOtherApps)
                    self.event('restore_background_setup_app', pid=previous_pid)
                self.event('background_wait_start', seconds=70)
                await asyncio.sleep(70)
                self.event('background_wait_complete', approvals=self.approvals(), status=self.status())
                assert self.approvals() == 0 and not self.status().get('held')
                assert 'within 60s' in self.relay_text()
                await self.raw_work('first_client_after_giveup')
                assert self.approvals() == 1
            elif name == 'A5':
                await self.start_tray()
                await self.ui('ready_for_foreground_launch_and_five_seconds_typing',
                    instructions='When ready, confirm. Chrome will launch after a 3-second countdown. Immediately press Cmd-L and type continuously for about 5 seconds, then stop. Do not press Return.')
                print('LAUNCH_COUNTDOWN 3 seconds', flush=True)
                await asyncio.sleep(3)
                await self.launch()
                await self.wait_for(lambda: self.status().get('held'), 80)
                await asyncio.sleep(1)
                self.event('held_verified', status=self.status(), approvals=self.approvals())
                assert self.approvals() == 1
                # Preserve the omnibox while the operator/observer verifies
                # typing. Client-created pages would otherwise steal its focus.
                answer = await self.ui('confirm_typing_and_launch_prompt_observations',
                    instructions='Confirm actual observations after the typing trial. Return completed, typed_continuously_for_five_seconds, prompt_only_after_idle, address_bar_text_preserved, and no_text_entered_sheet as true only if each was observed. Readiness alone is not a pass.')
                assert all(answer.get(key) is True for key in (
                    'typed_continuously_for_five_seconds', 'prompt_only_after_idle',
                    'address_bar_text_preserved', 'no_text_entered_sheet')), answer
                await self.raw_work('raw_after_typing')
                await self.pw_work()
                assert self.approvals() == 1
            elif name == 'A6':
                await self.start_tray()
                (self.case_out / 'inhibit-approval').touch()
                await self.launch()
                await self.ui('click_cancel_on_disposable_launch_prompt', browser_pid=self.pid,
                    instructions='Click Cancel on the disposable Chrome launch prompt; then confirm.')
                await self.wait_for(lambda: 'Could not open the held connection at launch:' in self.relay_text(), 25)
                self.event('cancel_failure_logged')
                # Observe beyond the entire 60-second launch eligibility window.
                await asyncio.sleep(65)
                assert self.approvals() == 0 and not self.status().get('held')
                (self.case_out / 'inhibit-approval').unlink()
                await self.textedit()
                await self.raw_work('first_client_after_cancel')
                assert self.approvals() == 1
            else:
                await self.start_tray()
                await self.launch()
                await self.primed()
                if name == 'A4':
                    old_pid = self.pid
                    await self.stop_browser()
                    await asyncio.sleep(2)
                    await self.launch()
                    await self.wait_for(lambda: self.status().get('held') and self.approvals() == 2)
                    await asyncio.sleep(1)
                    self.event('restart_primed', old_pid=old_pid, new_pid=self.pid)
                    await self.raw_work('raw_after_restart')
                    await self.pw_work()
                    assert self.approvals() == 2
            case['passed'] = True
        except Exception as exc:
            case.update(error=repr(exc), traceback=traceback.format_exc())
        finally:
            case.update(end=time.time(), browser_pid=self.pid, approvals=self.approvals(), status=self.status())
            self.event('scenario_complete', **case)
            await self.stop_tray()
            self.cases.append(case)
            write_json(self.out / 'cases.json', self.cases)
            print(json.dumps(case), flush=True)

    async def run(self):
        if self.args.start_delay:
            print(f'WAIT {self.args.start_delay}s before the quiet interval', flush=True)
            await asyncio.sleep(self.args.start_delay)
        if self.args.wait_idle:
            from relay_mac import idle_seconds
            print('Waiting for an input-free interval before launch checks', flush=True)
            await self.wait_for(lambda: idle_seconds() >= self.args.wait_idle, 300)
        sampler = asyncio.create_task(self.sample())
        try:
            for name in self.args.only or ['A1', 'A4', 'A2', 'A3']:
                await self.scenario(name)
        finally:
            await self.stop_tray()
            self.sampling = False
            await sampler
            for handle in self.handles:
                handle.close()
            windowserver(self.out, self.started)
            current = self.normal_config.read_bytes() if self.normal_config.exists() else None
            write_json(self.out / 'cleanup.json', {
                'owned_trays_stopped': all(p.poll() is not None for p in self.processes),
                'new_private_runtimes_remaining': [str(p) for p in set(Path('/tmp').glob('yesdev-relay-*')) - self.initial_runtimes],
                'normal_config_unchanged': current == self.initial_config,
                'production_unchanged': all(hashlib.sha256((ROOT / f).read_bytes()).hexdigest() == h for f, h in self.source_hash.items()),
                'disposable_browser_left_running': self.find_browser()})
        return 0 if all(c['passed'] for c in self.cases) else 1


def main():
    children = {'--tray-child': tray_child, '--watcher-child': watcher_child, '--relay-child': relay_child}
    if len(sys.argv) > 1 and sys.argv[1] in children:
        children[sys.argv[1]]()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--port', type=int, default=19339)
    parser.add_argument('--only', action='append', choices=['A1', 'A2', 'A3', 'A4', 'A5', 'A6'])
    parser.add_argument('--ui-timeout', type=float, default=900)
    parser.add_argument('--start-delay', type=float, default=0)
    parser.add_argument('--wait-idle', type=float, default=0)
    parser.add_argument('--launch-url', default='about:blank', help='Optional local visual fixture for identifying the disposable test window')
    parser.add_argument('--hidden-background', action='store_true', help='Use open -j as well as -g because Chrome may self-activate during startup')
    parser.add_argument('--restore-background-app', action='store_true', help='Return to TextEdit immediately after the endpoint appears if Chrome self-activates despite open -g')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(LaunchRun(args).run()))


if __name__ == '__main__':
    main()
