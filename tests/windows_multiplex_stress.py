"""Additional private-browser checks for concurrent CDP sharing.

Creates its own headless Chrome and Edge profiles and process jobs. It never
uses installed Yes, Dev settings, an active browser profile, or an MCP bridge.
"""
import argparse
import asyncio
from contextlib import AsyncExitStack, suppress
import json
from pathlib import Path
import socket
import time
import traceback

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from windows_relay_live import PrivateChrome, NoFocus, Cdp, eventually
from cdp_multiplex import MultiplexRelay


class Peer:
    """A CDP test client that retains events while calls complete out of order."""
    def __init__(self, url):
        self.url = url
        self.counter = 0
        self.pending = {}
        self.events = asyncio.Queue(4096)

    async def __aenter__(self):
        self.ws = await connect(self.url, proxy=None, close_timeout=1, max_size=64 * 1024 * 1024)
        self.reader = asyncio.create_task(self.read())
        return self

    async def read(self):
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                if "id" in message:
                    future = self.pending.pop(message["id"], None)
                    if future is None:
                        raise AssertionError(f"Unmatched CDP reply: {message}")
                    future.set_result(message)
                else:
                    self.events.put_nowait(message)
        except ConnectionClosed:
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Test client disconnected"))
            self.pending.clear()

    async def call(self, method, params=None, session=None, error=False):
        self.counter += 1
        call_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[call_id] = future
        message = dict(id=call_id, method=method, params=params or {})
        if session:
            message["sessionId"] = session
        await self.ws.send(json.dumps(message))
        response = await asyncio.wait_for(future, 15)
        assert response.get("sessionId") == session, (method, session, response)
        if "error" in response and not error:
            raise AssertionError((method, response["error"]))
        return response if error else response["result"]

    async def event(self, predicate, timeout=10):
        async with asyncio.timeout(timeout):
            while True:
                event = await self.events.get()
                if predicate(event):
                    return event

    async def __aexit__(self, *_):
        await self.ws.close()
        await asyncio.gather(self.reader, return_exceptions=False)


async def pipelined_calls(url, report):
    async with AsyncExitStack() as stack:
        peers = [await stack.enter_async_context(Peer(url)) for _ in range(8)]
        targets = []
        try:
            sessions = []
            for peer in peers:
                target = (await peer.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
                targets.append(target)
                sessions.append((await peer.call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"])
            async def evaluate(peer, session, owner, number):
                token = f"client-{owner}-call-{number}"
                expression = f"new Promise(r => setTimeout(() => r({json.dumps(token)}), {(number % 7) * 4}))"
                result = await peer.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                                              "returnByValue": True}, session)
                assert result["result"]["value"] == token, result
            started = time.monotonic()
            await asyncio.gather(*(evaluate(peer, session, owner, number)
                                  for owner, (peer, session) in enumerate(zip(peers, sessions))
                                  for number in range(80)))
            report["pipelined_calls"] = dict(clients=8, calls=640, seconds=round(time.monotonic() - started, 3))
            # A client leaves with an asynchronous page command still pending.
            pending = asyncio.create_task(peers[0].call("Runtime.evaluate",
                {"expression": "new Promise(r => setTimeout(() => r(1), 500))", "awaitPromise": True}, sessions[0]))
            await asyncio.sleep(.03)
            peers[0].ws.transport.abort()
            assert isinstance((await asyncio.gather(pending, return_exceptions=True))[0], Exception)
            assert (await peers[1].call("Runtime.evaluate", {"expression": "21*2"}, sessions[1]))["result"]["value"] == 42
            report["pending_disconnect"] = "other client's page session remains usable"
        finally:
            for target in targets:
                await peers[-1].call("Target.closeTarget", {"targetId": target})


async def context_cycles(url, relay, report):
    async with Peer(url) as peer:
        for _ in range(100):
            context = (await peer.call("Target.createBrowserContext"))["browserContextId"]
            owner = next(owner for owner in relay.owners.values() if context in owner.contexts)
            await peer.call("Browser.setDownloadBehavior", {"behavior": "deny", "browserContextId": context})
            await peer.call("Target.disposeBrowserContext", {"browserContextId": context})
            assert context not in owner.contexts
        assert not owner.contexts
        target = (await peer.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
        try:
            session = (await peer.call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
            response = await peer.call("Page.setDownloadBehavior", {"behavior": "deny"}, session, error=True)
            assert "error" in response and response["error"]["code"] == -32000
        finally:
            await peer.call("Target.closeTarget", {"targetId": target})
        report["context_cycles"] = dict(disposed=100, retained_context_ids=len(owner.contexts), legacy_download="rejected")


async def reconnect_cycles(url, relay, report):
    async with Peer(url) as keeper:
        await keeper.call("Browser.getVersion")
        for _ in range(100):
            async def round_trip():
                async with Peer(url) as peer:
                    await peer.call("Browser.getVersion")
            await asyncio.gather(*(round_trip() for _ in range(3)))
            await eventually(lambda: len(relay.clients) == 1)
            assert len(relay.owners) == 1 and not relay.pending
        assert await keeper.call("Browser.getVersion")
    report["reconnect_cycles"] = dict(concurrent_new_clients=3, waves=100, connections=300)


async def client_limit(url, relay, report):
    async with AsyncExitStack() as stack:
        peers = [await stack.enter_async_context(Peer(url)) for _ in range(16)]
        await asyncio.gather(*(peer.call("Browser.getVersion") for peer in peers))
        try:
            async with connect(url, proxy=None) as excess:
                await asyncio.wait_for(excess.wait_closed(), 2)
                assert excess.close_code == 1013
        except InvalidStatus as exc:
            assert exc.response.status_code == 503
        assert len(relay.clients) == 16
        assert await peers[-1].call("Browser.getVersion")
    report["client_limit"] = "16 clients work; client 17 is rejected without another upstream"


async def slow_client(url, relay, report):
    async with Peer(url) as fast, Cdp(url) as slow:
        target = (await fast.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
        try:
            session = (await slow.call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
            await slow.call("Runtime.enable", session=session)
            slow.ws.transport.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            # Pause only this test client's reads. Keep the TCP connection open
            # while its page produces enough events to exceed the output queue.
            slow.ws.transport.pause_reading()
            server_side = next(ws for ws in relay.clients if ws.remote_address[1] == slow.ws.local_address[1])
            await slow.ws.send(json.dumps(dict(id=1000, method="Runtime.evaluate", sessionId=session,
                params=dict(expression="for (let i=0;i<1500;i++) console.log('x'.repeat(16384)); 42"))))
            latencies = []
            for _ in range(12):
                started = time.monotonic()
                assert await fast.call("Browser.getVersion")
                latencies.append(time.monotonic() - started)
                await asyncio.sleep(.02)
            await eventually(lambda: server_side not in relay.clients, 10)
            assert max(latencies) < 5, latencies
            report["slow_client"] = dict(slow_peer_disconnected=True,
                fast_client_max_seconds=round(max(latencies), 3), browser_usable=True)
        finally:
            slow.ws.transport.resume_reading()
            # Empty buffered frames so the normal close handshake can complete.
            with suppress(ConnectionClosed, TimeoutError):
                async with asyncio.timeout(2):
                    while True:
                        await slow.ws.recv()
            await fast.call("Target.closeTarget", {"targetId": target})


async def worker_frames(url, relay, report):
    async def serve_page(reader, writer):
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            path = request.split(b" ", 2)[1].decode()
            script = path.endswith(".js")
            if path == "/main":
                body = (f'<iframe src="http://localhost:{port}/frame"></iframe>'
                        '<script>const w = new Worker("/worker.js");'
                        'w.onmessage=e=>globalThis.workerValue=e.data; w.postMessage(21);</script>')
            elif path == "/worker.js":
                body = ('const child = new Worker("/nested.js"); child.onmessage=e=>postMessage(e.data);'
                        'onmessage=e=>child.postMessage(e.data);')
            elif path == "/nested.js":
                body = 'onmessage=e=>postMessage(e.data*2);'
            else:
                body = '<title>isolated frame</title><script>globalThis.frameValue=42;</script>'
            data = body.encode()
            mime = "application/javascript" if script else "text/html"
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {len(data)}\r\n"
                          "Connection: close\r\n\r\n").encode() + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(serve_page, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    watchers = []
    targets = []
    try:
        async with Peer(url) as controller, Peer(url) as a, Peer(url) as b:
            try:
                root_options = dict(autoAttach=True, waitForDebuggerOnStart=True, flatten=True,
                                    filter=[dict(type="page"), dict(exclude=True)])
                for peer in (a, b):
                    await peer.call("Target.setAutoAttach", root_options)
                target = (await controller.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
                targets.append(target)
                def attached_to(target_id):
                    return lambda event: (event.get("method") == "Target.attachedToTarget"
                                          and event["params"]["targetInfo"]["targetId"] == target_id)
                events = await asyncio.gather(*(peer.event(attached_to(target)) for peer in (a, b)))
                sessions = [event["params"]["sessionId"] for event in events]
                seen = [[], []]
                child_options = dict(autoAttach=True, waitForDebuggerOnStart=True, flatten=True,
                                     filter=[dict(type="worker"), dict(type="iframe"), dict(exclude=True)])
                async def resume_children(peer, found):
                    while True:
                        event = await peer.events.get()
                        if event.get("method") != "Target.attachedToTarget":
                            continue
                        params = event["params"]
                        if params["targetInfo"]["type"] not in ("worker", "iframe"):
                            continue
                        child = params["sessionId"]
                        found.append((params["targetInfo"]["type"], child))
                        await peer.call("Target.setAutoAttach", child_options, child)
                        await peer.call("Runtime.runIfWaitingForDebugger", session=child)
                for peer, session, found in zip((a, b), sessions, seen):
                    await peer.call("Target.setAutoAttach", child_options, session)
                    watchers.append(asyncio.create_task(resume_children(peer, found)))
                await asyncio.gather(*(peer.call("Runtime.runIfWaitingForDebugger", session=session)
                                       for peer, session in zip((a, b), sessions)))
                await a.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/main"}, sessions[0])
                async with asyncio.timeout(15):
                    while True:
                        for watcher in watchers:
                            if watcher.done():
                                watcher.result()
                        value = await a.call("Runtime.evaluate", {"expression": "globalThis.workerValue"}, sessions[0])
                        if value["result"].get("value") == 42 and all(
                                sum(kind == "worker" for kind, _ in found) >= 2
                                and any(kind == "iframe" for kind, _ in found) for found in seen):
                            break
                        await asyncio.sleep(.02)
                for peer, found in zip((a, b), seen):
                    frame_session = next(session for kind, session in found if kind == "iframe")
                    value = await peer.call("Runtime.evaluate", {"expression": "location.hostname"}, frame_session)
                    assert value["result"]["value"] == "localhost"
                worker_session = next(session for kind, session in seen[0] if kind == "worker")
                assert "error" in await b.call("Runtime.evaluate", {"expression": "1"}, worker_session, error=True)
                report["nested_targets"] = dict(clients=2,
                    workers_per_client=[sum(kind == "worker" for kind, _ in found) for found in seen],
                    iframe_sessions_per_client=[sum(kind == "iframe" for kind, _ in found) for found in seen],
                    worker_result=42, foreign_worker_session="rejected")
                for watcher in watchers:
                    watcher.cancel()
                await asyncio.gather(*watchers, return_exceptions=True)
                watchers.clear()
                paused = (await controller.call("Target.createTarget", {"url": "data:text/html,<title>resumed</title>"}))["targetId"]
                targets.append(paused)
                paused_events = await asyncio.gather(*(peer.event(attached_to(paused)) for peer in (a, b)))
                assert all(event["params"]["waitingForDebugger"] for event in paused_events)
                await a.ws.close()
                await eventually(lambda: len(relay.clients) == 2)
                survivor = paused_events[1]["params"]["sessionId"]
                await b.call("Runtime.runIfWaitingForDebugger", session=survivor)
                async with asyncio.timeout(10):
                    while (await b.call("Runtime.evaluate", {"expression": "document.title"}, survivor))["result"].get("value") != "resumed":
                        await asyncio.sleep(.02)
                report["paused_target_disconnect"] = "page loads after one debugger client leaves and the other resumes it"
            finally:
                for watcher in watchers:
                    watcher.cancel()
                await asyncio.gather(*watchers, return_exceptions=True)
                for target in targets:
                    await controller.call("Target.closeTarget", {"targetId": target})
    finally:
        server.close()
        await server.wait_closed()


async def check(executable, report):
    browser = PrivateChrome(executable, extra_args=("--site-per-process",))
    relay = None
    try:
        await browser.start()
        focus = NoFocus()
        relay = await MultiplexRelay(browser.profile, focus, port=0).start()
        url = f"ws://127.0.0.1:{relay.port}/devtools/browser/yesdev"
        async with Peer(url) as peer:
            report["product"] = (await peer.call("Browser.getVersion"))["product"]
        for test in (pipelined_calls, context_cycles, reconnect_cycles, client_limit, slow_client, worker_frames):
            if test is pipelined_calls:
                await test(url, report)
            else:
                await test(url, relay, report)
            await eventually(lambda: not relay.clients)
            assert not relay.owners and not relay.pending
            assert focus.calls == 1
            print(json.dumps(dict(browser=report["product"], passed=test.__name__)), flush=True)
        report.update(passed=True, upstream_connections=focus.calls)
    finally:
        if relay is not None:
            await relay.close()
        await browser.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chrome", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    parser.add_argument("--edge", default=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    args = parser.parse_args()
    report = dict(passed=False, browsers=[{}, {}], scope="Private headless browsers with --site-per-process")
    async def run():
        results = await asyncio.gather(*(check(executable, result) for executable, result in
                                        zip((args.chrome, args.edge), report["browsers"])), return_exceptions=True)
        errors = [str(error) for error in results if isinstance(error, BaseException)]
        assert not errors, errors
        report["passed"] = True
    try:
        asyncio.run(run())
    except Exception:
        report["error"] = traceback.format_exc()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
