"""Real TCP/websocket lifecycle tests; no Chrome, native UI or user profile."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cdp_relay import Endpoint, NoFocus, Relay, RELAY_PATH, read_endpoint
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus


class RecordingFocus(NoFocus):
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.calls = 0
        self.finished = 0
        self.ready = asyncio.Event()
        self.release = None

    @asynccontextmanager
    async def request(self, endpoint):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.ready.set()
        try:
            if self.release is not None:
                await self.release.wait()
            yield
        finally:
            self.active -= 1
            self.finished += 1


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name)
        self.connections = 0
        self.closed = 0
        self.upstreams = []
        self.upstream = await serve(self.echo, "127.0.0.1", 0, ping_interval=None)
        self.addAsyncCleanup(self.stop_upstream)
        self.write_endpoint(self.upstream)
        self.focus = RecordingFocus()
        self.relay = await Relay(self.profile, self.focus, port=0, open_timeout=.3).start()
        self.addAsyncCleanup(self.relay.close)
        self.url = f"ws://127.0.0.1:{self.relay.port}{RELAY_PATH}"

    def write_endpoint(self, server, path="/devtools/browser/test"):
        (self.profile / "DevToolsActivePort").write_text(f"{server.sockets[0].getsockname()[1]}\n{path}\n")

    async def stop_upstream(self):
        self.upstream.close()
        await self.upstream.wait_closed()

    async def echo(self, ws):
        self.connections += 1
        self.upstreams.append(ws)
        number = self.connections
        try:
            async for message in ws:
                if message == "close-me":
                    await ws.close(1012, "restarting")
                else:
                    data = json.loads(message)
                    await ws.send(json.dumps({"id": data["id"], "result": data["params"], "connection": number}))
        except ConnectionClosed:
            pass
        finally:
            self.closed += 1

    async def client(self):
        client = await connect(self.url, proxy=None)
        self.addAsyncCleanup(client.close)
        return client

    async def exchange(self, client, value):
        await client.send(json.dumps({"id": 1, "params": value}))
        return json.loads(await asyncio.wait_for(client.recv(), 2))

    async def eventually(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail("Condition did not become true")

    async def http(self, path="/json/version", headers=""):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.relay.port)
        writer.write((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.relay.port}\r\n" + headers + "\r\n").encode())
        await writer.drain()
        response = (await reader.read()).decode()
        writer.close()
        await writer.wait_closed()
        return response

    async def test_discovery_does_not_connect_or_arm(self):
        response = await self.http()
        self.assertIn("200 OK", response)
        self.assertEqual(response.lower().count("content-type:"), 1)
        self.assertIn("application/json", response)
        self.assertIn(self.url, response)
        self.assertEqual(self.connections, 0)
        self.assertEqual(self.focus.calls, 0)

    async def test_web_origin_cannot_discover_or_connect(self):
        self.assertIn("403 Forbidden", await self.http(headers="Origin: https://example.com\r\n"))
        with self.assertRaises(InvalidStatus):
            await connect(self.url, origin="https://example.com", proxy=None)
        self.assertEqual(self.focus.calls, 0)

    async def test_bad_host_and_unknown_path_cannot_connect(self):
        with self.assertRaises(InvalidStatus):
            await connect(self.url, additional_headers={"Host": "evil.example"}, proxy=None)
        self.assertIn("404 Not Found", await self.http("/json/new"))
        self.assertEqual(self.focus.calls, 0)

    async def test_concurrent_clients_keep_identical_ids_separate(self):
        a, b = await self.client(), await self.client()
        ra, rb = await asyncio.gather(self.exchange(a, "one"), self.exchange(b, "two"))
        self.assertEqual((ra["id"], rb["id"]), (1, 1))
        self.assertEqual((ra["result"], rb["result"]), ("one", "two"))
        self.assertNotEqual(ra["connection"], rb["connection"])
        self.assertEqual(self.focus.peak, 1)
        await a.close()
        self.assertEqual((await self.exchange(b, "still alive"))["result"], "still alive")

    async def test_reconnect_gets_new_upstream_and_fresh_arm(self):
        a = await self.client()
        first = await self.exchange(a, "first")
        await a.close()
        await self.eventually(lambda: self.closed == 1)
        b = await self.client()
        second = await self.exchange(b, "second")
        self.assertNotEqual(first["connection"], second["connection"])
        self.assertEqual(self.focus.calls, 2)

    async def test_disconnect_while_waiting_for_arm_cancels_request(self):
        self.focus.release = asyncio.Event()
        client = await self.client()
        await asyncio.wait_for(self.focus.ready.wait(), 1)
        await client.close()
        await self.eventually(lambda: self.focus.finished == 1)
        self.assertEqual(self.connections, 0)
        self.assertEqual(self.focus.active, 0)

    async def test_queued_disconnect_does_not_open_chrome(self):
        self.focus.release = asyncio.Event()
        a = await self.client()
        await self.focus.ready.wait()
        b = await self.client()
        await self.eventually(lambda: len(self.relay.clients) == 2)
        await b.close()
        await self.eventually(lambda: len(self.relay.clients) == 1)
        self.focus.release.set()
        await self.exchange(a, "ok")
        self.assertEqual(self.focus.calls, 1)
        self.assertEqual(self.connections, 1)

    async def test_without_hold_browser_close_is_forwarded_like_a_direct_connection(self):
        client = await self.client()
        await client.send(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
        reply = json.loads(await asyncio.wait_for(client.recv(), 2))
        self.assertEqual(reply["connection"], 1, "Browser.close did not reach Chrome")

    async def test_upstream_loss_closes_only_its_downstream(self):
        a, b = await self.client(), await self.client()
        await asyncio.gather(self.exchange(a, "one"), self.exchange(b, "two"))
        await a.send("close-me")
        await asyncio.wait_for(a.wait_closed(), 2)
        self.assertEqual(a.close_code, 1012)
        self.assertEqual((await self.exchange(b, "ok"))["result"], "ok")

    async def test_chrome_restart_is_picked_up_on_next_connection(self):
        a = await self.client()
        await self.exchange(a, "before")
        await self.stop_upstream()
        await asyncio.wait_for(a.wait_closed(), 2)
        self.upstream = await serve(self.echo, "127.0.0.1", 0)
        self.write_endpoint(self.upstream, "/devtools/browser/restarted")
        b = await self.client()
        self.assertEqual((await self.exchange(b, "after"))["result"], "after")
        self.assertEqual(self.focus.calls, 2)

    async def test_missing_endpoint_fails_without_arming(self):
        (self.profile / "DevToolsActivePort").unlink()
        self.assertIn("503 Service Unavailable", await self.http())
        client = await self.client()
        await asyncio.wait_for(client.wait_closed(), 2)
        self.assertEqual(client.close_code, 1011)
        self.assertEqual(self.focus.calls, 0)

    async def test_shutdown_cancels_pending_connection(self):
        self.focus.release = asyncio.Event()
        client = await self.client()
        await self.focus.ready.wait()
        await asyncio.wait_for(self.relay.close(), 2)
        self.assertEqual(self.focus.active, 0)
        self.assertEqual(self.connections, 0)
        await client.wait_closed()

    async def test_disconnect_during_chrome_handshake_cancels_upstream(self):
        await self.stop_upstream()
        handshake = asyncio.Event()
        cancelled = asyncio.Event()
        async def pending(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            handshake.set()
            await reader.read()
            cancelled.set()
            writer.close()
            await writer.wait_closed()
        self.upstream = await asyncio.start_server(pending, "127.0.0.1", 0)
        self.write_endpoint(self.upstream)
        client = await self.client()
        await asyncio.wait_for(handshake.wait(), 1)
        await client.close()
        await self.eventually(lambda: self.focus.finished == 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual(self.connections, 0)
        self.assertEqual(self.focus.active, 0)

    async def test_chrome_handshake_timeout_releases_focus_and_closes_client(self):
        await self.stop_upstream()
        cancelled = asyncio.Event()
        async def pending(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            await reader.read()
            cancelled.set()
            writer.close()
            await writer.wait_closed()
        self.upstream = await asyncio.start_server(pending, "127.0.0.1", 0)
        self.write_endpoint(self.upstream)
        client = await self.client()
        await asyncio.wait_for(client.wait_closed(), 2)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual(client.close_code, 1011)
        self.assertEqual(self.focus.active, 0)
        self.assertEqual(self.connections, 0)


if __name__ == "__main__":
    unittest.main()
