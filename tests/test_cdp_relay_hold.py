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
import cdp_relay
from cdp_relay import Relay, RELAY_PATH
from unittest.mock import patch
from test_cdp_relay import RecordingFocus
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


class FakeChrome:
    """Answers every call; the methods the relay depends on behave like Chrome 154's.

    Strict where real Chrome was strict in the 2026-10-02 live run: browser-level
    auto-attach without flatten is refused with Chrome's own error, and
    detaching a session it does not have is an error. Tests can make it refuse
    or ignore other calls, and Browser.close ends every connection, as Chrome
    exiting would.
    """

    def __init__(self):
        self.connections = 0
        self.sockets = []
        self.received = []         # (connection number, method, params)
        self.wire_ids = []         # (method, id) as Chrome saw them
        self.sessions = 0
        self.contexts = 0
        self.live_sessions = set()
        self.auto_attach = {}      # connection number -> browser-level auto-attach on?
        self.refuse = lambda method, params: None   # -> error dict to send instead
        self.ignore = lambda method, params: False  # -> never answer this call

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
                error = self.refuse(method, params)
                if "sessionId" not in message and method == "Target.setAutoAttach" \
                        and params.get("flatten") is not True:
                    error = {"code": -32602,
                             "message": "Only flatten protocol is supported with browser level auto-attach"}
                if method == "Target.detachFromTarget" and params.get("sessionId") not in self.live_sessions:
                    error = {"code": -32602, "message": "No session with given id"}
                if error is not None:
                    await ws.send(json.dumps({"id": message["id"], "error": error}))
                    continue
                if self.ignore(method, params):
                    continue
                if method == "Browser.close":
                    for other in list(self.sockets):
                        await other.close(1011, "browser exited")
                    return
                if method == "Test.emit":
                    await ws.send(json.dumps(params["event"]))
                    result = {}
                elif method == "Test.forget":     # the session vanished without telling anyone
                    self.live_sessions.discard(params["sessionId"])
                    result = {}
                elif method == "Test.closeMe":
                    await ws.close(1012, "restarting")
                    return
                elif method in ("Target.attachToTarget", "Target.attachToBrowserTarget"):
                    self.sessions += 1
                    self.live_sessions.add(f"S{self.sessions}")
                    result = {"sessionId": f"S{self.sessions}"}
                elif method == "Target.detachFromTarget":
                    self.live_sessions.discard(params["sessionId"])
                    result = {}
                elif method == "Target.setAutoAttach" and "sessionId" not in message:
                    self.auto_attach[number] = params["autoAttach"]
                    result = {}
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


class HeldRelayFixture(unittest.IsolatedAsyncioTestCase):
    """The relay with hold on, a fake Chrome and client helpers. No tests here."""

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


class HeldRelayTests(HeldRelayFixture):
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
        context = (await self.call(a, "Target.createBrowserContext",
                                   {"disposeOnDetach": True}))["result"]["browserContextId"]
        await self.call(a, "Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": "/tmp/x"})
        await self.call(a, "Fetch.enable", {"patterns": []})
        await self.call(a, "Runtime.enable", session=session)   # session state: undone by detaching
        before = len(self.chrome.received)
        await self.leave(a)
        undo = self.chrome.methods(before)
        self.assertIn(("Target.setDiscoverTargets", {"discover": False}), undo)
        self.assertIn(("Target.setAutoAttach", {"autoAttach": False, "waitForDebuggerOnStart": False,
                                                "flatten": True}), undo)
        self.assertIn(("Browser.setDownloadBehavior", {"behavior": "default"}), undo)
        self.assertIn(("Fetch.disable", {}), undo)
        self.assertIn(("Target.detachFromTarget", {"sessionId": session}), undo)
        self.assertIn(("Target.disposeBrowserContext", {"browserContextId": context}), undo)
        # settings are switched off before sessions go, so nothing new auto-attaches meanwhile
        self.assertLess(undo.index(("Target.setAutoAttach", {"autoAttach": False, "waitForDebuggerOnStart": False,
                                                             "flatten": True})),
                        undo.index(("Target.detachFromTarget", {"sessionId": session})))
        self.assertTrue(self.relay.held.open, "a clean reset should keep the connection")

    async def test_auto_attached_sessions_are_detached_too(self):
        self.chrome.live_sessions.add("AUTO1")
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

    async def test_auto_attach_is_really_off_for_the_next_client(self):
        """The live failure: Playwright left auto-attach on and the next page hung."""
        a = await self.client()
        await self.call(a, "Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        self.assertTrue(self.chrome.auto_attach[1])
        await self.leave(a)
        self.assertFalse(self.chrome.auto_attach[1], "Chrome still auto-attaches for the departed client")
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        self.assertEqual(self.chrome.connections, 1)

    async def test_a_refused_reset_retires_the_connection(self):
        self.chrome.refuse = lambda method, params: (
            {"code": -32000, "message": "refused"}
            if method == "Browser.setDownloadBehavior" and params.get("behavior") == "default" else None)
        a = await self.client()
        await self.call(a, "Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": "/tmp/x"})
        await self.leave(a)
        self.assertFalse(self.relay.held.open)
        self.assertFalse(self.statuses[-1].get("held"))
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        self.assertEqual(self.chrome.connections, 2, "the next client inherited an unreset connection")
        self.assertEqual(self.focus.calls, 2)

    async def test_an_unanswered_reset_retires_the_connection(self):
        self.chrome.ignore = lambda method, params: (
            method == "Target.setDiscoverTargets" and params.get("discover") is False)
        a = await self.client()
        await self.call(a, "Target.setDiscoverTargets", {"discover": True})
        with patch.object(cdp_relay, "HOLD_RESET_TIMEOUT", .2):
            await self.leave(a)
        self.assertFalse(self.relay.held.open)
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        self.assertEqual(self.chrome.connections, 2)

    async def test_a_session_that_was_already_gone_does_not_retire_the_connection(self):
        a = await self.client()
        session = (await self.call(a, "Target.attachToTarget", {"targetId": "T1", "flatten": True}))["result"]["sessionId"]
        await self.call(a, "Test.forget", {"sessionId": session})
        await self.leave(a)
        self.assertTrue(self.relay.held.open)
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        self.assertEqual(self.chrome.connections, 1)

    async def test_contexts_follow_chromes_own_rule(self):
        a = await self.client()
        disposed = (await self.call(a, "Target.createBrowserContext",
                                    {"disposeOnDetach": True}))["result"]["browserContextId"]
        kept = [(await self.call(a, "Target.createBrowserContext", params))["result"]["browserContextId"]
                for params in ({}, {"disposeOnDetach": False})]
        before = len(self.chrome.received)
        await self.leave(a)
        undo = self.chrome.methods(before)
        self.assertIn(("Target.disposeBrowserContext", {"browserContextId": disposed}), undo)
        for context in kept:
            self.assertNotIn(("Target.disposeBrowserContext", {"browserContextId": context}), undo)

    async def test_a_concurrent_clients_browser_close_does_not_close_chrome(self):
        """The live failure: the fallback path forwarded Browser.close and Chrome exited."""
        a = await self.client()
        await self.call(a, "Runtime.evaluate", {"v": "held"})
        b = await self.client()
        await self.call(b, "Runtime.evaluate", {"v": "own"})
        self.assertEqual(self.chrome.connections, 2)
        reply = await self.call(b, "Browser.close")
        self.assertEqual(reply["result"], {})
        await asyncio.wait_for(b.wait_closed(), 2)
        self.assertEqual(b.close_code, 1000)
        self.assertNotIn("Browser.close", [m for _, m, _ in self.chrome.received])
        self.assertEqual((await self.call(a, "Runtime.evaluate", {"v": "still"}))["result"]["echo"], {"v": "still"})

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
