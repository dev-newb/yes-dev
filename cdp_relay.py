"""Loopback CDP relay.

By default: one upstream websocket per client, unchanged messages. Only
connection establishment is serialized, so the early-focus guard has one
unambiguous pending request. Established clients run concurrently.

With hold=True (phase 1 of docs/planning/cdp-proxy.md): one approved Chrome
connection is kept open and lent to one client at a time, so Chrome asks once
per launch instead of once per client. Message ids are remapped so a departing
client's late replies never reach the next one, and what a client changed in
Chrome is undone when it leaves. A second client arriving while the held
connection is lent gets its own upstream connection, as without hold.
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
import time

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

LOGGER = logging.getLogger("yesdev.relay")
RELAY_PATH = "/devtools/browser/yesdev"
MAX_MESSAGE = 64 * 1024 * 1024
HOLD_RESET_TIMEOUT = 2.0   # seconds to undo a departed client's state
HOLD_RESET_ROUNDS = 3      # sessions can attach while the first round runs
# Cleanup whose failure only means the thing was already gone: a session that
# detached itself, a context its client disposed. Any other failed reset leaves
# state behind that the next client would inherit, so the connection is retired.
IDEMPOTENT_CLEANUP = ("Target.detachFromTarget", "Target.disposeBrowserContext")
PRIME_IDLE_S = 2.0      # no keyboard or mouse input this long before a launch-time prompt
PRIME_WINDOW_S = 60.0   # after Chrome starts, keep waiting for a good moment this long
_CLOSE_CODES = (1000, 1001, 1008, 1009, 1011, 1012, 1013)


def _is_browser_close(raw):
    """Browser.close in any frame, parsed only when the text could hold it."""
    if not isinstance(raw, str) or "Browser.close" not in raw:
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if isinstance(message, dict) and message.get("method") == "Browser.close":
        return message
    return None


def _close_code(code):
    """Pass ordinary close codes through; report anything else as an error."""
    return code if code in _CLOSE_CODES else (1011 if code else 1000)


def _reset_for(method, params):
    """The browser-level call that undoes one a client made, or None.

    A client's per-session state - emulation, interception on a page - ends
    when its sessions are detached. These are the settings a client can make
    on the browser itself, which outlive its sessions.
    """
    context = {"browserContextId": params["browserContextId"]} if "browserContextId" in params else {}
    if method == "Target.setDiscoverTargets":
        return "Target.setDiscoverTargets", {"discover": False}
    if method == "Target.setAutoAttach":
        # Chrome rejects browser-level auto-attach without flatten, even to turn
        # it off (-32602, seen live on 154.0.8037.97), and the client's setting
        # then survives into the next client's session.
        return "Target.setAutoAttach", {"autoAttach": False, "waitForDebuggerOnStart": False,
                                        "flatten": True}
    if method == "Browser.setDownloadBehavior":
        return "Browser.setDownloadBehavior", {"behavior": "default", **context}
    if method == "Fetch.enable":
        return "Fetch.disable", {}
    if method == "Security.setIgnoreCertificateErrors":
        return "Security.setIgnoreCertificateErrors", {"ignore": False}
    if method in ("Browser.grantPermissions", "Browser.setPermission"):
        return "Browser.resetPermissions", context
    return None


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
    def __init__(self, profile, focus, port=9333, status=None, open_timeout=20, max_clients=16,
                 hold=False):
        self.profile = Path(profile)
        self.focus = focus
        self.port = port
        self.status = status or (lambda **_: None)
        self.open_timeout = open_timeout
        self.max_clients = max_clients
        self.open_lock = asyncio.Lock()
        self.clients = set()
        self.server = None
        self.held = HeldConnection(self) if hold else None

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
        return (await self.establish_endpoint())[0]

    async def establish_endpoint(self):
        # Lock only until Chrome grants this connection. Without hold, CDP
        # streams are never shared, remapped or replayed, and their lifetimes
        # are independent.
        async with self.open_lock:
            endpoint = read_endpoint(self.profile)
            async with self.focus.request(endpoint):
                if read_endpoint(self.profile) != endpoint:
                    raise RuntimeError("Chrome restarted before the connection; reconnect")
                upstream = await connect(endpoint.url, proxy=None, open_timeout=self.open_timeout,
                                         close_timeout=1, ping_interval=None, max_size=MAX_MESSAGE,
                                         max_queue=16, compression=None)
                return upstream, endpoint

    @staticmethod
    async def forward(source, destination):
        try:
            async for message in source:
                await destination.send(message)
        except ConnectionClosed:
            pass
        finally:
            # Preserve ordinary close codes, including a normal client detach.
            await destination.close(_close_code(source.close_code))

    @staticmethod
    async def forward_guarded(source, destination):
        """Client to Chrome while Chrome is shared: Browser.close ends this client.

        With hold on, a second client's own connection reaches the same Chrome
        as the held one, so its Browser.close would close the browser under
        everyone. Without hold the relay stays a plain pipe, as direct.
        """
        try:
            async for message in source:
                close = _is_browser_close(message)
                if close is not None:
                    if isinstance(close.get("id"), int):
                        await source.send(json.dumps({"id": close["id"], "result": {}}))
                    await source.close(1000, "Browser.close ends this client; Chrome stays open")
                    return
                await destination.send(message)
        except ConnectionClosed:
            pass
        finally:
            await destination.close(_close_code(source.close_code))

    async def handle(self, downstream):
        if len(self.clients) >= self.max_clients:
            await downstream.close(1013, "Relay is busy")
            return
        self.clients.add(downstream)
        self.status(connections=len(self.clients))
        try:
            if self.held is not None and await self.serve_held(downstream):
                return
            await self.serve_own(downstream)
        finally:
            self.clients.discard(downstream)
            self.status(connections=len(self.clients))
            LOGGER.info("Client disconnected (%s active)", len(self.clients))

    async def serve_held(self, downstream):
        """Lend the held connection. False means it is busy: open one of our own."""
        held = self.held
        if held.busy():
            return False
        acquiring = asyncio.create_task(held.acquire(downstream))
        disconnected = asyncio.create_task(downstream.wait_closed())
        try:
            ready, _ = await asyncio.wait((acquiring, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if acquiring not in ready:
                return True       # left while Chrome was being asked; release() tidies up
            if not acquiring.result():
                return False
            self.status(last_error="")
            LOGGER.info("Client using the held connection (%s active)", len(self.clients))
            async for raw in downstream:
                await held.from_client(downstream, raw)
            return True
        except ConnectionClosed:
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("Held connection failed: %s", exc)
            self.status(last_error=str(exc))
            await downstream.close(1011, "Chrome connection failed; check Yes, Dev settings")
            return True
        finally:
            disconnected.cancel()
            if not acquiring.done():
                acquiring.cancel()
            await asyncio.gather(acquiring, disconnected, return_exceptions=True)
            await held.release(downstream)

    async def serve_own(self, downstream):
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
            outbound = self.forward_guarded if self.held is not None else self.forward
            pumps = [asyncio.create_task(outbound(downstream, upstream)),
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

    async def start(self):
        self.server = await serve(self.handle, "127.0.0.1", self.port,
                                  process_request=self.process_request, origins=[None],
                                  max_size=MAX_MESSAGE, max_queue=16, compression=None,
                                  close_timeout=1, ping_interval=None)
        self.port = self.server.sockets[0].getsockname()[1]
        self.status(state="listening", url=self.address, connections=0, last_error="",
                    hold=self.held is not None, held=False)
        return self

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        if self.held is not None:
            await self.held.close()
        await self.focus.close()
        self.status(state="stopped", connections=0)


class HeldConnection:
    """One approved Chrome connection, lent to one client at a time.

    Chrome numbers nothing itself: every id on the wire is ours, remapped from
    each client's own numbering, so a reply still in flight when its client
    leaves is dropped rather than delivered to the next one. Sessions a client
    attached, browser contexts it created and the browser-level settings in
    _reset_for are undone when it leaves, before the connection is lent again.
    Targets it opened stay open, as they would after a direct disconnect.
    """

    def __init__(self, relay):
        self.relay = relay
        self.upstream = None
        self.endpoint = None
        self.reader = None
        self.owner = None
        self.generation = 0          # bumped on every lend and every release
        self.next_id = 0
        self.pending = {}            # upstream id -> Future, or (generation, client id, method, params)
        self.sessions = {}           # top-level session id -> generation that attached it
        self.retired = set()         # sessions detached by a reset; their stragglers are dropped
        self.contexts = set()
        self.resets = {}
        self.lock = asyncio.Lock()
        self.reset_done = asyncio.Event()
        self.reset_done.set()

    @property
    def open(self):
        return self.upstream is not None and self.upstream.state is State.OPEN

    def busy(self):
        return self.owner is not None or self.lock.locked()

    async def acquire(self, downstream):
        """Lend the connection to downstream, opening it first if need be."""
        async with self.lock:
            if self.owner is not None:
                return False
            try:
                await asyncio.wait_for(self.reset_done.wait(), HOLD_RESET_TIMEOUT)
            except asyncio.TimeoutError:
                return False
            await self._open_if_needed()
            self.generation += 1
            self.owner = downstream
            return True

    async def _open_if_needed(self):
        """With the lock held: make sure the connection is open to the current Chrome."""
        endpoint = read_endpoint(self.relay.profile)
        if self.open and endpoint == self.endpoint:
            return False
        await self.close_upstream()
        upstream, endpoint = await self.relay.establish_endpoint()
        self.upstream, self.endpoint = upstream, endpoint
        self.reader = asyncio.create_task(self.read_chrome(upstream))
        self.relay.status(held=True)
        LOGGER.info("Holding a Chrome connection open for later clients")
        return True

    async def prime(self):
        """Open the held connection with no client waiting. False if there was nothing to do."""
        if self.busy() or not self.reset_done.is_set():
            return False
        async with self.lock:
            if self.owner is not None:
                return False
            return await self._open_if_needed()

    async def release(self, downstream):
        if self.owner is not downstream:
            return
        self.owner = None
        self.generation += 1
        self.reset_done.clear()
        try:
            try:
                clean = await self.reset()
            except Exception as exc:
                LOGGER.warning("Could not reset the held connection: %s", exc)
                clean = False
            if not clean and self.upstream is not None:
                # Whatever the client left behind is still in effect. Closing the
                # connection makes Chrome drop all of it; the next client pays
                # one prompt instead of inheriting it.
                LOGGER.warning("Retiring the held connection: its reset did not complete")
                await self.close_upstream()
                self.relay.status(held=False)
        finally:
            self.reset_done.set()

    async def reset(self):
        """Undo the departed client's browser settings, then detach its sessions.

        True only when every reset was accepted. A session or context that was
        already gone counts as cleaned; any other error, or no answer within
        HOLD_RESET_TIMEOUT, does not.
        """
        calls = list(self.resets.values())
        self.resets.clear()
        clean = True
        for _ in range(HOLD_RESET_ROUNDS):
            calls += [("Target.detachFromTarget", {"sessionId": s}) for s in sorted(self.sessions)]
            calls += [("Target.disposeBrowserContext", {"browserContextId": c}) for c in sorted(self.contexts)]
            if len(self.retired) > 4096:
                self.retired.clear()
            self.retired.update(self.sessions)
            self.sessions.clear()
            self.contexts.clear()
            if not calls:
                return clean
            if not self.open:
                return False
            sent = [(method, await self.call(method, params)) for method, params in calls]
            _, late = await asyncio.wait([future for _, future in sent], timeout=HOLD_RESET_TIMEOUT)
            for future in late:
                future.cancel()
            for method, future in sent:
                if future in late or future.cancelled():
                    clean = False
                elif "error" in future.result() and method not in IDEMPOTENT_CLEANUP:
                    LOGGER.warning("Chrome refused %s: %s", method, future.result()["error"])
                    clean = False
            LOGGER.info("Reset the held connection: %s calls, %s unanswered", len(calls), len(late))
            calls = []   # next round only if a session attached meanwhile
        return clean

    async def call(self, method, params):
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        await self.upstream.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        return future

    async def from_client(self, downstream, raw):
        try:
            message = json.loads(raw)
            if not (isinstance(message, dict) and isinstance(message.get("id"), int)
                    and isinstance(message.get("method"), str)):
                raise ValueError
        except (ValueError, TypeError):
            await downstream.send(json.dumps({"error": {
                "code": -32700, "message": "Message must be a JSON object with an integer id and a method"}}))
            return
        client_id, method = message["id"], message["method"]
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "Browser.close":
            # Closing the user's own Chrome because one agent finished would be
            # a disaster on a shared connection. End this client only.
            await downstream.send(json.dumps({"id": client_id, "result": {}}))
            await downstream.close(1000, "Browser.close ends this client; Chrome stays open")
            return
        browser_level = "sessionId" not in message
        if browser_level:
            reset = _reset_for(method, params)
            if reset is not None:
                self.resets[reset[0] + json.dumps(reset[1], sort_keys=True)] = reset
        self.next_id += 1
        self.pending[self.next_id] = (self.generation, client_id, method, params if browser_level else None)
        message["id"] = self.next_id
        await self.upstream.send(json.dumps(message))

    def track_result(self, generation, method, params, result):
        """Remember what a browser-level call created, whoever it was for."""
        if not isinstance(result, dict):
            return
        if isinstance(result.get("sessionId"), str):
            self.sessions[result["sessionId"]] = generation
        # Chrome disposes a context on disconnect only when it was created with
        # disposeOnDetach: true (measured on 154). Do the same and no more, so
        # a held connection keeps exactly what a direct one would keep.
        if (method == "Target.createBrowserContext" and params.get("disposeOnDetach") is True
                and isinstance(result.get("browserContextId"), str)):
            self.contexts.add(result["browserContextId"])
        if method == "Target.disposeBrowserContext":
            self.contexts.discard(params.get("browserContextId"))

    def stale(self, session_id):
        """A session a previous client attached: its traffic is nobody's now."""
        if session_id in self.retired:
            return True
        owner_generation = self.sessions.get(session_id)
        return owner_generation is not None and owner_generation != self.generation

    async def from_chrome(self, raw):
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(message, dict):
            return
        if "id" in message:
            entry = self.pending.pop(message["id"], None)
            if isinstance(entry, asyncio.Future):
                if not entry.done():
                    entry.set_result(message)
                return
            if entry is None:
                return
            generation, client_id, method, params = entry
            if params is not None and "result" in message:
                self.track_result(generation, method, params, message["result"])
            if generation != self.generation or self.owner is None:
                return
            message["id"] = client_id
            await self.owner.send(json.dumps(message))
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "sessionId" not in message:
            method = message.get("method")
            if method == "Target.attachedToTarget" and isinstance(params.get("sessionId"), str):
                self.sessions[params["sessionId"]] = self.generation
            elif method == "Target.detachedFromTarget":
                session = params.get("sessionId")
                was_stale = self.stale(session)
                self.sessions.pop(session, None)
                self.retired.discard(session)
                if was_stale:
                    return
        elif self.stale(message["sessionId"]):
            return
        if self.owner is not None:
            await self.owner.send(raw)

    async def read_chrome(self, upstream):
        try:
            async for raw in upstream:
                try:
                    await self.from_chrome(raw)
                except ConnectionClosed:
                    pass          # the client went; release() will tidy up
        except ConnectionClosed:
            pass
        finally:
            if self.upstream is upstream:
                self.upstream = self.endpoint = None
                self.relay.status(held=False)
                LOGGER.info("Chrome closed the held connection")
            for entry in self.pending.values():
                if isinstance(entry, asyncio.Future) and not entry.done():
                    entry.cancel()
            self.pending.clear()
            self.sessions.clear()
            self.retired.clear()
            self.contexts.clear()
            self.resets.clear()
            owner = self.owner
            if owner is not None:
                await owner.close(_close_code(upstream.close_code))

    async def close_upstream(self):
        upstream, reader = self.upstream, self.reader
        self.upstream = self.endpoint = self.reader = None
        if upstream is not None:
            await upstream.close()
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def close(self):
        owner = self.owner
        if owner is not None:
            await owner.close(1001, "Relay stopping")
        await self.close_upstream()


class LaunchPrimer:
    """Open the held connection when Chrome starts, while you are already in it.

    Chrome activates its window for every consent prompt. Just after you launch
    it, Chrome is in front anyway, so a prompt then takes nothing from you, and
    with the connection already held no agent triggers one later. The catch is
    typing: the sheet takes the keyboard, and a Space presses Cancel. So the
    held connection is opened at launch only:

      - for a Chrome endpoint that appeared while the relay was running, so a
        Chrome already open when the relay starts is never prompted out of turn;
      - while that same Chrome is the frontmost app and has a focused window;
      - after PRIME_IDLE_S without keyboard or mouse input;
      - within PRIME_WINDOW_S of the launch, and at most once per launch, so a
        prompt you cancel is never repeated.

    Otherwise the first client opens it, as without priming. The probes are
    passed in so the decision can be tested without a Mac: endpoint() returns
    the current Endpoint or None, owner_pid(endpoint) the pid listening on it,
    front_pid() the active app's pid if its window is on top (both async), and
    idle_seconds() the time since the last input.
    """

    def __init__(self, held, endpoint, owner_pid, front_pid, idle_seconds, clock=time.monotonic):
        self.held = held
        self.endpoint = endpoint
        self.owner_pid = owner_pid
        self.front_pid = front_pid
        self.idle_seconds = idle_seconds
        self.clock = clock
        self.known = endpoint()      # whatever Chrome is running now is not a launch
        self.pending_until = None

    async def tick(self):
        """One look. Returns what it decided, for the log and the tests."""
        current = self.endpoint()
        now = self.clock()
        if current is not None and current != self.known:
            self.known = current
            self.pending_until = now + PRIME_WINDOW_S
            LOGGER.info("Chrome started; opening the held connection when Chrome is in front and idle")
        if self.pending_until is None:
            return "idle"
        if current is None:
            self.pending_until = None
            return "chrome-gone"
        if now > self.pending_until:
            self.pending_until = None
            LOGGER.info("Chrome was not in front and idle within %ss of starting; "
                        "the first client will open the connection", int(PRIME_WINDOW_S))
            return "gave-up"
        if self.held.open and self.held.endpoint == current:
            self.pending_until = None
            return "already-open"
        if self.held.busy():
            return "busy"
        if self.idle_seconds() < PRIME_IDLE_S:
            return "input"
        owner = await self.owner_pid(current)
        if owner is None or await self.front_pid() != owner:
            return "not-in-front"
        self.pending_until = None          # one attempt per launch, whatever happens
        try:
            opened = await self.held.prime()
        except Exception as exc:
            LOGGER.warning("Could not open the held connection at launch: %s", exc)
            return "failed"
        return "primed" if opened else "already-open"


class NoFocus:
    """For transport regression tests, without macOS permissions or UI."""
    @asynccontextmanager
    async def request(self, endpoint):
        yield

    async def close(self):
        pass
