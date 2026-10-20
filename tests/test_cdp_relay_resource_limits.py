"""Bounded resource checks using loopback fake CDP sockets only."""
import asyncio
import json
from unittest.mock import patch

import cdp_relay
from test_cdp_relay_hold import HeldRelayFixture
from websockets.asyncio.client import connect


class RelayResourceTests(HeldRelayFixture):
    async def test_unanswered_calls_do_not_accumulate_between_borrowers(self):
        self.chrome.ignore = lambda method, params: method == "Runtime.getProperties"
        counts = []
        for borrower in range(12):
            ws = await self.client()
            for client_id in range(24):
                await ws.send(json.dumps({"id": client_id, "method": "Runtime.getProperties",
                                          "params": {"objectId": "x" * 8192}}))
            # A reply to this marker proves Chrome received all earlier calls.
            await self.call(ws, "Browser.getVersion")
            await self.leave(ws)
            counts.append(len(self.relay.held.pending))
        self.assertEqual(counts, [0] * 12)
        self.assertEqual(self.chrome.connections, 1)
        # Replies to forgotten wire ids must still be discarded on reuse.
        ws = await self.client()
        await self.call(ws, "Browser.getVersion")
        old_ids = [wire_id for method, wire_id in self.chrome.wire_ids
                   if method == "Runtime.getProperties"]
        for wire_id in old_ids:
            await self.chrome.sockets[0].send(json.dumps({"id": wire_id, "result": {"old": True}}))
        reply = await self.call(ws, "Browser.getVersion", id=999)
        self.assertEqual(reply["id"], 999)
        self.assertEqual(self.relay.held.pending, {})

    async def test_pending_limit_rejects_new_work_and_recovers_after_release(self):
        self.chrome.ignore = lambda method, params: method == "Runtime.getProperties"
        with patch.object(cdp_relay, "MAX_PENDING_CALLS", 16, create=True):
            ws = await self.client()
            for client_id in range(16):
                await ws.send(json.dumps({"id": client_id, "method": "Runtime.getProperties"}))
            await ws.send(json.dumps({"id": 16, "method": "Target.setDiscoverTargets",
                                      "params": {"discover": True}}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
            self.assertEqual(reply["id"], 16)
            self.assertEqual(reply["error"]["code"], -32000)
            self.assertEqual(len(self.relay.held.pending), 16)
            self.assertEqual(self.relay.held.resets, {})
            await self.eventually(lambda: len(self.chrome.wire_ids) == 16)
            await self.chrome.sockets[0].send(json.dumps({"id": self.chrome.wire_ids[0][1], "result": {}}))
            self.assertEqual(json.loads(await asyncio.wait_for(ws.recv(), 2))["id"], 0)
            self.assertIn("result", await self.call(ws, "Browser.getVersion"))
            await self.leave(ws)
            self.assertEqual(self.relay.held.pending, {})
            self.assertEqual(len(self.chrome.received), 17)
            ws = await self.client()
            self.assertIn("result", await self.call(ws, "Browser.getVersion"))
        self.assertEqual(self.chrome.connections, 1)

    async def test_nested_json_within_decoder_limit_preserves_owner(self):
        ws = await self.client()
        # Stay within the decoder limit on all supported Python versions.
        await ws.send('{"id":1,"method":"Runtime.evaluate","params":{"value":'
                      + "[" * 64 + "0" + "]" * 64 + "}}")
        reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
        value = reply["result"]["echo"]["value"]
        for _ in range(64):
            value = value[0]
        self.assertEqual(value, 0)
        self.assertIn("result", await self.call(ws, "Browser.getVersion"))
        self.assertEqual(self.chrome.connections, 1)

    async def test_nonfinite_ids_do_not_poison_next_call(self):
        ws = await self.client()
        for value in ("NaN", "Infinity", "-Infinity", "1e400"):
            await ws.send('{"id":' + value + ',"method":"Runtime.evaluate"}')
            reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
            self.assertEqual(reply["error"]["code"], -32700)
        self.assertEqual(self.chrome.received, [])
        self.assertIn("result", await self.call(ws, "Browser.getVersion"))

    async def test_large_valid_result_and_event_survive(self):
        ws = await connect(self.url, proxy=None, max_size=cdp_relay.MAX_MESSAGE)
        self.addAsyncCleanup(ws.close)
        await self.call(ws, "Browser.getVersion")
        self.chrome.ignore = lambda method, params: method == "Page.captureScreenshot"
        await ws.send(json.dumps({"id": 700, "method": "Page.captureScreenshot"}))
        await self.eventually(lambda: any(m == "Page.captureScreenshot" for m, _ in self.chrome.wire_ids))
        wire_id = next(i for m, i in self.chrome.wire_ids if m == "Page.captureScreenshot")
        payload = "x" * (4 * 1024 * 1024)
        await self.chrome.sockets[0].send(json.dumps({"id": wire_id, "result": {"data": payload}}))
        reply = json.loads(await asyncio.wait_for(ws.recv(), 4))
        self.assertEqual(reply, {"id": 700, "result": {"data": payload}})
        event = {"method": "Tracing.dataCollected", "params": {"value": [payload]}}
        await self.chrome.sockets[0].send(json.dumps(event))
        self.assertEqual(json.loads(await asyncio.wait_for(ws.recv(), 4)), event)
        await self.leave(ws)
        self.assertEqual(self.relay.held.pending, {})

    async def test_stop_completes_when_downstream_does_not_read(self):
        ws = await connect(self.url, proxy=None, max_size=cdp_relay.MAX_MESSAGE, max_queue=1)
        self.addAsyncCleanup(ws.close)
        await self.call(ws, "Browser.getVersion")
        owner = self.relay.held.owner
        # Force real TCP backpressure with bounded writes. Do not depend on the
        # host's receive-window size to decide when the reader is slow.
        ws.transport.pause_reading()
        owner.transport.set_write_buffer_limits(high=1024)
        event = json.dumps({"method": "Tracing.dataCollected", "params": {"data": "x" * (1024 * 1024)}})

        async def flood():
            for _ in range(32):
                await self.chrome.sockets[0].send(event)

        sending = asyncio.create_task(flood())
        try:
            await self.eventually(lambda: owner.transport.get_write_buffer_size() > 1024)
            await asyncio.wait_for(self.relay.close(), 5)
            self.assertEqual(self.relay.held.pending, {})
            self.assertFalse(self.relay.clients)
            self.assertTrue(self.relay.held.reader is None or self.relay.held.reader.done())
        finally:
            ws.transport.resume_reading()
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)


if __name__ == "__main__":
    import unittest
    unittest.main()
