"""Record normal MCP, Playwright and Puppeteer work against the real thin relay.

Requires record_cdp_traffic.py's verified disposable Chrome. All tools connect
to one relay with hold off. The label is captured when each downstream opens;
connections are initiated sequentially, then normal work may run concurrently.
No production multiplexing implementation is added by this test.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, quote, urlsplit

from record_cdp_traffic import LaunchRun, ROOT, Cdp, append, disposable_pid, write_json, windowserver


class Stdio:
    def __init__(self, owner, label, command, mcp=False):
        self.owner, self.label, self.command, self.mcp = owner, label, command, mcp
        self.next_id, self.pending = 0, {}
        self.process = self.reader = None
        self.path = owner.case_out / (label + '-tools.jsonl')
        self.page_id = None
        self.schemas = {}

    async def start(self):
        stderr = (self.owner.case_out / (self.label + '-stderr.log')).open('w')
        self.owner.handles.append(stderr)
        self.process = await asyncio.create_subprocess_exec(*self.command, cwd=ROOT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
            env={**os.environ, 'CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS': '1'})
        self.owner.event('tool_process_started', client=self.label, pid=self.process.pid, command=self.command)
        self.reader = asyncio.create_task(self.read())
        self.owner.tool_processes.append(self)
        if self.mcp:
            await self.call('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                'clientInfo': {'name': 'yesdev-live-recon', 'version': '1.0'}})
            await self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            listed = await self.call('tools/list', {})
            self.schemas = {tool['name']: tool['inputSchema'] for tool in listed['tools']}
        return self

    async def read(self):
        try:
            while raw := await self.process.stdout.readline():
                try:
                    message = json.loads(raw)
                except ValueError:
                    append(self.path, {'direction': 'stdout_non_json', 'text': raw.decode(errors='replace')})
                    continue
                append(self.path, {'direction': 'received', 'message': message})
                future = self.pending.pop(message.get('id'), None)
                if future is not None and not future.done():
                    future.set_result(message)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(self.label + ' exited'))

    async def send(self, message):
        append(self.path, {'direction': 'sent', 'message': message})
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        await self.process.stdin.drain()

    async def call(self, method, params=None, timeout=40):
        self.next_id += 1
        message = {'id': self.next_id, 'method': method, 'params': params or {}}
        if self.mcp:
            message['jsonrpc'] = '2.0'
        future = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        await self.send(message)
        reply = await asyncio.wait_for(future, timeout)
        if 'error' in reply:
            raise RuntimeError(f'{self.label} {method}: {reply["error"]}')
        return reply.get('result')

    async def tool(self, name, arguments=None, timeout=40):
        arguments = dict(arguments or {})
        if 'pageId' in self.schemas[name].get('required', []) and 'pageId' not in arguments:
            assert self.page_id is not None
            arguments['pageId'] = self.page_id
        result = await self.call('tools/call', {'name': name, 'arguments': arguments}, timeout)
        if result.get('isError'):
            raise RuntimeError(f'{self.label} {name}: {result}')
        return result

    async def close(self, kill=False):
        if not self.process or self.process.returncode is not None:
            return
        self.owner.event('tool_exit_requested', client=self.label, kill=kill)
        if kill:
            self.process.kill()
        else:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 8)
        except asyncio.TimeoutError:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        await self.reader
        self.owner.event('tool_process_exited', client=self.label, code=self.process.returncode)


def text_content(result):
    return '\n'.join(c.get('text', '') for c in result.get('content', []) if c.get('type') == 'text')


class Recon(LaunchRun):
    def __init__(self, args):
        super().__init__(args)
        self.hold = False
        self.tool_processes = []
        self.fixture_hits = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hit = {'path': self.path, 'time': time.time()}
                owner.fixture_hits.append(hit)
                append(owner.out / 'http-hits.jsonl', hit)
                if self.path.startswith('/script-executed'):
                    body = b'ok'
                else:
                    title = parse_qs(urlsplit(self.path).query).get('label', ['YesDev Fixture'])[0]
                    body = ('<!doctype html><meta charset="utf-8"><title>' + title + '</title>'
                        '<h1>YesDev traffic fixture</h1><button onclick="window.clicks++;document.querySelector(\'output\').textContent=window.clicks">Count click</button>'
                        '<output>0</output><a href="?label=linked-fixture">A local link</a>'
                        '<script>window.clicks=0;window.yesdevStarted=Date.now();fetch("/script-executed?label="+encodeURIComponent(document.title));</script>').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                with suppress(BrokenPipeError):
                    self.wfile.write(body)
            def log_message(self, *_):
                pass
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def fixture(self, label):
        return f'http://127.0.0.1:{self.http.server_port}/fixture?label={quote(label)}'

    def puppeteer_module(self):
        package = self.args.tooling / 'node_modules/puppeteer-core'
        return package / json.loads((package / 'package.json').read_text())['main']

    def label_next(self, label):
        write_json(self.case_out / 'client-label.json', {'label': label})
        self.event('next_connection_label', client=label)

    async def mcp(self, label, package):
        self.label_next(label)
        command = ['node', str(self.args.tooling / 'node_modules' / package / 'build/src/bin/chrome-devtools-mcp.js'),
                   '--browserUrl', self.address, '--no-usage-statistics', '--no-performance-crux']
        client = await Stdio(self, label, command, mcp=True).start()
        # list_tools and initialize should not be assumed to connect to Chrome.
        try:
            await client.tool('list_pages')
        except RuntimeError as exc:
            if 'No page selected' not in str(exc):
                raise
            self.event('initial_list_pages_no_selection', client=label, error=str(exc))
        return client

    async def mcp_new(self, client, label, background=None, isolated=None):
        params = {'url': self.fixture(label)}
        if background is not None:
            params['background'] = background
        if isolated is not None:
            params['isolatedContext'] = isolated
        self.event('create_page_start', client=client.label, params=params)
        result = await client.tool('new_page', params)
        text = text_content(result)
        matches = re.findall(r'(?m)^\s*(\d+):\s+([^\n]+)', text)
        matching = [(int(index), value) for index, value in matches if label in value]
        assert matching, text
        client.page_id = matching[-1][0]
        self.event('create_page_complete', client=client.label, page_id=client.page_id)
        return client.page_id

    async def mcp_work(self, client, label):
        await client.tool('navigate_page', {'type': 'url', 'url': self.fixture(label)})
        snapshot = await client.tool('take_snapshot')
        text = text_content(snapshot)
        button = re.search(r'uid=([^\s]+)\s+button\s+"Count click"', text)
        assert button, text
        await client.tool('click', {'uid': button.group(1), 'includeSnapshot': True})
        result = await client.tool('evaluate_script', {
            'function': '() => ({title:document.title,clicks:window.clicks,answer:6*7})'})
        assert '42' in text_content(result), result
        self.event('mcp_work_complete', client=client.label, result=result)
        return result

    async def mcp_solo(self, package):
        client = await self.mcp(package, package)
        first = await self.mcp_new(client, package + '-first')
        await self.mcp_work(client, package + '-work-0')
        for index in range(1, 3):
            await asyncio.sleep(self.args.work_pause)
            await self.mcp_work(client, package + '-work-' + str(index))
        await client.tool('close_page', {'pageId': first})
        survivor = await self.mcp_new(client, package + '-left-open-on-exit', background=True)
        await client.close()
        await asyncio.sleep(1)
        self.label_next(package + '-exit-observer')
        async with Cdp(self.url, self.case_out / 'observer.jsonl', package + '-exit-observer') as raw:
            targets = await raw.call('Target.getTargets')
        self.event('targets_after_mcp_exit', tool_page_id=survivor, targets=targets)

    async def playwright_solo(self):
        from playwright.async_api import async_playwright
        self.label_next('playwright')
        self.event('playwright_connect_start')
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(self.address, timeout=25000)
            self.event('playwright_connected')
            page = await browser.contexts[0].new_page()
            for index in range(3):
                await page.goto(self.fixture('playwright-' + str(index)))
                await page.get_by_role('button', name='Count click').click()
                result = await page.evaluate('({title:document.title,clicks:window.clicks,answer:6*7})')
                self.event('playwright_work', result=result)
                if index < 2:
                    await asyncio.sleep(self.args.work_pause)
            context = await browser.new_context()
            private_page = await context.new_page()
            await private_page.goto(self.fixture('playwright-private'))
            await private_page.close()
            await context.close()
            await page.close()
            self.event('playwright_close_start')
            await browser.close()
            self.event('playwright_close_complete')
        finally:
            await pw.stop()

    async def puppeteer_solo(self):
        self.label_next('puppeteer')
        module = self.puppeteer_module()
        client = await Stdio(self, 'puppeteer', ['node', str(Path(__file__).with_name('traffic_puppeteer_client.mjs')),
            str(module), self.address]).start()
        await client.call('connect')
        page = await client.call('new_page', {'url': self.fixture('puppeteer-first')})
        for index in range(3):
            await client.call('work', {'id': page['id'], 'url': self.fixture('puppeteer-' + str(index))})
            if index < 2:
                await asyncio.sleep(self.args.work_pause)
        await client.call('new_page', {'url': self.fixture('puppeteer-isolated'), 'isolated': True})
        await client.call('close_page', {'id': page['id']})
        await client.call('disconnect')
        await client.close()

    async def list_foreign(self, client, expected_labels):
        result = await client.tool('list_pages')
        content = text_content(result)
        self.event('foreign_pages_listed', client=client.label, expected=expected_labels,
                   visible={label: label in content for label in expected_labels}, result=result)
        return result

    async def concurrent(self, mixed=False):
        from playwright.async_api import async_playwright
        one = await self.mcp('mcp-a', 'mcp170')
        await self.mcp_new(one, 'mcp-a-own')
        two = await self.mcp('mcp-b', 'mcp170')
        await self.mcp_new(two, 'mcp-b-own', background=True)
        pw = browser = page = None
        try:
            if mixed:
                self.label_next('playwright-mixed')
                pw = await async_playwright().start()
                browser = await pw.chromium.connect_over_cdp(self.address, timeout=25000)
                page = await browser.contexts[0].new_page()
                await page.goto(self.fixture('playwright-mixed-own'))
                self.event('mixed_playwright_page_created', value=await page.evaluate('document.title'))
            for index in range(3):
                await asyncio.gather(self.mcp_work(one, f'mcp-a-work-{index}'),
                                     self.mcp_work(two, f'mcp-b-work-{index}'))
                if page:
                    await page.goto(self.fixture('playwright-mixed-' + str(index)))
                    await page.get_by_role('button', name='Count click').click()
                    self.event('mixed_playwright_work', value=await page.evaluate('({clicks:window.clicks,title:document.title})'))
                if index < 2:
                    await asyncio.sleep(min(self.args.work_pause, 20))
            await self.list_foreign(one, ['mcp-b-work-2'])
            await self.list_foreign(two, ['mcp-a-work-2'])
            self.event('one_client_leaves_midwork', client=one.label)
            await one.close()
            await self.mcp_work(two, 'mcp-b-after-a-exit')
            if page:
                await page.goto(self.fixture('playwright-after-a-exit'))
                assert await page.evaluate('6*7') == 42
            self.event('remaining_clients_worked_after_exit', clients=[two.label] + (['playwright-mixed'] if page else []))
            await two.close()
            if browser:
                await browser.close()
        finally:
            if pw:
                await pw.stop()

    async def reconnect(self, package):
        client = await self.mcp(package + '-reconnect', package)
        await self.mcp_new(client, package + '-before-drop')
        await self.mcp_work(client, package + '-before-drop-work')
        (self.case_out / ('drop-' + client.label)).touch()
        self.event('controlled_drop_requested', client=client.label)
        await self.wait_for(lambda: self.status().get('connections') == 0, 10)
        await asyncio.sleep(5)
        self.event('after_drop_without_tool_calls', approvals=self.approvals(), status=self.status())
        for attempt in range(2):
            try:
                result = await client.tool('list_pages')
                self.event('post_drop_tool_succeeded', attempt=attempt + 1, result=result)
                break
            except Exception as exc:
                self.event('post_drop_tool_failed', attempt=attempt + 1, error=repr(exc))
        await self.mcp_new(client, package + '-after-drop')
        await self.mcp_work(client, package + '-after-drop-work')
        await client.close()

    async def creation_focus(self, latest_only=False):
        from playwright.async_api import async_playwright
        client = await self.mcp('mcp-create', 'mcplatest' if latest_only else 'mcp170')
        # Tool background options may map to Page.bringToFront rather than to
        # Target.createTarget.background. Keep the actual trace as the judge.
        for label, background, isolated in [('default', None, None), ('background', True, None),
                                             ('foreground', False, None), ('isolated', True, 'yesdev-isolated')]:
            await self.textedit()
            await self.mcp_new(client, 'mcp-create-' + label, background, isolated)
            await asyncio.sleep(1)
        await client.close()
        if latest_only:
            return
        self.label_next('pw-create')
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(self.address, timeout=25000)
            await self.textedit()
            self.event('pw_default_create_start')
            page = await browser.contexts[0].new_page()
            await page.goto(self.fixture('pw-create-default'))
            self.event('pw_default_create_complete')
            context = await browser.new_context()
            await self.textedit()
            self.event('pw_context_create_start')
            page = await context.new_page()
            await page.goto(self.fixture('pw-create-isolated'))
            self.event('pw_context_create_complete')
            await context.close()
            await browser.close()
        self.label_next('pptr-create')
        module = self.puppeteer_module()
        client = await Stdio(self, 'pptr-create', ['node', str(Path(__file__).with_name('traffic_puppeteer_client.mjs')),
            str(module), self.address]).start()
        await client.call('connect')
        for isolated in (False, True):
            await self.textedit()
            self.event('puppeteer_create_start', isolated=isolated)
            await client.call('new_page', {'url': self.fixture('pptr-create-' + str(isolated)), 'isolated': isolated})
            self.event('puppeteer_create_complete', isolated=isolated)
        await client.call('disconnect')
        await client.close()

    def delayed_resumes(self, after):
        result = []
        for path in self.case_out.glob('trace-*.jsonl'):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                frame = json.loads(line)
                if frame['t'] >= after and frame['direction'] == 'resume_delivery_delayed':
                    result.append({'trace': str(path), 'line': number, **frame})
        return result

    async def release_probe(self, label, one, two, pending, after):
        await self.wait_for(lambda: len({f['client'] for f in self.delayed_resumes(after)}) == 2, 15)
        self.event('both_clients_resume_blocked', probe=label, frames=self.delayed_resumes(after),
                   hits=[h for h in self.fixture_hits if h['time'] >= after])
        write_json(self.case_out / 'resume-released.json', {'clients': [one.label]})
        self.event('first_client_resume_released', probe=label, client=one.label)
        await asyncio.sleep(3)
        self.event('after_first_resume_only', probe=label,
                   hits=[h for h in self.fixture_hits if h['time'] >= after],
                   creator_tool_done=pending.done() if pending else None)
        write_json(self.case_out / 'resume-released.json', {'clients': [one.label, two.label]})
        self.event('second_client_resume_released', probe=label, client=two.label)
        if pending:
            await asyncio.wait_for(pending, 30)
        await asyncio.sleep(2)
        self.event('after_both_resumes', probe=label,
                   hits=[h for h in self.fixture_hits if h['time'] >= after])
        (self.case_out / 'resume-gate.json').unlink(missing_ok=True)

    async def autoattach_probe(self, native=False):
        one = await self.mcp('mcp-a', 'mcp170')
        await self.mcp_new(one, 'mcp-a-probe-setup')
        two = await self.mcp('mcp-b', 'mcp170')
        await self.mcp_new(two, 'mcp-b-probe-setup', background=True)
        after = time.time()
        write_json(self.case_out / 'resume-released.json', {'clients': []})
        write_json(self.case_out / 'resume-gate.json', {'clients': [one.label, two.label], 'after': after})
        pending = None
        label = 'native-new-tab' if native else 'client-created'
        try:
            if native:
                await self.ui('open_native_tab_with_two_real_mcp_clients', browser_pid=self.pid,
                    url=self.fixture(label), instructions='Open a new native tab in the verified disposable browser and navigate to the fixture URL.')
            else:
                pending = asyncio.create_task(self.mcp_new(one, label, background=True))
            await self.release_probe(label, one, two, pending, after)
            await self.list_foreign(one, [label])
            await self.list_foreign(two, [label])
        finally:
            (self.case_out / 'resume-gate.json').unlink(missing_ok=True)
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await one.close()
            await two.close()

    async def raw_resume_probe(self, tabs=False, navigate=False):
        self.label_next('raw-a')
        one = await Cdp(self.url, self.case_out / 'raw-control.jsonl', 'raw-a').__aenter__()
        two = None
        try:
            await one.call('Browser.getVersion')
            await asyncio.sleep(1)
            self.label_next('raw-b')
            two = await Cdp(self.url, self.case_out / 'raw-control.jsonl', 'raw-b').__aenter__()
            await two.call('Browser.getVersion')
            params = {'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True}
            if tabs:
                params['filter'] = [{'type': 'page', 'exclude': True}, {}]
            await one.call('Target.setAutoAttach', params)
            await two.call('Target.setAutoAttach', params)
            after = time.time()
            label = 'raw-tab-navigate-probe' if navigate else ('raw-tab-resume-probe' if tabs else 'raw-resume-probe')
            target = (await one.call('Target.createTarget', {'url': 'about:blank' if navigate else self.fixture(label), 'background': True}))['targetId']
            def attachment(client):
                return next((e['params'] for e in client.events if e.get('method') == 'Target.attachedToTarget'
                    and ((tabs and e['params']['targetInfo']['type'] == 'tab' and e['params']['waitingForDebugger'])
                         or (not tabs and e['params']['targetInfo']['targetId'] == target))), None)
            a = await self.wait_for(lambda: attachment(one))
            b = await self.wait_for(lambda: attachment(two))
            child_sessions = {}
            if tabs:
                child_params = {'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True, 'filter': [{}]}
                for client, parent in ((one, a), (two, b)):
                    await client.call('Target.setAutoAttach', child_params, session=parent['sessionId'])
                    child = await self.wait_for(lambda: next((e['params'] for e in client.events
                        if e.get('method') == 'Target.attachedToTarget' and e.get('sessionId') == parent['sessionId']
                        and e['params']['targetInfo']['targetId'] == target), None))
                    await client.call('Runtime.runIfWaitingForDebugger', session=child['sessionId'])
                    child_sessions[client.label] = child['sessionId']
                    self.event('raw_child_resumed_before_either_tab', client=client.label, child=child)
            self.event('raw_both_attached', target=target, one=a, two=b,
                       hits=[h for h in self.fixture_hits if h['time'] >= after])
            assert a['waitingForDebugger'] and b['waitingForDebugger']
            await one.call('Runtime.runIfWaitingForDebugger', session=a['sessionId'])
            self.event('raw_first_resume_acknowledged')
            navigating = None
            if navigate:
                await one.call('Page.enable', session=child_sessions[one.label])
                navigating = asyncio.create_task(one.call('Page.navigate', {'url': self.fixture(label)}, session=child_sessions[one.label]))
            await asyncio.sleep(3)
            self.event('raw_after_first_only', hits=[h for h in self.fixture_hits if h['time'] >= after])
            await two.call('Runtime.runIfWaitingForDebugger', session=b['sessionId'])
            self.event('raw_second_resume_acknowledged')
            if navigating:
                await navigating
            await self.wait_for(lambda: any(h['path'] == '/script-executed?label=' + label
                                           for h in self.fixture_hits))
            self.event('raw_after_both', hits=[h for h in self.fixture_hits if h['time'] >= after])
            await one.call('Target.closeTarget', {'targetId': target})
        finally:
            if two:
                await two.__aexit__()
            await one.__aexit__()

    async def mcp_exit_probe(self):
        client = await self.mcp('mcp170-exit', 'mcp170')
        first = await self.mcp_new(client, 'mcp170-close-unselected')
        survivor = await self.mcp_new(client, 'mcp170-left-open-on-exit', background=True)
        await client.tool('close_page', {'pageId': first})
        await client.close()
        await asyncio.sleep(1)
        self.label_next('mcp170-exit-observer')
        async with Cdp(self.url, self.case_out / 'observer.jsonl', 'mcp170-exit-observer') as raw:
            targets = await raw.call('Target.getTargets')
        self.event('targets_after_mcp_exit', tool_page_id=survivor, targets=targets)

    async def puppeteer_foreign(self):
        self.label_next('puppeteer-foreign')
        client = await Stdio(self, 'puppeteer-foreign', ['node', str(Path(__file__).with_name('traffic_puppeteer_client.mjs')),
            str(self.puppeteer_module()), self.address]).start()
        await client.call('connect')
        await asyncio.sleep(1)
        self.label_next('foreign-creator')
        async with Cdp(self.url, self.case_out / 'foreign-creator.jsonl', 'foreign-creator') as creator:
            await creator.call('Browser.getVersion')
            target = await creator.call('Target.createTarget', {'url': self.fixture('foreign-to-puppeteer'), 'background': True})
            await self.wait_for(lambda: any(h['path'] == '/script-executed?label=foreign-to-puppeteer' for h in self.fixture_hits))
            pages = await client.call('pages')
            self.event('puppeteer_adopted_foreign_page', target=target, pages=pages)
            assert any('foreign-to-puppeteer' in p['url'] for p in pages)
            await creator.call('Target.closeTarget', target)
        await client.call('disconnect')
        await client.close()

    async def scenario(self, name):
        self.case_out = self.out / name
        self.case_out.mkdir()
        case = {'case': name, 'start': time.time(), 'passed': False}
        try:
            if self.find_browser() is None:
                await self.launch(background=True)
            self.pid = disposable_pid(self.profile)
            await self.start_tray()
            assert not self.status().get('hold')
            if name in ('mcp170', 'mcplatest'):
                await self.mcp_solo(name)
            elif name == 'playwright':
                await self.playwright_solo()
            elif name == 'puppeteer':
                await self.puppeteer_solo()
            elif name in ('dual-mcp', 'mixed-three'):
                await self.concurrent(mixed=name == 'mixed-three')
            elif name.endswith('-reconnect'):
                await self.reconnect(name.removesuffix('-reconnect'))
            elif name in ('creation-focus', 'creation-focus-latest'):
                await self.creation_focus(latest_only=name.endswith('-latest'))
            elif name in ('autoattach-client', 'autoattach-native'):
                await self.autoattach_probe(native=name == 'autoattach-native')
            elif name in ('autoattach-raw', 'autoattach-raw-tabs', 'autoattach-raw-tab-navigation'):
                await self.raw_resume_probe(tabs=name != 'autoattach-raw', navigate=name.endswith('-navigation'))
            elif name == 'mcp170-exit':
                await self.mcp_exit_probe()
            elif name == 'puppeteer-foreign':
                await self.puppeteer_foreign()
            else:
                raise ValueError(name)
            case['passed'] = True
        except Exception as exc:
            case.update(error=repr(exc), traceback=traceback.format_exc())
        finally:
            for tool in self.tool_processes:
                await tool.close()
            await asyncio.sleep(1)
            case.update(end=time.time(), approvals=self.approvals(), status=self.status())
            self.event('scenario_complete', **case)
            await self.stop_tray()
            self.cases.append(case)
            write_json(self.out / 'cases.json', self.cases)
            print(json.dumps(case), flush=True)

    async def run(self):
        try:
            return await super().run()
        finally:
            self.http.shutdown()
            self.http.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--tooling', type=Path, required=True)
    parser.add_argument('--port', type=int, default=19339)
    parser.add_argument('--only', action='append', choices=['mcp170', 'mcplatest', 'playwright', 'puppeteer',
        'dual-mcp', 'mixed-three', 'mcp170-reconnect', 'mcplatest-reconnect', 'creation-focus',
        'autoattach-client', 'autoattach-native', 'autoattach-raw', 'mcp170-exit', 'creation-focus-latest',
        'puppeteer-foreign', 'autoattach-raw-tabs', 'autoattach-raw-tab-navigation'])
    parser.add_argument('--work-pause', type=float, default=60)
    parser.add_argument('--ui-timeout', type=float, default=900)
    args = parser.parse_args()
    args.only = args.only or ['mcp170', 'mcplatest', 'playwright', 'puppeteer']
    args.start_delay = args.wait_idle = 0
    args.hidden_background = False
    raise SystemExit(asyncio.run(Recon(args).run()))


if __name__ == '__main__':
    main()
