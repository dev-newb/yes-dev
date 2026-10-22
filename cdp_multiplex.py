"""Concurrent flattened CDP sessions over one browser WebSocket.

Each client owns a browser-target session. Chromium scopes discovery and
auto-attach to that session; the relay maps wire IDs and child-session owners.
Tabs are shared. This is connection sharing, not a security boundary.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json

from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from cdp_relay import LOGGER, MAX_MESSAGE, MAX_PENDING_CALLS, Relay, read_endpoint


@dataclass(eq=False)
class Client:
    ws: object
    upstream: object = None
    root: str = ""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(256))
    queued_bytes: int = 0
    contexts: set = field(default_factory=set)
    active: bool = True


class MultiplexRelay(Relay):
    def __init__(self, *args, **kwargs):
        kwargs.pop("hold", None)
        super().__init__(*args, hold=False, **kwargs)
        self.upstream = self.endpoint = self.reader = None
        self.lock = asyncio.Lock()
        self.pending = {}
        self.owners = {}
        self.next_id = 0
        self.stopping = False
        self.opening = None

    async def start(self):
        await super().start()
        self.status(hold=True, multiplex=True, held_state="empty")
        return self

    async def ensure_upstream(self):
        async with self.lock:
            if self.stopping:
                raise RuntimeError("Relay is stopping")
            if self.upstream is not None:
                if (self.upstream.state is State.OPEN and self.reader is not None and not self.reader.done()
                        and self.endpoint == read_endpoint(self.profile)):
                    return
                await self.retire()
            self.status(held_state="opening")
            self.opening = asyncio.create_task(self.establish_endpoint())
            try:
                self.upstream, self.endpoint = await self.opening
            finally:
                self.opening = None
            self.reader = asyncio.create_task(self.read_upstream(self.upstream))
            self.status(hold=True, held=True, held_state="held", last_error="")

    async def send_call(self, method, params, session=None, client=None, original=None):
        if len(self.pending) >= MAX_PENDING_CALLS:
            raise RuntimeError("Too many pending browser calls")
        self.next_id += 1
        wire_id = self.next_id
        if original is not None:
            # Retain reply routing only, not a potentially large command body.
            original = {key: original[key] for key in ("id", "sessionId") if key in original}
        future = asyncio.get_running_loop().create_future() if client is None else None
        disposed_context = params.get("browserContextId") if method == "Target.disposeBrowserContext" else None
        self.pending[wire_id] = (client, original, method, future, disposed_context)
        message = dict(id=wire_id, method=method, params=params)
        if session:
            message["sessionId"] = session
        try:
            await asyncio.wait_for(self.upstream.send(json.dumps(message)), 5)
            if future is not None:
                response = await asyncio.wait_for(future, 5)
                if "error" in response:
                    raise RuntimeError(str(response["error"]))
                return response.get("result", {})
        except BaseException:
            self.pending.pop(wire_id, None)
            raise

    def enqueue(self, client, message):
        if not client.active:
            return
        raw = json.dumps(message)
        size = len(raw.encode("utf-8"))
        if client.queue.full() or client.queued_bytes + size > MAX_MESSAGE:
            client.active = False
            client.ws.transport.abort()
            return
        client.queued_bytes += size
        client.queue.put_nowait((raw, size))

    async def write_client(self, client):
        while True:
            raw, size = await client.queue.get()
            client.queued_bytes -= size
            try:
                await asyncio.wait_for(client.ws.send(raw), 2)
            except (TimeoutError, ConnectionClosed):
                client.ws.transport.abort()
                return

    async def read_upstream(self, upstream):
        try:
            async for raw in upstream:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("Invalid browser message")
                session = message.get("sessionId")
                owner = self.owners.get(session)
                if "id" in message:
                    pending = self.pending.pop(message["id"], None)
                    if pending is None:
                        continue
                    client, original, method, future, disposed_context = pending
                    if future is not None:
                        if not future.done():
                            future.set_result(message)
                        continue
                    owner = client
                    result = message.get("result", {})
                    if method == "Target.attachToTarget" and "sessionId" in result:
                        self.owners[result["sessionId"]] = owner
                    if method == "Target.createBrowserContext" and "browserContextId" in result:
                        owner.contexts.add(result["browserContextId"])
                    if method == "Target.disposeBrowserContext" and "error" not in message:
                        owner.contexts.discard(disposed_context)
                    message["id"] = original["id"]
                    if "sessionId" in original:
                        message["sessionId"] = original["sessionId"]
                    else:
                        message.pop("sessionId", None)
                elif owner is not None:
                    params = message.get("params", {})
                    child = params.get("sessionId")
                    if message.get("method") == "Target.attachedToTarget" and child:
                        self.owners[child] = owner
                    elif message.get("method") == "Target.detachedFromTarget" and child:
                        self.owners.pop(child, None)
                    if session == owner.root:
                        message.pop("sessionId", None)
                if owner is not None:
                    self.enqueue(owner, message)
        except (ConnectionClosed, ValueError, TypeError):
            pass
        finally:
            upstream.transport.abort()
            # Only this generation can own the maps: replacement waits for us.
            for client in set(self.owners.values()):
                client.active = False
                client.ws.transport.abort()
            for _, _, _, future, _ in self.pending.values():
                if future is not None and not future.done():
                    future.set_exception(RuntimeError("Browser connection ended"))
            self.pending.clear()
            self.owners.clear()
            self.status(held=False, held_state="disconnected")

    def policy_error(self, client, message):
        method, params = message["method"], message.get("params", {})
        if any(key in params and not isinstance(params[key], str) for key in ("sessionId", "browserContextId")):
            return "Session and context IDs must be strings"
        session = message.get("sessionId")
        if session and self.owners.get(session) is not client:
            return "Session belongs to another client or has ended"
        if method in {"Target.attachToBrowserTarget", "Target.exposeDevToolsProtocol",
                      "Target.sendMessageToTarget", "Target.setRemoteLocations"}:
            return "Command is not supported by the shared relay"
        if method == "Page.setDownloadBehavior":
            return "Use Browser.setDownloadBehavior with a context created by this client"
        if method in {"Target.attachToTarget", "Target.setAutoAttach"} and params.get("flatten") is not True:
            return "The shared relay requires flatten=true"
        if method == "Target.detachFromTarget" and (self.owners.get(params.get("sessionId")) is not client
                                                    or params.get("sessionId") == client.root):
            return "Detach requires a session owned by this client"
        context = params.get("browserContextId")
        if method == "Target.disposeBrowserContext" and context not in client.contexts:
            return "Only a context created by this client can be disposed"
        if method.startswith("Browser.") and method not in {
                "Browser.getVersion", "Browser.getBrowserCommandLine", "Browser.getWindowForTarget",
                "Browser.getWindowBounds", "Browser.getHistogram", "Browser.getHistograms", "Browser.close"}:
            if method not in {"Browser.setDownloadBehavior", "Browser.grantPermissions",
                              "Browser.resetPermissions", "Browser.setPermission"} or context not in client.contexts:
                return "Browser-wide changes require a private context or a direct connection"
        if method.startswith(("Tracing.", "Security.", "SystemInfo.")):
            return "This browser-wide domain requires a direct connection"
        return None

    async def from_client(self, client, raw):
        if client.upstream is not self.upstream or not client.active:
            client.ws.transport.abort()
            return
        try:
            message = json.loads(raw)
            if (not isinstance(message, dict) or type(message.get("id")) is not int
                    or not isinstance(message.get("method"), str)
                    or not isinstance(message.get("params", {}), dict)
                    or ("sessionId" in message and not isinstance(message["sessionId"], str))):
                raise ValueError("Invalid CDP command")
        except (ValueError, TypeError):
            await client.ws.close(1008, "Expected a CDP command object")
            return
        error = self.policy_error(client, message)
        outstanding = sum(p[0] is client for p in self.pending.values())
        if outstanding >= 256:
            error = "Too many pending calls for this client"
        if error:
            reply = dict(id=message["id"], error=dict(code=-32000, message=error))
            if "sessionId" in message:
                reply["sessionId"] = message["sessionId"]
            self.enqueue(client, reply)
            return
        if message["method"] == "Browser.close":
            # A disconnect must not close either shared browser.
            reply = dict(id=message["id"], result={})
            if "sessionId" in message:
                reply["sessionId"] = message["sessionId"]
            await asyncio.wait_for(client.ws.send(json.dumps(reply)), 2)
            await client.ws.close(1000, "Client disconnected; browser stays open")
            return
        params = dict(message.get("params", {}))
        if message["method"] == "Target.createBrowserContext":
            params["disposeOnDetach"] = True
        await self.send_call(message["method"], params, message.get("sessionId") or client.root,
                             client, message)

    async def release(self, client):
        client.active = False
        # Free its call budget before submitting the internal detach command.
        self.pending = {key: value for key, value in self.pending.items() if value[0] is not client}
        try:
            if (client.root and client.upstream is self.upstream and self.upstream is not None
                    and self.upstream.state is State.OPEN):
                # Detaching the browser session disposes its contexts and all
                # child sessions, including replies still in flight.
                await self.send_call("Target.detachFromTarget", {"sessionId": client.root})
        except Exception as exc:
            if not self.stopping and self.upstream is not None and self.upstream.state is State.OPEN:
                LOGGER.warning("Client cleanup failed; retiring shared connection: %s", exc)
            await self.retire()
        finally:
            self.owners = {session: owner for session, owner in self.owners.items() if owner is not client}

    async def handle(self, downstream):
        if self.stopping or len(self.clients) >= self.max_clients:
            await downstream.close(1013, "Relay is busy or stopping")
            return
        self.clients.add(downstream)
        client = Client(downstream)
        writer = asyncio.create_task(self.write_client(client))
        self.status(connections=len(self.clients))
        try:
            await self.ensure_upstream()
            # Serialize attachment with other attachment/retirement work.
            async with self.lock:
                try:
                    result = await self.send_call("Target.attachToBrowserTarget", {})
                except (TimeoutError, asyncio.CancelledError) as exc:
                    # The browser may have created a session whose reply never
                    # arrived. Its unknown ID cannot be detached individually.
                    await self.retire()
                    if isinstance(exc, TimeoutError):
                        raise RuntimeError("Browser session attachment timed out; shared connection retired") from exc
                    raise
                client.root = result["sessionId"]
                client.upstream = self.upstream
                self.owners[client.root] = client
                self.status(last_error="")
            async for raw in downstream:
                await self.from_client(client, raw)
        except ConnectionClosed:
            pass
        except Exception as exc:
            LOGGER.warning("Shared connection failed: %s", exc)
            self.status(last_error=str(exc))
            await downstream.close(1011, "Browser connection failed; reconnect")
        finally:
            async with self.lock:
                await self.release(client)
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            self.clients.discard(downstream)
            self.status(connections=len(self.clients))

    async def retire(self):
        if self.upstream is not None:
            try:
                await asyncio.wait_for(self.upstream.close(), 1)
            except TimeoutError:
                self.upstream.transport.abort()
        if self.reader is not None:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        self.upstream = self.endpoint = self.reader = None

    async def close(self):
        self.stopping = True
        if self.opening is not None:
            self.opening.cancel()
            await asyncio.gather(self.opening, return_exceptions=True)
        if self.upstream is not None:
            self.upstream.transport.abort()
        # Retire first so downstream handlers do not wait for cleanup calls.
        async with self.lock:
            await self.retire()
        await super().close()
