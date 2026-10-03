"""Real Chrome tests for the opt-in held CDP connection.

Requires an already-running consent-mode /tmp/yesdev-* Chrome. Results stay
outside Git. Each scenario gets a fresh relay, and only the verified disposable
browser may be restarted. Approval uses the real watcher with PID discovery
restricted to that profile. A ClientConnection subclass records upstream frames
without changing messages, replies, reset logic, or the relay entry point.

Install puppeteer-core outside the repository and pass its absolute module path.
The optional native-UI step writes ui-request.json and waits for ui-result.json;
an operator/UI driver must actually open the requested tab before recording it.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
import functools
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from cdp_relay import read_endpoint
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def disposable_pid(profile):
    """Never infer the browser from the frontmost app or a bundle id alone."""
    profile = profile.resolve()
    assert profile.is_relative_to(Path('/tmp').resolve()) and profile.name.startswith('yesdev-')
    endpoint = read_endpoint(profile)
    pids = set(subprocess.check_output(['lsof', '-nP', '-t', f'-iTCP:{endpoint.port}',
                                      '-sTCP:LISTEN'], text=True).split())
    assert len(pids) == 1, pids
    pid = int(pids.pop())
    command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True).strip()
    assert command.startswith(CHROME + ' '), command
    # Resolve /tmp's macOS symlink without relaxing the exact profile check.
    matches = re.findall(r'--user-data-dir=([^ ]+)', command)
    assert len(matches) == 1 and Path(matches[0]).resolve() == profile, command
    assert '--remote-debugging-port' not in command and '--remote-debugging-pipe' not in command
    return pid


def cpu_sample(pid):
    fields = subprocess.check_output(['ps', '-p', str(pid), '-o', 'pid=,pcpu=,cputime=,rss='], text=True).split()
    total = 0.
    for part in fields[2].split(':'):
        total = total * 60 + float(part)
    return {'t': time.monotonic(), 'pid': int(fields[0]), 'cpu_percent_ps': float(fields[1]),
            'cpu_seconds': total, 'rss_kib': int(fields[3])}


def foreground():
    from AppKit import NSWorkspace
    from Foundation import NSDate, NSRunLoop
    from Quartz import CGEventSourceSecondsSinceLastEventType, kCGEventSourceStateCombinedSessionState
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.002))
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return {'t': time.time(), 'pid': int(app.processIdentifier()), 'name': str(app.localizedName()),
            'bundle': str(app.bundleIdentifier()), 'policy': int(app.activationPolicy()),
            'idle': CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateCombinedSessionState, 0xffffffff)}


def banner_snapshot(pid):
    """Read only text relevant to the automation notice in the disposable app."""
    import watcher_mac as watcher
    found, visited = set(), 0
    def walk(element, depth):
        nonlocal visited
        visited += 1
        if depth > 15 or visited > 4000:
            return
        for key in ('AXTitle', 'AXDescription', 'AXValue'):
            value = watcher._attr(element, key)
            if isinstance(value, str) and re.search(r'controlled|automated|debugg', value, re.I):
                found.add(value)
        for child in watcher._attr(element, 'AXChildren') or []:
            walk(child, depth + 1)
    walk(watcher.AXUIElementCreateApplication(pid), 0)
    return {'time': time.time(), 'pid': pid, 'matching_text': sorted(found), 'visited_nodes': visited}


class Cdp:
    def __init__(self, url, trace_path, label):
        self.url, self.path, self.label = url, trace_path, label
        self.next_id = 0
        self.pending, self.events, self.unsolicited = {}, [], []
        self.ws = self.reader = None

    def record(self, direction, message):
        with self.path.open('a') as out:
            out.write(json.dumps({'t': time.time(), 'client': self.label,
                                  'direction': direction, 'message': message}) + '\n')

    async def __aenter__(self):
        self.ws = await connect(self.url, proxy=None, close_timeout=1, open_timeout=20,
                                ping_interval=None, max_size=64 * 1024 * 1024)
        self.reader = asyncio.create_task(self.read())
        return self

    async def read(self):
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                self.record('received', message)
                if 'id' in message:
                    future = self.pending.pop(message['id'], None)
                    if future is not None and not future.done(): future.set_result(message)
                    else: self.unsolicited.append(message)
                else:
                    self.events.append(message)
        except ConnectionClosed:
            pass
        finally:
            for future in self.pending.values():
                if not future.done(): future.set_exception(ConnectionError('CDP connection closed'))

    async def call(self, method, params=None, session=None, timeout=20):
        self.next_id += 1
        message = {'id': self.next_id, 'method': method, 'params': params or {}}
        if session is not None: message['sessionId'] = session
        future = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        self.record('sent', message)
        await self.ws.send(json.dumps(message))
        reply = await asyncio.wait_for(future, timeout)
        if 'error' in reply: raise RuntimeError(f'{method}: {reply["error"]}')
        return reply['result']

    async def __aexit__(self, *_):
        await self.ws.close()
        await self.reader


class LiveRun:
    def __init__(self, args):
        self.args = args
        self.out = args.out.resolve()
        self.out.mkdir(parents=True, exist_ok=False)
        self.profile = args.profile.resolve()
        self.browser_pid = disposable_pid(self.profile)
        assert self.browser_pid == args.browser_pid
        self.started = datetime.now()
        self.current = None
        self.cases, self.processes, self.handles, self.samples = [], [], [], []
        self.engine = self.relay = None
        self.sampling_stop = False
        self.initial_runtimes = set(Path('/tmp').glob('yesdev-relay-*'))
        self.url = f'ws://127.0.0.1:{args.port}/devtools/browser/yesdev'
        self.address = f'http://127.0.0.1:{args.port}'
        self.trace = self.out / 'client-protocol.jsonl'
        self.fixture_hits = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.fixture_hits.append({'time': time.time(), 'path': self.path})
                body = (b'<!doctype html><title>YesDev Manual Load</title><p>Loaded test page</p>'
                        b'<script>window.yesdevLoaded=true;fetch("/script-executed")</script>')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_): pass
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def spawn(self, arguments, label, extra_env=None):
        output = open(self.out / f'{label}-stdout.log', 'w')
        self.handles.append(output)
        env = {**os.environ, 'YESDEV_DATA_DIR': str(self.out / 'app-data'), **(extra_env or {})}
        proc = subprocess.Popen([sys.executable, *map(str, arguments)], cwd=ROOT,
                                env=env, stdout=output, stderr=subprocess.STDOUT)
        self.processes.append(proc)
        return proc

    async def stop(self, proc):
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try: await asyncio.to_thread(proc.wait, 6)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait)

    async def quiet(self, seconds=45):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = foreground()
            if value['policy'] == 0 and not value['bundle'].startswith('com.google.Chrome') and value['idle'] >= 1:
                return value
            await asyncio.sleep(.05)
        raise RuntimeError('No quiet non-Chrome interval for a counted activation')

    async def sample_foreground(self):
        while not self.sampling_stop:
            self.samples.append(foreground())
            await asyncio.sleep(.05)

    async def start_relay(self, label):
        await self.stop(self.relay)
        status_path = self.out / 'relay-status.json'
        status_path.unlink(missing_ok=True)
        self.relay = self.spawn([Path(__file__), '--relay-child', '--profile', self.profile,
            '--port', self.args.port, '--hold', '--exit-with-parent', '--log-path', self.out/'relay.log',
            '--status-path', status_path], label + '-relay',
            {'YESDEV_TEST_TRACE': str(self.out / f'{label}-upstream.jsonl')})
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                data = json.loads(status_path.read_text())
                if data.get('state') == 'listening' and data.get('pid') == self.relay.pid: return
            except (OSError, ValueError): pass
            assert self.relay.poll() is None, 'Relay exited'
            await asyncio.sleep(.05)
        raise RuntimeError('Relay did not start listening')

    def client(self, label, direct=False):
        assert disposable_pid(self.profile) == self.browser_pid
        url = read_endpoint(self.profile).url if direct else self.url
        return Cdp(url, self.trace, label)

    def count_approvals(self):
        text = (self.out / 'engine.log').read_text() if (self.out / 'engine.log').exists() else ''
        return len(re.findall(r'\[ACTION\].*APPROVED via ', text))

    async def settled(self):
        # The grant precedes the watcher's verification log; allow it to finish.
        await asyncio.sleep(.8)

    async def work(self, cdp, label, context=None):
        version = await cdp.call('Browser.getVersion')
        params = {'url': 'about:blank', 'background': True}
        if context: params['browserContextId'] = context
        target = (await cdp.call('Target.createTarget', params))['targetId']
        session = (await cdp.call('Target.attachToTarget', {'targetId': target, 'flatten': True}))['sessionId']
        await cdp.call('Page.enable', session=session)
        url = 'data:text/html,' + quote(f'<title>{label}</title><p>Disposable Yes Dev page</p>')
        await cdp.call('Page.navigate', {'url': url}, session=session)
        value = None
        for _ in range(20):
            value = await cdp.call('Runtime.evaluate', {'expression': '({title:document.title,answer:6*7})',
                                                      'returnByValue': True}, session=session)
            if value.get('result', {}).get('value', {}).get('title') == label: break
            await asyncio.sleep(.05)
        assert value['result']['value'] == {'title': label, 'answer': 42}, value
        await cdp.call('Target.detachFromTarget', {'sessionId': session})
        return {'version': version, 'target': target, 'session': session, 'evaluated': value}

    async def case(self, name, callback, fresh=True):
        print(f'START {name}', flush=True)
        if fresh: await self.start_relay(name)
        self.current = {'name': name, 'started': time.time(), 'passed': False, 'details': {}}
        before = self.count_approvals()
        relay_log=self.out/'relay.log'
        log_offset=len(relay_log.read_text()) if relay_log.exists() else 0
        try:
            await callback(self.current['details'])
            self.current['passed'] = True
        except Exception as exc:
            self.current['error'] = repr(exc)
            self.current['traceback'] = traceback.format_exc()
        finally:
            await self.settled()
            added=relay_log.read_text()[log_offset:] if relay_log.exists() else ''
            warnings=[line for line in added.splitlines() if 'Chrome refused' in line or 'Retiring the held connection' in line]
            resets=[]
            trace_path=self.out/f'{name}-upstream.jsonl'
            if trace_path.exists():
                frames=[json.loads(line) for line in trace_path.read_text().splitlines()]
                for frame in frames:
                    message=frame['message']
                    if (frame['direction']=='sent' and isinstance(message,dict)
                        and message.get('method')=='Target.setAutoAttach' and message.get('params',{}).get('autoAttach') is False):
                        reply=next((f['message'] for f in frames if f['direction']=='received'
                            and f['socket']==frame['socket'] and isinstance(f['message'],dict)
                            and f['message'].get('id')==message.get('id')),None)
                        resets.append({'request':message,'reply':reply})
            self.current['details']['reset_audit']={'unexpected_log_lines':warnings,'autoattach_resets':resets}
            reset_bad=any(r['request']['params'].get('flatten') is not True or r['reply'] is None
                          or 'error' in r['reply'] for r in resets)
            if warnings or reset_bad or (name=='a-sequential' and not resets):
                self.current['passed']=False
                self.current['reset_audit_failed']=True
            self.current.update(ended=time.time(), approvals=self.count_approvals()-before)
            self.cases.append(self.current)
            write_json(self.out/'cases.json', self.cases)
            print(json.dumps({k: self.current[k] for k in ('name', 'passed', 'approvals')}), flush=True)

    async def sequential(self, result):
        from playwright.async_api import async_playwright
        result['previous'] = await self.quiet()
        before = self.count_approvals()
        async with self.client('a-raw-first') as cdp:
            result['first'] = await self.work(cdp, 'YesDev A raw first')
        await self.settled()
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(self.address, timeout=15000)
            session = await browser.new_browser_cdp_session()
            result['playwright_version'] = await session.send('Browser.getVersion')
            page = await browser.contexts[0].new_page()
            await page.goto('data:text/html,<title>YesDev A Playwright</title><p>Test</p>')
            result['playwright_value'] = await page.evaluate('({title:document.title,answer:6*7})')
            await session.detach()
            assert result['playwright_value']['answer'] == 42
        finally: await pw.stop()
        await self.settled()
        async with self.client('a-raw-last') as cdp:
            result['last'] = await self.work(cdp, 'YesDev A raw last')
            result['unexpected_events_before_work'] = cdp.events[:]
            result['unsolicited_replies'] = cdp.unsolicited[:]
            result['inherited_paused_attachments']=[e for e in cdp.events if
                e.get('method')=='Target.attachedToTarget' and e.get('params',{}).get('waitingForDebugger') is True]
            assert not result['inherited_paused_attachments'],result
        await self.settled()
        result['approval_count'] = self.count_approvals()-before
        result['banner_while_held_between_clients'] = banner_snapshot(self.browser_pid)
        assert result['approval_count'] == 1, result['approval_count']

    async def contexts(self, result):
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(self.address, timeout=15000)
            session = await browser.new_browser_cdp_session()
            result['before'] = await session.send('Target.getBrowserContexts')
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto('data:text/html,<title>YesDev B context</title>')
            result['context_value'] = await page.evaluate('6*7')
            result['during'] = await session.send('Target.getBrowserContexts')
            result['default_target'] = await session.send('Target.createTarget',
                                                          {'url':'data:text/html,<title>YesDev B default</title>', 'background':True})
        finally: await pw.stop()
        await self.settled()
        async with self.client('b-observer') as cdp:
            result['after'] = await cdp.call('Target.getBrowserContexts')
            targets = (await cdp.call('Target.getTargets'))['targetInfos']
            result['default_survived'] = result['default_target']['targetId'] in [x['targetId'] for x in targets]
        created = set(result['during']['browserContextIds']) - set(result['before']['browserContextIds'])
        result['created_contexts'] = sorted(created)
        assert created and created.isdisjoint(result['after']['browserContextIds']), result
        assert result['default_survived'], result

    async def parity(self, result, direct=True):
        before=self.count_approvals()
        result['variants'] = []
        for setting in (None, False, True):
            item = {'disposeOnDetach': 'omitted' if setting is None else setting}
            params = {} if setting is None else {'disposeOnDetach': setting}
            async with self.client(f'c-create-{setting}', direct=direct) as cdp:
                item['version'] = await cdp.call('Browser.getVersion')
                context = (await cdp.call('Target.createBrowserContext', params))['browserContextId']
                item['context'] = context
                item['work'] = await self.work(cdp, 'YesDev C parity', context)
            await self.settled()
            async with self.client(f'c-observer-{setting}', direct=direct) as cdp:
                after = await cdp.call('Target.getBrowserContexts')
                item['after_disconnect'] = after
                item['survived'] = context in after['browserContextIds']
                if item['survived']: await cdp.call('Target.disposeBrowserContext', {'browserContextId': context})
            result['variants'].append(item)
        result['via']='direct' if direct else 'held_relay'
        result['survival']=[v['survived'] for v in result['variants']]
        result['matches_measured_native_rule']=result['survival']==[True,True,False]
        await self.settled()
        result['approval_count']=self.count_approvals()-before
        assert result['matches_measured_native_rule'],result
        if not direct: assert result['approval_count']==1,result

    async def relay_parity(self,result):
        await self.parity(result,direct=False)

    async def close_clients(self, result):
        from playwright.async_api import async_playwright
        before = self.count_approvals()
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(self.address, timeout=15000)
            session = await browser.new_browser_cdp_session()
            result['playwright_version'] = await session.send('Browser.getVersion')
            await browser.close()
            result['playwright_close_returned'] = True
        finally: await pw.stop()
        result['pid_after_playwright_close'] = disposable_pid(self.profile)
        await self.settled()
        async with self.client('e-raw-close') as cdp:
            result['raw_version'] = await cdp.call('Browser.getVersion')
            result['raw_close_reply'] = await cdp.call('Browser.close')
            await asyncio.wait_for(cdp.ws.wait_closed(), 5)
            result['raw_close_code'] = cdp.ws.close_code
        result['pid_after_raw_close'] = disposable_pid(self.profile)
        await self.settled()
        async with self.client('e-reuse') as cdp:
            result['reuse_version'] = await cdp.call('Browser.getVersion')
        await self.settled()
        result['approval_count'] = self.count_approvals()-before
        assert result['raw_close_code'] == 1000 and result['approval_count'] == 1, result

    async def puppeteer(self, result):
        if self.args.puppeteer_module is None: raise RuntimeError('Supply --puppeteer-module for the real tool check')
        before = self.count_approvals()
        command = ['node', str(Path(__file__).with_name('hold_puppeteer_probe.mjs')),
                   str(self.args.puppeteer_module), self.address, 'YesDev F Puppeteer']
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try: stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
        except asyncio.TimeoutError:
            process.kill(); await process.wait(); raise
        (self.out/'puppeteer-stdout.log').write_bytes(stdout)
        (self.out/'puppeteer-stderr.log').write_bytes(stderr)
        result['exit_code'] = process.returncode
        assert process.returncode == 0, stderr.decode()
        result['tool_result'] = json.loads(stdout)
        await self.settled()
        async with self.client('f-reuse') as cdp:
            result['reuse_version'] = await cdp.call('Browser.getVersion')
            await asyncio.sleep(.2)
            result['stray_events'] = cdp.events[:]
            result['stray_replies'] = cdp.unsolicited[:]
            result['reuse_page_work']=await self.work(cdp,'YesDev F successor page')
            result['inherited_paused_attachments']=[e for e in cdp.events if
                e.get('method')=='Target.attachedToTarget' and e.get('params',{}).get('waitingForDebugger') is True]
            assert not result['inherited_paused_attachments'],result
        await self.settled()
        result['approval_count'] = self.count_approvals()-before
        assert result['approval_count'] == 1, result

    async def concurrent(self, result):
        before = self.count_approvals()
        first = await self.client('g-first-held').__aenter__()
        second = None
        try:
            result['first_version'] = await first.call('Browser.getVersion')
            result['previous_for_second'] = await self.quiet()
            second = await self.client('g-second-own').__aenter__()
            result['second_version'] = await second.call('Browser.getVersion')
            result['work'] = await asyncio.gather(self.work(first, 'YesDev G first'), self.work(second, 'YesDev G second'))
            await first.__aexit__(); first = None
            await self.settled()
            async with self.client('g-third-reuse') as third:
                result['third_version'] = await third.call('Browser.getVersion')
                result['second_still_works'] = await second.call('Browser.getVersion')
            await self.settled()
            result['approval_count'] = self.count_approvals()-before
            assert result['approval_count'] == 2, result
        finally:
            if first is not None: await first.__aexit__()
            if second is not None: await second.__aexit__()

    async def restart_browser(self):
        pid = disposable_pid(self.profile)
        assert pid == self.browser_pid
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic()+10
        while subprocess.run(['ps','-p',str(pid),'-o','pid='],capture_output=True).stdout.strip():
            if time.monotonic() > deadline: raise RuntimeError('Disposable Chrome did not stop')
            await asyncio.sleep(.05)
        (self.profile/'DevToolsActivePort').unlink(missing_ok=True)
        subprocess.run(['open','-gna','Google Chrome','--args','--user-data-dir='+str(self.profile),
                        '--no-first-run','about:blank'],check=True)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            try:
                self.browser_pid=disposable_pid(self.profile)
                return
            except (OSError,ValueError,AssertionError,subprocess.CalledProcessError): await asyncio.sleep(.1)
        raise RuntimeError('Restarted Chrome endpoint unavailable')

    async def queue(self, result):
        before_text=(self.out/'engine.log').read_text()
        async def one(index):
            async with self.client(f'queue-{index}',direct=True) as cdp:
                return await cdp.call('Browser.getVersion')
        result['versions']=await asyncio.gather(*(one(i) for i in range(3)))
        await self.settled()
        added=(self.out/'engine.log').read_text()[len(before_text):]
        result['counts']={
            'axpress':len(re.findall(r'APPROVED via AXPress',added)),
            'failed':len(re.findall(r'\bFAILED:',added)),
            'tab':len(re.findall(r'(?:posted|sent|via).*\bTab\b',added,re.I)),
            'space':len(re.findall(r'(?:posted|sent|via).*\bSpace\b',added,re.I)),
        }
        result['engine_excerpt']=added
        assert result['counts']=={'axpress':3,'failed':0,'tab':0,'space':0},result

    def window_snapshot(self):
        import Quartz
        from ApplicationServices import AXValueGetValue,kAXValueCGPointType
        import watcher_mac as watcher
        pid=disposable_pid(self.profile)
        windows=[]
        def has_sheet(window):
            # Chrome 154 exposes this as AXGroup/AXApplicationAlertDialog,
            # not necessarily AXSheet. Match the same consent shapes as the
            # production watcher, confined to native window-level children.
            candidates=[window]+(watcher._attr(window,'AXSheets') or [])+(watcher._attr(window,'AXChildren') or [])
            for element in candidates:
                label=watcher._element_label(element)
                is_dialog=watcher._is_dialog_role(element)
                heading=is_dialog and not label and watcher._has_dialog_heading(element)
                if watcher._is_consent_host(label,heading,is_dialog): return True
            return False
        for window in watcher._attr(watcher.AXUIElementCreateApplication(pid),'AXWindows') or []:
            if watcher._attr(window,'AXSubrole')!='AXStandardWindow': continue
            position=watcher._attr(window,'AXPosition')
            ok,point=AXValueGetValue(position,kAXValueCGPointType,None)
            if ok: windows.append({'title':watcher._attr(window,'AXTitle'),'x':point.x,'y':point.y,
                                   'sheet':has_sheet(window)})
        order=[]
        for item in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,0):
            if item.get('kCGWindowOwnerPID')!=pid or item.get('kCGWindowLayer')!=0: continue
            bounds=dict(item['kCGWindowBounds'])
            match=next((w for w in windows if abs(w['x']-bounds['X'])<1 and abs(w['y']-bounds['Y'])<1),None)
            if match: order.append({'id':item['kCGWindowNumber'],'bounds':bounds,**match})
        return {'time':time.time(),'foreground':foreground(),'windows_front_to_back':order}

    async def background_window(self,result):
        if self.args.reuse_background_windows:
            result['existing_windows']=self.window_snapshot()
            assert len(result['existing_windows']['windows_front_to_back'])==2,'Expected exactly two existing disposable windows'
            assert not any(w['sheet'] for w in result['existing_windows']['windows_front_to_back']), 'An existing consent dialog can belong to an expired client; restart disposable Chrome before this test'
        else:
            async with self.client('window-setup',direct=True) as setup:
                result['setup_version']=await setup.call('Browser.getVersion')
                existing=self.window_snapshot()['windows_front_to_back']
                left,top=next((x,y) for x,y in [(240,180),(80,60),(380,260)]
                    if all(abs(w['x']-x)>1 or abs(w['y']-y)>1 for w in existing))
                result['extra_window_geometry']={'left':left,'top':top}
                result['extra_window']=await setup.call('Target.createTarget',{
                    'url':'data:text/html,<title>YesDev Window Order</title><p>Disposable window-order fixture</p>',
                    'newWindow':True,'background':True,'left':left,'top':top,'width':850,'height':600})
            await self.settled()
        await self.stop(self.engine)
        result['engine_approvals_before']=self.count_approvals()
        before_log=(self.out/'engine.log').read_text() if (self.out/'engine.log').exists() else ''
        async def pending_grant():
            async with connect(read_endpoint(self.profile).url,proxy=None,open_timeout=self.args.ui_timeout+20,
                               ping_interval=None) as ws:
                await ws.send(json.dumps({'id':1,'method':'Browser.getVersion'}))
                while True:
                    reply=json.loads(await ws.recv())
                    if reply.get('id')==1:return reply
        pending=asyncio.create_task(pending_grant())
        write_json(self.out/'ui-request.json',{'action':'arrange_disposable_background_consent_window',
            'browser_pid':self.browser_pid,'instructions':'Bring the disposable Chrome window without the consent dialog above the one with the dialog, then click a non-Chrome app and keep hands off. The watcher starts automatically when CGWindowList and AX confirm the arrangement.'})
        print('UI_STEP Put the consent sheet behind the OTHER disposable Chrome window, then switch to a non-Chrome app. Detection is automatic.',flush=True)
        deadline=time.monotonic()+self.args.ui_timeout
        try:
            while time.monotonic()<deadline:
                snapshot=self.window_snapshot()
                write_json(self.out/'window-setup-current.json',snapshot)
                windows=snapshot['windows_front_to_back']
                front=snapshot['foreground']
                if (len(windows)==2 and not windows[0]['sheet'] and sum(w['sheet'] for w in windows)==1
                    and not front['bundle'].startswith('com.google.Chrome') and front['idle']>=2):
                    result['before']=snapshot
                    break
                if pending.done(): raise RuntimeError('Consent completed before background arrangement; no valid trial')
                await asyncio.sleep(.15)
            else: raise RuntimeError('Required native window arrangement not observed; no approval attempted')
            self.engine=self.spawn([Path(__file__),'--watcher-child','--diagnostics','--exit-with-parent',
                                    '--log-path',self.out/'engine.log'],'window-watcher',
                                   {'YESDEV_TEST_PROFILE':str(self.profile)})
            result['reply']=await asyncio.wait_for(pending,25)
            await self.settled()
            result['after']=self.window_snapshot()
            added=(self.out/'engine.log').read_text()[len(before_log):]
            result['engine_excerpt']=added
            result['window_order_unchanged']=[w['id'] for w in result['before']['windows_front_to_back']]==[w['id'] for w in result['after']['windows_front_to_back']]
            result['all_consent_dialogs_gone']=not any(w['sheet'] for w in result['after']['windows_front_to_back'])
            assert 'product' in result['reply'].get('result',{}),result
            assert (len(re.findall('APPROVED via AXPress',added))==1 and result['window_order_unchanged']
                    and result['all_consent_dialogs_gone'] and 'FAILED:' not in added
                    and not re.search(r'sent (?:Tab|Space) to pid',added)),result
        finally:
            pending.cancel()
            with suppress(asyncio.CancelledError):await pending

    async def concurrent_close(self,result):
        first=await self.client('close-held-owner').__aenter__()
        second=None
        try:
            result['held_version']=await first.call('Browser.getVersion')
            second=await self.client('close-concurrent-owner').__aenter__()
            result['concurrent_version']=await second.call('Browser.getVersion')
            await self.settled()
            result['verified_disposable_pid']=disposable_pid(self.profile)
            result['close_reply']=await second.call('Browser.close')
            await asyncio.wait_for(second.ws.wait_closed(),5)
            await asyncio.sleep(2)
            result['chrome_process_alive']=bool(subprocess.run(
                ['ps','-p',str(self.browser_pid),'-o','pid='],capture_output=True).stdout.strip())
            result['held_socket_close_code']=first.ws.close_code
            result['concurrent_socket_close_code']=second.ws.close_code
            assert result['chrome_process_alive'],'Concurrent Browser.close exited disposable Chrome'
            assert result['concurrent_socket_close_code']==1000,result
            result['held_survivor_page_work']=await self.work(first,'YesDev held survivor after concurrent close')
        finally:
            await first.__aexit__()
            if second is not None: await second.__aexit__()

    async def restart(self, result):
        before=self.count_approvals()
        async with self.client('h-before') as cdp:
            result['before_version']=await cdp.call('Browser.getVersion')
            # The CDP grant precedes the engine's verified APPROVED log. Do
            # not deliberately kill Chrome in the middle of that verification.
            await self.settled()
            result['old_pid']=self.browser_pid
            await self.restart_browser()
            await asyncio.wait_for(cdp.ws.wait_closed(),5)
            result['old_close_code']=cdp.ws.close_code
        result['new_pid']=self.browser_pid
        async with self.client('h-after') as cdp:
            result['after_version']=await cdp.call('Browser.getVersion')
        await self.settled()
        async with self.client('h-reuse') as cdp:
            result['reuse_version']=await cdp.call('Browser.getVersion')
        await self.settled()
        result['approval_count']=self.count_approvals()-before
        assert result['approval_count']==2, result

    async def idle(self, result):
        async with self.client('i-idle') as cdp:
            result['initial_version']=await cdp.call('Browser.getVersion')
            result['banner_at_start']=banner_snapshot(self.browser_pid)
            await self.settled()
            samples=[cpu_sample(self.relay.pid)]
            deadline=time.monotonic()+self.args.idle_seconds
            while time.monotonic()<deadline:
                await asyncio.sleep(min(30,max(.01,deadline-time.monotonic())))
                assert cdp.ws.close_code is None, 'Idle client dropped'
                samples.append(cpu_sample(self.relay.pid))
                print(f'IDLE {round(samples[-1]["t"]-samples[0]["t"])} seconds, socket open',flush=True)
            result['final_version']=await cdp.call('Browser.getVersion')
            result['samples']=samples
            elapsed=samples[-1]['t']-samples[0]['t']
            cpu=samples[-1]['cpu_seconds']-samples[0]['cpu_seconds']
            result['duration_seconds']=elapsed
            result['average_relay_core_percent']=100*cpu/elapsed
            result['banner_at_end']=banner_snapshot(self.browser_pid)
        await self.settled()
        result['banner_between_clients']=banner_snapshot(self.browser_pid)

    async def autoattach(self,result):
        from playwright.async_api import async_playwright
        pw=await async_playwright().start()
        try:
            browser=await pw.chromium.connect_over_cdp(self.address,timeout=15000)
            page=await browser.contexts[0].new_page()
            await page.goto('data:text/html,<title>YesDev Manual Tab Fixture</title><p>Disposable Chrome</p>')
            result['playwright_value']=await page.evaluate('6*7')
        finally: await pw.stop()
        await self.settled()
        async with self.client('d-autoattach') as cdp:
            result['version']=await cdp.call('Browser.getVersion')
            result['targets_before_native_step']=await cdp.call('Target.getTargets')
            result['set_autoattach']=await cdp.call('Target.setAutoAttach',
                {'autoAttach':True,'waitForDebuggerOnStart':True,'flatten':True})
        await asyncio.sleep(2.5)
        request={'action':'open_new_tab_in_disposable_chrome_ui','browser_pid':self.browser_pid,
                 'url':f'http://127.0.0.1:{self.http.server_port}/manual-load',
                 'result_path':str(self.out/'ui-result.json')}
        write_json(self.out/'ui-request.json',request)
        print('UI_STEP '+json.dumps(request),flush=True)
        deadline=time.monotonic()+self.args.ui_timeout
        while time.monotonic()<deadline:
            if (self.out/'ui-result.json').exists(): break
            await asyncio.sleep(.2)
        else: raise RuntimeError('Native new-tab action was not completed; no synthetic pass recorded')
        result['ui_evidence']=json.loads((self.out/'ui-result.json').read_text())
        assert result['ui_evidence'].get('completed'),result
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and not any(x['path']=='/script-executed' for x in self.fixture_hits):
            await asyncio.sleep(.1)
        result['fixture_hits']=self.fixture_hits[:]
        result['script_executed']=any(x['path']=='/script-executed' for x in self.fixture_hits)
        assert result['script_executed'],'Native new tab did not execute its load script after owner disconnected'
        async with self.client('d-native-result-observer') as cdp:
            result['targets_after_native_step']=await cdp.call('Target.getTargets')
        previous={t['targetId'] for t in result['targets_before_native_step']['targetInfos']}
        result['new_native_targets']=[t for t in result['targets_after_native_step']['targetInfos']
            if t['targetId'] not in previous and t.get('url')==request['url']]
        assert result['new_native_targets'],'Loaded URL was not a new tab in the disposable Chrome'

    async def run(self):
        write_json(self.out/'run.json', {'started':self.started.isoformat(),'browser_pid':self.browser_pid,
           'profile':str(self.profile),'repo_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
           'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('cdp_relay.py','relay_mac.py','watcher_mac.py','early_focus_guard_mac.py')},
           'instrumentation':'Upstream send/recv recording only; watcher discovery restricted to verified disposable profile.'})
        if not self.args.reuse_background_windows:
            self.engine=self.spawn([Path(__file__),'--watcher-child','--diagnostics','--exit-with-parent',
                                    '--log-path',self.out/'engine.log'], 'watcher',{'YESDEV_TEST_PROFILE':str(self.profile)})
        sampler=asyncio.create_task(self.sample_foreground())
        sequence=[('a-sequential',self.sequential),('b-contexts',self.contexts),('c-direct-parity',self.parity),
                  ('e-browser-close',self.close_clients),('f-puppeteer',self.puppeteer),
                  ('g-concurrent',self.concurrent),('h-restart',self.restart),
                  ('i-idle-and-banner',self.idle),('d-native-tab-after-autoattach',self.autoattach)]
        if self.args.only: sequence=[x for x in sequence if x[0] in self.args.only]
        extras={'c-relay-parity':self.relay_parity,'q-direct-queue':self.queue,'w-background-window':self.background_window,
                'x-concurrent-close':self.concurrent_close}
        sequence += [(name,callback) for name,callback in extras.items() if name in (self.args.only or [])]
        if not sequence: raise ValueError('No recognized scenarios selected')
        try:
            for name,callback in sequence: await self.case(name,callback)
        finally:
            for proc in reversed(self.processes): await self.stop(proc)
            self.sampling_stop=True
            await sampler
            self.http.shutdown()
            self.http.server_close()
            for handle in self.handles: handle.close()
            write_json(self.out/'foreground.json',self.samples)
            logs=subprocess.run(['/usr/bin/log','show','--info','--debug','--style','compact',
                '--start',(self.started-timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S'),
                '--end',(datetime.now()+timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S'),
                '--predicate','process == "WindowServer" AND eventMessage CONTAINS "Deferring events from frontmost process"'],
                capture_output=True,text=True,timeout=30)
            (self.out/'windowserver.log').write_text(logs.stdout)
            (self.out/'windowserver-stderr.log').write_text(logs.stderr)
            try: remaining_browser_pid=disposable_pid(self.profile)
            except (OSError,ValueError,AssertionError,subprocess.CalledProcessError): remaining_browser_pid=None
            write_json(self.out/'cleanup.json',{'owned_children_stopped':all(p.poll() is not None for p in self.processes),
                'new_private_runtimes_remaining':[str(p) for p in set(Path('/tmp').glob('yesdev-relay-*'))-self.initial_runtimes],
                'disposable_browser_left_running':remaining_browser_pid})
        return 0 if all(x['passed'] for x in self.cases) else 1


def relay_child():
    import cdp_relay
    import relay_mac
    trace=Path(os.environ['YESDEV_TEST_TRACE'])
    class TraceConnection(ClientConnection):
        def record(self,direction,payload):
            try: message=json.loads(payload)
            except (ValueError,TypeError): message=str(payload)
            with trace.open('a') as out:
                out.write(json.dumps({'t':time.time(),'socket':str(self.id),'direction':direction,'message':message})+'\n')
        async def send(self,message,*args,**kwargs):
            self.record('sent',message)
            return await super().send(message,*args,**kwargs)
        async def recv(self,*args,**kwargs):
            message=await super().recv(*args,**kwargs)
            self.record('received',message)
            return message
    cdp_relay.connect=functools.partial(cdp_relay.connect,create_connection=TraceConnection)
    sys.argv=[str(ROOT/'relay_mac.py'),*sys.argv[2:]]
    raise SystemExit(relay_mac.main())


def watcher_child():
    import watcher_mac
    profile=Path(os.environ['YESDEV_TEST_PROFILE'])
    def only_disposable(_self):
        try: return [disposable_pid(profile)]
        except (OSError,ValueError,AssertionError,subprocess.CalledProcessError): return []
    watcher_mac.Engine.chrome_pids=only_disposable
    watcher_mac.Engine.workspace_chrome_pids=only_disposable
    sys.argv=[str(ROOT/'watcher_mac.py'),*sys.argv[2:]]
    raise SystemExit(watcher_mac.main())


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--relay-child': relay_child()
    if len(sys.argv)>1 and sys.argv[1]=='--watcher-child': watcher_child()
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile',type=Path,required=True)
    p.add_argument('--browser-pid',type=int,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--port',type=int,default=19333)
    p.add_argument('--puppeteer-module',type=Path)
    p.add_argument('--idle-seconds',type=float,default=600)
    p.add_argument('--ui-timeout',type=float,default=120)
    p.add_argument('--reuse-background-windows',action='store_true',help='Resume the native window check using exactly two existing disposable windows; no setup approval')
    p.add_argument('--only',action='append')
    args=p.parse_args()
    if args.reuse_background_windows and args.only!=['w-background-window']:p.error('--reuse-background-windows requires only w-background-window')
    if not 1024<=args.port<=65535 or args.idle_seconds<=0 or args.ui_timeout<=0: p.error('Invalid port or duration')
    raise SystemExit(asyncio.run(LiveRun(args).run()))
