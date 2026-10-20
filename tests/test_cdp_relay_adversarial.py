"""Adversarial loopback transport tests. No installed browser or user profile.

Use the existing scripted Chrome fixture. Assertions about browser commands
describe the relay's wire contract, not security against other local programs.
"""
import asyncio
import json
from unittest.mock import patch

from test_cdp_relay_hold import HeldRelayFixture
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.protocol import State


class AdversarialRelayTests(HeldRelayFixture):
    async def receive(self, ws):
        return json.loads(await asyncio.wait_for(ws.recv(), 2))

    async def http(self, path="/json/version", headers=()):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.relay.port)
        fields = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{self.relay.port}", *headers, "", ""]
        writer.write("\r\n".join(fields).encode())
        await writer.drain()
        try:
            return (await asyncio.wait_for(reader.read(), 2)).decode()
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_json_shapes_and_bad_ids_do_not_poison_next_call(self):
        ws = await self.client()
        invalid = [None, [], 3, True, "text", {}, {"id": []},
                   {"id": {}, "method": "Runtime.evaluate"},
                   {"id": "1", "method": "Runtime.evaluate"},
                   {"id": 1.25, "method": "Runtime.evaluate"},
                   {"id": 1, "method": None}]
        for value in invalid:
            with self.subTest(value=value):
                await ws.send(json.dumps(value))
                self.assertEqual((await self.receive(ws))["error"]["code"], -32700)
        self.assertEqual(self.chrome.received, [])
        self.assertIn("result", await self.call(ws, "Runtime.evaluate"))

    async def test_boolean_ids_are_not_cdp_integer_ids(self):
        ws = await self.client()
        for value in (True, False):
            with self.subTest(value=value):
                await ws.send(json.dumps({"id": value, "method": "Runtime.evaluate"}))
                self.assertIn("error", await self.receive(ws))
        self.assertEqual(self.chrome.received, [])

    async def test_negative_and_large_integer_ids_are_remapped(self):
        ws = await self.client()
        ids = [-1, -(2 ** 63), 2 ** 256]
        for client_id in ids:
            self.assertEqual((await self.call(ws, "Runtime.evaluate", id=client_id))["id"], client_id)
        wire = [i for method, i in self.chrome.wire_ids if method == "Runtime.evaluate"]
        self.assertEqual(len(set(wire)), len(ids))
        self.assertTrue(all(type(i) is int and 0 < i < 100 for i in wire))

    async def test_duplicate_inflight_ids_keep_their_own_results(self):
        ws = await self.client()
        for delay, value in ((.08, "first"), (.01, "second")):
            await ws.send(json.dumps({"id": 7, "method": "Test.delay",
                                     "params": {"seconds": delay, "value": value}}))
        replies = [await self.receive(ws), await self.receive(ws)]
        self.assertEqual([reply["id"] for reply in replies], [7, 7])
        self.assertEqual([reply["result"]["late"] for reply in replies], ["second", "first"])
        wire = [i for method, i in self.chrome.wire_ids if method == "Test.delay"]
        self.assertEqual(len(set(wire)), 2)

    async def test_binary_json_normal_command_is_supported_by_held_path(self):
        ws = await self.client()
        await ws.send(json.dumps({"id": 4, "method": "Runtime.evaluate", "params": {"value": "binary"}}).encode())
        self.assertEqual((await self.receive(ws))["result"]["echo"], {"value": "binary"})

    async def test_invalid_utf8_binary_does_not_break_owner(self):
        ws = await self.client()
        await ws.send(b"\xff\xfe\x00")
        self.assertIn("error", await self.receive(ws))
        self.assertIn("result", await self.call(ws, "Runtime.evaluate"))

    async def test_binary_browser_close_on_fallback_does_not_reach_chrome(self):
        a = await self.client()
        await self.call(a, "Runtime.evaluate")
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        await b.send(b'{"id": 9, "method": "Browser.close"}')
        await asyncio.wait_for(b.wait_closed(), 2)
        self.assertNotIn("Browser.close", [method for _, method, _ in self.chrome.received])
        self.assertIn("result", await self.call(a, "Runtime.evaluate"))

    async def test_binary_browser_close_on_held_path_does_not_reach_chrome(self):
        ws = await self.client()
        await ws.send(b'{"id": 9, "method": "Browser.close"}')
        self.assertEqual(await self.receive(ws), {"id": 9, "result": {}})
        await asyncio.wait_for(ws.wait_closed(), 2)
        self.assertNotIn("Browser.close", [method for _, method, _ in self.chrome.received])

    async def test_escaped_browser_close_on_fallback_does_not_reach_chrome(self):
        a = await self.client()
        await self.call(a, "Runtime.evaluate")
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        await b.send('{"id": 9, "method": "Browser.\\u0063lose"}')
        await asyncio.wait_for(b.wait_closed(), 2)
        self.assertNotIn("Browser.close", [method for _, method, _ in self.chrome.received])
        self.assertIn("result", await self.call(a, "Runtime.evaluate"))

    async def test_stale_session_event_and_reply_do_not_reach_new_owner(self):
        a = await self.client()
        session = (await self.call(a, "Target.attachToTarget", {"targetId": "T1", "flatten": True}))["result"]["sessionId"]
        await self.leave(a)
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        upstream = self.chrome.sockets[0]
        await upstream.send(json.dumps({"id": -989, "result": {"not": "pending"}}))
        await upstream.send(json.dumps({"method": "Runtime.consoleAPICalled", "sessionId": session, "params": {}}))
        await upstream.send(json.dumps({"method": "Runtime.consoleAPICalled", "sessionId": "unknown-local-session", "params": {}}))
        # Unknown session ids are not a claim about local-peer authorization.
        event = await self.receive(b)
        self.assertEqual(event["sessionId"], "unknown-local-session")
        self.assertIn("result", await self.call(b, "Runtime.evaluate"))

    async def test_rapid_sequential_attach_and_disconnect_cleans_all_sessions(self):
        for index in range(12):
            ws = await self.client()
            await self.call(ws, "Target.attachToTarget", {"targetId": f"T{index}", "flatten": True})
            await self.leave(ws)
        self.assertEqual(self.chrome.live_sessions, set())
        self.assertEqual(self.chrome.connections, 1)
        self.assertEqual(self.relay.held.pending, {})

    async def test_disconnect_storm_during_prompt_opens_no_socket(self):
        self.focus.release = asyncio.Event()
        sockets = await asyncio.gather(*(self.client() for _ in range(10)))
        await asyncio.wait_for(self.focus.ready.wait(), 1)
        await asyncio.gather(*(ws.close() for ws in sockets))
        await self.eventually(lambda: not self.relay.clients and self.focus.active == 0)
        self.assertEqual(self.chrome.connections, 0)
        self.assertFalse(self.relay.held.lock.locked())

    async def test_client_limit_counts_pending_prompt_and_recovers(self):
        self.relay.max_clients = 2
        self.focus.release = asyncio.Event()
        a, b = await self.client(), await self.client()
        await self.eventually(lambda: len(self.relay.clients) == 2)
        with self.assertRaises(InvalidStatus) as failed:
            await connect(self.url, proxy=None)
        self.assertEqual(failed.exception.response.status_code, 503)
        await b.close()
        await self.eventually(lambda: len(self.relay.clients) == 1)
        c = await self.client()
        await c.close()
        await a.close()
        await self.eventually(lambda: not self.relay.clients)
        self.assertEqual(self.chrome.connections, 0)

    async def test_duplicate_host_and_origin_headers_do_not_arm_focus(self):
        for headers in (("Host: evil.example",),
                        (f"Host: 127.0.0.1:{self.relay.port}",),
                        ("Origin: null",),
                        ("Origin: http://localhost", "Origin: http://localhost")):
            with self.subTest(headers=headers):
                response = await self.http(headers=headers)
                self.assertTrue("400 Bad Request" in response or "403 Forbidden" in response)
        self.assertEqual(self.focus.calls, 0)
        self.assertEqual(self.chrome.connections, 0)

    async def test_discovery_rejects_endpoint_injection_and_bad_paths(self):
        endpoint_file = self.profile / "DevToolsActivePort"
        for content in ("0\n/devtools/browser/test\n", "65536\n/devtools/browser/test\n",
                        "1234\n//example.com\n", "1234\n/devtools/browser/test?x=1\n",
                        "1234\n/devtools/browser/../test\n", "x\n/devtools/browser/test\n"):
            with self.subTest(content=content):
                endpoint_file.write_text(content)
                self.assertIn("503 Service Unavailable", await self.http())
        self.write_endpoint()
        for path in ("/json/version?x=1", "/json/list", "/json/new", "/devtools/browser/yesdev/"):
            self.assertIn("404 Not Found", await self.http(path))
        self.assertEqual(self.focus.calls, 0)

    async def test_endpoint_change_during_prompt_is_rejected_before_connect(self):
        self.focus.release = asyncio.Event()
        ws = await self.client()
        await asyncio.wait_for(self.focus.ready.wait(), 1)
        self.write_endpoint("/devtools/browser/changed")
        self.focus.release.set()
        await asyncio.wait_for(ws.wait_closed(), 2)
        self.assertEqual(ws.close_code, 1011)
        self.assertEqual(self.chrome.connections, 0)
        b = await self.client()
        self.assertIn("result", await self.call(b, "Runtime.evaluate"))

    async def test_upstream_loss_during_reset_retires_connection(self):
        self.chrome.ignore = lambda method, params: method == "Target.setDiscoverTargets" and params.get("discover") is False
        a = await self.client()
        await self.call(a, "Target.setDiscoverTargets", {"discover": True})
        await a.close()
        await self.eventually(lambda: any(method == "Target.setDiscoverTargets" and params.get("discover") is False
                                          for _, method, params in self.chrome.received))
        await self.chrome.sockets[0].close(1012, "reset interrupted")
        await self.eventually(lambda: self.relay.held.reset_done.is_set() and not self.relay.clients)
        self.assertFalse(self.relay.held.open)
        b = await self.client()
        self.assertIn("result", await self.call(b, "Runtime.evaluate"))
        self.assertEqual(self.chrome.connections, 2)

    async def test_cancelled_reset_retires_instead_of_lending_dirty_connection(self):
        a = await self.client()
        await self.call(a, "Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        resetting = asyncio.Event()

        async def cancelled_reset():
            resetting.set()
            await asyncio.Future()

        with patch.object(self.relay.held, "reset", cancelled_reset):
            release = asyncio.create_task(self.relay.held.release(self.relay.held.owner))
            await asyncio.wait_for(resetting.wait(), 1)
            release.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await release
        await a.close()
        self.assertFalse(self.relay.held.open, "A cancelled reset made a dirty connection available")
        self.assertTrue(self.relay.held.reset_done.is_set())

    async def test_late_attach_after_release_does_not_leave_old_session_live(self):
        self.chrome.ignore = lambda method, params: method == "Target.attachToTarget"
        a = await self.client()
        await a.send(json.dumps({"id": 6, "method": "Target.attachToTarget", "params": {"targetId": "T1", "flatten": True}}))
        await self.eventually(lambda: any(method == "Target.attachToTarget" for method, _ in self.chrome.wire_ids))
        wire_id = next(i for method, i in self.chrome.wire_ids if method == "Target.attachToTarget")
        original = self.chrome.sockets[0]
        await self.leave(a)
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        if original.state is not State.OPEN:
            self.assertEqual(self.chrome.connections, 2)
            return  # Retiring the socket also ends its outstanding session calls.
        self.chrome.live_sessions.add("LATE-OLD")
        await original.send(json.dumps({"id": wire_id, "result": {"sessionId": "LATE-OLD"}}))
        await asyncio.sleep(.1)
        self.assertNotIn("LATE-OLD", self.chrome.live_sessions,
                         "An old borrower's late attach remained live while the next borrower owns the connection")

    async def test_late_dispose_on_detach_context_is_cleaned_before_next_borrower(self):
        self.chrome.ignore = lambda method, params: method == "Target.createBrowserContext"
        a = await self.client()
        await a.send(json.dumps({"id": 6, "method": "Target.createBrowserContext", "params": {"disposeOnDetach": True}}))
        await self.eventually(lambda: any(method == "Target.createBrowserContext" for method, _ in self.chrome.wire_ids))
        wire_id = next(i for method, i in self.chrome.wire_ids if method == "Target.createBrowserContext")
        original = self.chrome.sockets[0]
        await self.leave(a)
        b = await self.client()
        await self.call(b, "Runtime.evaluate")
        if original.state is not State.OPEN:
            self.assertEqual(self.chrome.connections, 2)
            return
        await original.send(json.dumps({"id": wire_id, "result": {"browserContextId": "LATE-CONTEXT"}}))
        await asyncio.sleep(.1)
        self.assertIn(("Target.disposeBrowserContext", {"browserContextId": "LATE-CONTEXT"}),
                      self.chrome.methods(), "An old borrower's context was not disposed during the next borrow")


if __name__ == "__main__":
    import unittest
    unittest.main()
