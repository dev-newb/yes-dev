"""Concurrent transport contracts, using real loopback WebSockets."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

from test_cdp_relay_hold import HeldRelayFixture
import test_cdp_relay_hold
from cdp_multiplex import Client, MultiplexRelay
from websockets.exceptions import ConnectionClosed


class MultiplexTests(HeldRelayFixture):
    async def asyncSetUp(self):
        with patch.object(test_cdp_relay_hold, "Relay", MultiplexRelay):
            await super().asyncSetUp()

    async def test_concurrent_duplicate_ids_and_single_upstream(self):
        clients = await asyncio.gather(*(self.client() for _ in range(8)))
        replies = await asyncio.gather(*(self.call(ws, "Test.echo", {"client": n}, id=1)
                                        for n, ws in enumerate(clients)))
        self.assertEqual([r["result"]["echo"]["client"] for r in replies], list(range(8)))
        self.assertTrue(all(r["id"] == 1 and "sessionId" not in r for r in replies))
        self.assertEqual(self.chrome.connections, 1)
        ids = [wire for _, wire in self.chrome.wire_ids]
        self.assertEqual(len(ids), len(set(ids)))

    async def test_nested_events_and_session_ownership(self):
        a, b = await self.client(), await self.client()
        attached = await self.call(a, "Target.attachToTarget", {"targetId": "page", "flatten": True})
        session = attached["result"]["sessionId"]
        reply = await self.call(b, "Runtime.evaluate", {"expression": "42"}, session=session)
        self.assertIn("error", reply)
        reply = await self.call(b, "Target.detachFromTarget", {"sessionId": session})
        self.assertIn("error", reply)
        owner = self.relay.owners[session]
        event = dict(method="Target.attachedToTarget", sessionId=session,
                     params=dict(sessionId="worker", targetInfo=dict(type="worker")))
        await self.chrome.sockets[0].send(json.dumps(event))
        self.assertEqual(json.loads(await asyncio.wait_for(a.recv(), 2)), event)
        self.assertIs(self.relay.owners["worker"], owner)
        root_event = dict(method="Target.targetCreated", sessionId=owner.root, params={})
        await self.chrome.sockets[0].send(json.dumps(root_event))
        self.assertNotIn("sessionId", json.loads(await asyncio.wait_for(a.recv(), 2)))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(b.recv(), .05)

    async def test_disconnect_keeps_other_client_and_drops_late_reply(self):
        a, b = await self.client(), await self.client()
        await self.call(a, "Test.echo")
        await self.call(b, "Test.echo")
        await a.send(json.dumps(dict(id=999, method="Test.delay", params=dict(seconds=.1, value="old"))))
        await a.close()
        await self.eventually(lambda: len(self.relay.clients) == 1)
        await asyncio.sleep(.15)
        self.assertEqual((await self.call(b, "Test.echo", {"value": "new"}))["result"]["echo"], {"value": "new"})
        self.assertEqual(self.chrome.connections, 1)

    async def test_context_and_browser_global_policy(self):
        a, b = await self.client(), await self.client()
        context = (await self.call(a, "Target.createBrowserContext", {"disposeOnDetach": False}))["result"]["browserContextId"]
        self.assertTrue(next(p for _, m, p in self.chrome.received if m == "Target.createBrowserContext")["disposeOnDetach"])
        for method, params in [
                ("Target.disposeBrowserContext", {"browserContextId": context}),
                ("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": "tmp"}),
                ("Browser.crash", {}), ("Tracing.start", {}),
                ("Security.setIgnoreCertificateErrors", {"ignore": True}),
                ("Target.attachToBrowserTarget", {}),
                ("Target.attachToTarget", {"targetId": "page"}),
                ("Target.sendMessageToTarget", {"sessionId": "x", "message": "{}"})]:
            self.assertIn("error", await self.call(b, method, params), method)
        self.assertIn("result", await self.call(a, "Browser.setDownloadBehavior",
                                               {"browserContextId": context, "behavior": "deny"}))

    async def test_browser_close_disconnects_only_its_client(self):
        a, b = await self.client(), await self.client()
        await self.call(a, "Test.echo")
        await self.call(b, "Test.echo")
        self.assertIn("result", await self.call(a, "Browser.close"))
        await a.wait_closed()
        self.assertIn("result", await self.call(b, "Test.echo"))
        self.assertNotIn("Browser.close", [m for _, m, _ in self.chrome.received])

    async def test_browser_loss_closes_all_and_reconnects_on_demand(self):
        a, b = await self.client(), await self.client()
        await self.call(a, "Test.echo")
        await self.call(b, "Test.echo")
        await self.chrome.sockets[0].close()
        await asyncio.wait_for(asyncio.gather(a.wait_closed(), b.wait_closed()), 2)
        c = await self.client()
        self.assertIn("result", await self.call(c, "Test.echo"))
        self.assertEqual(self.chrome.connections, 2)

    async def test_old_client_cleanup_cannot_detach_from_replacement_upstream(self):
        old_client = Client(Mock(), upstream=object(), root="old")
        a = await self.client()
        await self.call(a, "Test.echo")
        upstream = self.relay.upstream
        with patch.object(self.relay, "send_call", new_callable=AsyncMock) as send:
            await self.relay.release(old_client)
            send.assert_not_called()
        self.assertIs(self.relay.upstream, upstream)
        self.assertIn("result", await self.call(a, "Test.echo"))

    async def test_malformed_upstream_retires_socket_before_reuse(self):
        a = await self.client()
        await self.call(a, "Test.echo")
        await self.chrome.sockets[0].send("[]")
        await asyncio.wait_for(a.wait_closed(), 2)
        b = await self.client()
        self.assertIn("result", await self.call(b, "Test.echo"))
        self.assertEqual(self.chrome.connections, 2)

    async def test_unsupported_browser_sessions_never_open_extra_upstreams(self):
        self.chrome.refuse = lambda m, p: {"code": -32601, "message": "unsupported"} if m == "Target.attachToBrowserTarget" else None
        for _ in range(2):
            ws = await self.client()
            await asyncio.wait_for(ws.wait_closed(), 2)
            self.assertEqual(ws.close_code, 1011)
        self.assertEqual(self.chrome.connections, 1)

    async def test_cleanup_failure_retires_shared_socket(self):
        a, b = await self.client(), await self.client()
        await self.call(a, "Test.echo")
        await self.call(b, "Test.echo")
        self.chrome.refuse = lambda m, p: {"code": -1, "message": "failure"} if m == "Target.detachFromTarget" else None
        await a.close()
        await asyncio.wait_for(b.wait_closed(), 2)

    async def test_malformed_client_does_not_affect_other_client(self):
        a, b = await self.client(), await self.client()
        await self.call(b, "Test.echo")
        await a.send("[]")
        await a.wait_closed()
        self.assertEqual(a.close_code, 1008)
        self.assertIn("result", await self.call(b, "Test.echo"))

    async def test_per_client_pending_limit_preserves_other_clients(self):
        a, b = await self.client(), await self.client()
        await self.call(a, "Test.echo")
        await self.call(b, "Test.echo")
        self.chrome.ignore = lambda m, p: m == "Test.stall"
        for number in range(257):
            await a.send(json.dumps(dict(id=number, method="Test.stall")))
        reply = json.loads(await asyncio.wait_for(a.recv(), 3))
        self.assertEqual(reply["id"], 256)
        self.assertIn("error", reply)
        self.assertIn("result", await self.call(b, "Test.echo"))

    async def test_slow_client_queue_is_bounded(self):
        ws = Mock()
        client = Client(ws)
        for _ in range(257):
            self.relay.enqueue(client, {"method": "Test.event"})
        self.assertFalse(client.active)
        ws.transport.abort.assert_called_once()
        self.assertEqual(client.queue.qsize(), 256)

    async def test_close_cancels_pending_connection(self):
        started = asyncio.Event()
        async def blocked():
            started.set()
            await asyncio.Future()
        with patch.object(self.relay, "establish_endpoint", blocked):
            await self.client()
            await started.wait()
            await asyncio.wait_for(self.relay.close(), 2)


if __name__ == "__main__":
    import unittest
    unittest.main()
