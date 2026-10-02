"""The held connection: one approved Chrome socket lent to one client at a time.

Real loopback websockets against a scripted fake Chrome that answers CDP calls,
hands out session and context ids, and records everything it receives. No
Chrome, native UI or user profile.
"""
import asyncio
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp_relay import Relay, RELAY_PATH
from test_cdp_relay import RecordingFocus
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


class FakeChrome:
    """Answers every call; a few methods behave like Chrome's."""

    def __init__(self):
        self.connections = 0
        self.sockets = []
        self.received = []         # (connection number, method, params)
        self.wire_ids = []         # (method, id) as Chrome saw them
        self.sessions = 0
        self.contexts = 0

    async def __call__(self, ws):
        self.connections += 1
        number = self.connections
        self.sockets.append(ws)
        try:
            async for raw in ws:
                message = json.loads(raw)
                method, params = message.get("method"), message.get("params", {})
                self.received.append((number, method, params))
                self.wire_ids.append((method, message.get("id")))
                if method == "Test.delay":
                    asyncio.get_running_loop().call_later(
                        params["seconds"], lambda m=message: asyncio.ensure_future(
                            ws.send(json.dumps({"id": m["id"], "result": {"late": m["params"]["value"]}}))))
                    continue
                if method == "Test.emit":
                    await ws.send(json.dumps(params["event"]))
                    result = {}
                elif method == "Test.closeMe":
                    await ws.close(1012, "restarting")
                    return
                elif method in ("Target.attachToTarget", "Target.attachToBrowserTarget"):
                    self.sessions += 1
                    result = {"sessionId": f"S{self.sessions}"}
                elif method == "Target.createBrowserContext":
                    self.contexts += 1
                    result = {"browserContextId": f"C{self.contexts}"}
                else:
                    result = {"echo": params, "connection": number}
                reply = {"id": message["id"], "result": result}
                if "sessionId" in message:
                    reply["sessionId"] = message["sessionId"]
                await ws.send(json.dumps(reply))
        except ConnectionClosed:
            pass

    def methods(self, after=0):
        return [(m, p) for _, m, p in self.received[after:] if not m.startswith("Test.")]


class HeldRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name)
        self.chrome = FakeChrome()
        self.server = await serve(self.chrome, "127.0.0.1", 0, ping_interval=None)
        self.addAsyncCleanup(self.stop_chrome)
        self.write_endpoint()
        self.focus = RecordingFocus()
        self.statuses = []
        self.relay = await Relay(self.profile, self.focus, port=0, open_timeout=.5, hold=True,
                                 status=lambda **v: self.statuses.append(v)).start()
        self.addAsyncCleanup(self.relay.close)
        self.url = f"ws://127.0.0.1:{self.relay.port}{RELAY_PATH}"
        self.next_id = 0

    def write_endpoint(self, path="/devtools/browser/test"):
        (self.profile / "DevToolsActivePort").write_text(f"{self.server.sockets[0].getsockname()[1]}\n{path}\n")

    async def stop_chrome(self):
        self.server.close()
        await self.server.wait_closed()

    async def client(self):
        ws = await connect(self.url, proxy=None)
        self.addAsyncCleanup(ws.close)
        return ws

    async def call(self, ws, method, params=None, id=None, session=None):
        self.next_id += 1
        message = {"id": id if id is not None else self.next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        await ws.send(json.dumps(message))
        return json.loads(await asyncio.wait_for(ws.recv(), 2))

    async def leave(self, ws):
        await ws.close()
        await self.eventually(lambda: self.relay.held.owner is None and self.relay.held.reset_done.is_set())

    async def eventually(self, predicate, seconds=2):
        for _ in range(int(seconds * 100)):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail("Condition did not become true")

    async def test_sequential_clients_share_one_connection_and_one_prompt(self):
        for value in ("a", "b", "c"):
            ws = await self.client()
            reply = await self.call(ws, "Runtime.evaluate", {"v": value})
            self.assertEqual(reply["result"]["echo"], {"v": value})
            await self.leave(ws)
        self.assertEqual(self.chrome.connections, 1)
        self.assertEqual(self.focus.calls, 1)
        self.assertTrue(self.relay.held.open)

    async def test_each_client_keeps_its_own_ids(self):
        a = await self.client()
        self.assertEqual((await self.call(a, "Runtime.evaluate", id=7))["id"], 7)
        await self.leave(a)
        b = await self.client()
        self.assertEqual((await self.call(b, "Runtime.evaluate", id=7))["id"], 7)
        on_the_wire = [i for m, i in self.chrome.wire_ids if m == "Runtime.evaluate"]
        self.assertEqual(len(on_the_wire), 2)
        self.assertNotEqual(on_the_wire[0], on_the_wire[1], "both clients' id 7 reached Chrome as the same id")
        self.assertEqual(self.chrome.connections, 1)

    async def test_a_departed_clients_late_reply_never_reaches_the_next(self):
        a = await self.client()
        await a.send(json.dumps({"id": 5, "method": "Test.delay", "params": {"seconds": .3, "value": "for-a"}}))
        await asyncio.sleep(.05)
        await self.leave(a)
        b = await self.client()
        reply = await self.call(b, "Runtime.evaluate", {"v": "for-b"}, id=5)
        self.assertEqual(reply["result"]["echo"], {"v": "for-b"})
        await asyncio.sleep(.4)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(b.recv(), .2)

    async def test_leaving_undoes_sessions_contexts_and_browser_settings(self):
        a = await self.client()
        await self.call(a, "Target.setDiscoverTargets", {"discover": True})
        await self.call(a, "Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        session = (await self.call(a, "Target.attachToTarget", {"targetId": "T1", "flatten": True}))["result"]["sessionId"]
        context = (await self.call(a, "Target.createBrowserContext"))["result"]["browserContextId"]
        await self.call(a, "Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": "/tmp/x"})
        await self.call(a, "Fetch.enable", {"patterns": []})
        await self.call(a, "Runtime.enable", session=session)   # session state: undone by detaching
        before = len(self.chrome.received)
        await self.leave(a)
        undo = self.chrome.methods(before)
        self.assertIn(("Target.setDiscoverTargets", {"discover": False}), undo)
        self.assertIn(("Target.setAutoAttach", {"autoAttach": False, "waitForDebuggerOnStart": False}), undo)
        self.assertIn(("Browser.setDownloadBehavior", {"behavior": "default"}), undo)
        self.assertIn(("Fetch.disable", {}), undo)
        self.assertIn(("Target.detachFromTarget", {"sessionId": session}), undo)
        self.assertIn(("Target.disposeBrowserContext", {"browserContextId": context}), undo)
        # settings are switched off before sessions go, so nothing new auto-attaches meanwhile
        self.assertLess(undo.index(("Target.setAutoAttach", {"autoAttach": False, "waitForDebuggerOnStart": False})),
                        undo.index(("Target.detachFromTarget", {"sessionId": session})))

    async def test_auto_attached_sessions_are_detached_too(self):
        a = await self.client()
        event = {"method": "Target.attachedToTarget",
                 "params": {"sessionId": "AUTO1", "targetInfo": {"targetId": "T9"}, "waitingForDebugger": False}}
        await a.send(json.dumps({"id": 1, "method": "Test.emit", "params": {"event": event}}))
        self.assertEqual(json.loads(await asyncio.wait_for(a.recv(), 2))["method"], "Target.attachedToTarget")
        await asyncio.wait_for(a.recv(), 2)   # Test.emit's own reply
        before = len(self.chrome.received)
        await self.leave(a)
        self.assertIn(("Target.detachFromTarget", {"sessionId": "AUTO1"}), self.chrome.methods(before))

    async def test_sessions_the_client_detached_itself_are_not_detached_again(self):
        a = await self.client()
        session = (await self.call(a, "Target.attachToTarget", {"targetId": "T1", "flatten": True}))["result"]["sessionId"]
        event = {"method": "Target.detachedFromTarget", "params": {"sessionId": session}}
        await a.send(json.dumps({"id": 99, "method": "Test.emit", "params": {"event": event}}))
        await asyncio.wait_for(a.recv(), 2)
        await asyncio.wait_for(a.recv(), 2)
        before = len(self.chrome.received)
        await self.leave(a)
        self.assertNotIn(("Target.detachFromTarget", {"sessionId": session}), self.chrome.methods(before))

    async def test_events_from_a_previous_clients_session_are_dropped(self):
        a = await self.client()
        session = (await self.call(a, "Target.attachToTarget", {"targetId": "T1", "flatten": True}))["result"]["sessionId"]
        await self.leave(a)
        b = await self.client()
        straggler = {"method": "Runtime.consoleAPICalled", "sessionId": session, "params": {}}
        await b.send(json.dumps({"id": 1, "method": "Test.emit", "params": {"event": straggler}}))
        reply = json.loads(await asyncio.wait_for(b.recv(), 2))
        self.assertEqual(reply.get("id"), 1, "the straggling event was delivered to the next client")

    async def test_browser_close_ends_the_client_not_chrome(self):
        a = await self.client()
        reply = await self.call(a, "Browser.close")
        self.assertEqual(reply, {"id": self.next_id, "result": {}})
        await asyncio.wait_for(a.wait_closed(), 2)
        self.assertEqual(a.close_code, 1000)
        self.assertNotIn("Browser.close", [m for _, m, _ in self.chrome.received])
        await self.eventually(lambda: self.relay.held.owner is None)
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        self.assertEqual(self.chrome.connections, 1)

    async def test_a_concurrent_client_gets_its_own_connection(self):
        a = await self.client()
        await self.call(a, "Runtime.evaluate", {"v": "a"})
        b = await self.client()
        reply = await self.call(b, "Runtime.evaluate", {"v": "b"})
        self.assertEqual(reply["result"]["echo"], {"v": "b"})
        self.assertEqual(self.chrome.connections, 2)
        self.assertEqual(self.focus.calls, 2)
        await b.close()
        await self.eventually(lambda: len(self.relay.clients) == 1)
        self.assertEqual((await self.call(a, "Runtime.evaluate", {"v": "a2"}))["result"]["echo"], {"v": "a2"})
        await self.leave(a)
        c = await self.client()
        await self.call(c, "Runtime.evaluate")
        self.assertEqual(self.chrome.connections, 2, "the held connection was not reused after the extra one closed")

    async def test_losing_chrome_closes_the_client_and_the_next_one_reconnects(self):
        a = await self.client()
        await self.call(a, "Runtime.evaluate")
        await a.send(json.dumps({"id": 2, "method": "Test.closeMe", "params": {}}))
        await asyncio.wait_for(a.wait_closed(), 2)
        self.assertEqual(a.close_code, 1012)
        await self.eventually(lambda: not self.relay.held.open and self.relay.held.owner is None)
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        self.assertEqual(self.chrome.connections, 2)
        self.assertEqual(self.focus.calls, 2)

    async def test_a_chrome_restart_is_noticed_before_lending(self):
        a = await self.client()
        await self.call(a, "Runtime.evaluate")
        await self.leave(a)
        self.write_endpoint("/devtools/browser/restarted")
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        self.assertEqual(self.chrome.connections, 2)
        self.assertEqual(self.focus.calls, 2)

    async def test_malformed_messages_are_answered_not_forwarded(self):
        a = await self.client()
        await a.send("not json")
        error = json.loads(await asyncio.wait_for(a.recv(), 2))
        self.assertEqual(error["error"]["code"], -32700)
        await a.send(json.dumps({"method": "Runtime.evaluate"}))
        self.assertEqual(json.loads(await asyncio.wait_for(a.recv(), 2))["error"]["code"], -32700)
        self.assertEqual(self.chrome.received, [])

    async def test_leaving_while_chrome_is_asked_opens_nothing_for_anyone(self):
        self.focus.release = asyncio.Event()
        a = await self.client()
        await asyncio.wait_for(self.focus.ready.wait(), 1)
        await a.close()
        await self.eventually(lambda: self.focus.finished == 1)
        self.assertEqual(self.chrome.connections, 0)
        self.assertIsNone(self.relay.held.owner)
        self.assertFalse(self.relay.held.lock.locked())

    async def test_status_reports_the_held_connection(self):
        a = await self.client()
        await self.call(a, "Runtime.evaluate")
        self.assertTrue(any(s.get("held") is True for s in self.statuses))
        self.assertTrue(self.statuses[0].get("hold"))

    async def test_shutdown_closes_the_held_connection(self):
        a = await self.client()
        await self.call(a, "Runtime.evaluate")
        await self.leave(a)
        await asyncio.wait_for(self.relay.close(), 2)
        await asyncio.wait_for(self.chrome.sockets[0].wait_closed(), 2)


if __name__ == "__main__":
    unittest.main()
