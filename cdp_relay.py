"""Loopback CDP relay: one upstream websocket per client, unchanged messages.

Only connection establishment is serialized, so the early-focus guard has one
unambiguous pending request. Established clients run concurrently. This is not
the persistent, multiplexed proxy described in docs/planning/cdp-proxy.md.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
import json
import logging
from pathlib import Path
import re

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

LOGGER = logging.getLogger("yesdev.relay")
RELAY_PATH = "/devtools/browser/yesdev"
MAX_MESSAGE = 64 * 1024 * 1024


@dataclass(frozen=True)
class Endpoint:
    port: int
    path: str

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}{self.path}"


def read_endpoint(profile: Path) -> Endpoint:
    """Use only Chrome's loopback browser endpoint, never an arbitrary URL."""
    data = (profile / "DevToolsActivePort").read_text().splitlines()
    if (len(data) < 2 or not data[0].isdigit() or not 0 < int(data[0]) <= 65535
            or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", data[1])):
        raise ValueError("Chrome's debugging endpoint is invalid")
    return Endpoint(int(data[0]), data[1])


class Relay:
    def __init__(self, profile, focus, port=9333, status=None, open_timeout=20, max_clients=16):
        self.profile = Path(profile)
        self.focus = focus
        self.port = port
        self.status = status or (lambda **_: None)
        self.open_timeout = open_timeout
        self.max_clients = max_clients
        self.open_lock = asyncio.Lock()
        self.clients = set()
        self.server = None

    @property
    def address(self):
        return f"http://127.0.0.1:{self.port}"

    def process_request(self, connection, request):
        # Reject website-initiated requests and DNS rebinding before discovery
        # or a websocket can cause any connection to Chrome.
        try:
            host = request.headers.get("Host", "")
            origin = request.headers.get("Origin")
        except Exception:
            return connection.respond(HTTPStatus.BAD_REQUEST, "Invalid request headers\n")
        if origin is not None or host not in {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}:
            return connection.respond(HTTPStatus.FORBIDDEN, "Local application connections only\n")
        if request.path.rstrip("/") == "/json/version":
            try:
                read_endpoint(self.profile)
            except (OSError, ValueError):
                return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE,
                                          "Open Chrome and enable Remote Debugging first.\n")
            response = connection.respond(HTTPStatus.OK, json.dumps({
                "Browser": "Yes, Dev relay", "Protocol-Version": "1.3",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}{RELAY_PATH}",
            }) + "\n")
            del response.headers["Content-Type"]
            response.headers["Content-Type"] = "application/json; charset=utf-8"
            response.headers["Cache-Control"] = "no-store"
            return response
        if request.path != RELAY_PATH:
            return connection.respond(HTTPStatus.NOT_FOUND, "Use /json/version or the browser websocket.\n")
        if len(self.clients) >= self.max_clients:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "Relay is busy; reconnect shortly.\n")
        return None

    async def establish(self):
        # Lock only until Chrome grants this connection. CDP streams are never
        # shared, remapped or replayed, and their lifetimes are independent.
        async with self.open_lock:
            endpoint = read_endpoint(self.profile)
            async with self.focus.request(endpoint):
                if read_endpoint(self.profile) != endpoint:
                    raise RuntimeError("Chrome restarted before the connection; reconnect")
                return await connect(endpoint.url, proxy=None, open_timeout=self.open_timeout,
                                     close_timeout=1, ping_interval=None, max_size=MAX_MESSAGE,
                                     max_queue=16, compression=None)

    @staticmethod
    async def forward(source, destination):
        try:
            async for message in source:
                await destination.send(message)
        except ConnectionClosed:
            pass
        finally:
            # Preserve ordinary close codes, including a normal client detach.
            code = source.close_code
            if code not in (1000, 1001, 1008, 1009, 1011, 1012, 1013):
                code = 1011
            await destination.close(code or 1000)

    async def handle(self, downstream):
        if len(self.clients) >= self.max_clients:
            await downstream.close(1013, "Relay is busy")
            return
        self.clients.add(downstream)
        self.status(connections=len(self.clients))
        upstream = None
        opening = asyncio.create_task(self.establish())
        disconnected = asyncio.create_task(downstream.wait_closed())
        pumps = []
        try:
            ready, _ = await asyncio.wait((opening, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if disconnected in ready:
                return
            upstream = opening.result()
            self.status(last_error="")
            LOGGER.info("Client connected (%s active)", len(self.clients))
            pumps = [asyncio.create_task(self.forward(downstream, upstream)),
                     asyncio.create_task(self.forward(upstream, downstream))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("Connection failed: %s", exc)
            self.status(last_error=str(exc))
            await downstream.close(1011, "Chrome connection failed; check Yes, Dev settings")
        finally:
            opening.cancel()
            disconnected.cancel()
            for task in pumps:
                task.cancel()
            outcomes = await asyncio.gather(opening, disconnected, *pumps, return_exceptions=True)
            # A disconnect can race with a successful upstream handshake. It
            # still owns that socket even if handle() never assigned upstream.
            if upstream is None and not isinstance(outcomes[0], BaseException):
                upstream = outcomes[0]
            if upstream is not None:
                await upstream.close()
            self.clients.discard(downstream)
            self.status(connections=len(self.clients))
            LOGGER.info("Client disconnected (%s active)", len(self.clients))

    async def start(self):
        self.server = await serve(self.handle, "127.0.0.1", self.port,
                                  process_request=self.process_request, origins=[None],
                                  max_size=MAX_MESSAGE, max_queue=16, compression=None,
                                  close_timeout=1, ping_interval=None)
        self.port = self.server.sockets[0].getsockname()[1]
        self.status(state="listening", url=self.address, connections=0, last_error="")
        return self

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        await self.focus.close()
        self.status(state="stopped", connections=0)


class NoFocus:
    """For transport regression tests, without macOS permissions or UI."""
    @asynccontextmanager
    async def request(self, endpoint):
        yield

    async def close(self):
        pass
