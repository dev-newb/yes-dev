"""Isolated Windows/headless CDP integration checks against a real Chrome.

Example (dependencies live outside the repository)::

    python tests/windows_relay_live.py --output C:/qa/run.json \
      --node C:/path/node.exe --playwright-modules C:/runtime/node_modules \
      --puppeteer-modules C:/qa/node_modules

This creates its own temporary profile and starts its own Chrome process. It
does not use the tray, global mutex, current MCP, existing Chrome ports, or an
existing profile. The no-op focus guard means these checks establish protocol
behavior only. They do not validate native Chrome consent UI or focus restore.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import functools
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cdp_relay
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
import websockets


async def eventually(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(.02)
    raise AssertionError("Condition did not become true before timeout")


class ChildJob:
    """A private, unnamed Windows job; cleanup reaches only this child tree."""

    def __init__(self, process):
        class Basic(ctypes.Structure):
            _fields_ = [("per_process", ctypes.c_longlong), ("per_job", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_mem", ctypes.c_size_t),
                        ("job_mem", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        config = Extended()
        config.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(config), ctypes.sizeof(config)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class PrivateChrome:
    def __init__(self, executable):
        self.executable = executable
        self.temp = tempfile.TemporaryDirectory(prefix="yesdev-windows-relay-live-")
        self.profile = Path(self.temp.name)
        self.process = self.job = None
        self.launches = []

    async def start(self):
        (self.profile / "DevToolsActivePort").unlink(missing_ok=True)
        command = [str(self.executable), f"--user-data-dir={self.profile}", "--headless=new",
                   "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
                   "--disable-background-networking", "about:blank"]
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.job = ChildJob(self.process)
        except BaseException:
            self.process.terminate()
            await asyncio.to_thread(self.process.wait, 10)
            raise
        self.launches.append({"pid": self.process.pid, "command": command})
        await eventually(lambda: self.process.poll() is not None or (self.profile / "DevToolsActivePort").exists(), 20)
        if self.process.poll() is not None:
            raise RuntimeError(f"Private Chrome exited with {self.process.returncode}")
        endpoint = cdp_relay.read_endpoint(self.profile)
        self.launches[-1]["endpoint"] = endpoint.url
        return endpoint

    async def stop(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            try:
                async with connect(cdp_relay.read_endpoint(self.profile).url, proxy=None, open_timeout=3) as ws:
                    await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                await asyncio.to_thread(self.process.wait, 8)
            except (OSError, ValueError, ConnectionClosed, TimeoutError, subprocess.TimeoutExpired):
                pass
        if self.job:
            self.job.close()
            self.job = None
        if self.process.poll() is None:
            self.process.terminate()
            await asyncio.to_thread(self.process.wait, 5)

    async def cleanup(self):
        await self.stop()
        # Chrome may release profile handles just after its root process exits.
        for attempt in range(30):
            try:
                self.temp.cleanup()
                return
            except PermissionError:
                if attempt == 29:
                    raise
                await asyncio.sleep(.1)


class NoFocus:
    def __init__(self):
        self.calls = 0

    @asynccontextmanager
    async def request(self, endpoint):
        self.calls += 1
        yield

    async def close(self):
        pass


class Cdp:
    def __init__(self, url):
        self.url, self.next_id = url, 0

    async def __aenter__(self):
        self.ws = await connect(self.url, proxy=None, open_timeout=10, close_timeout=1,
                                ping_interval=None, max_size=cdp_relay.MAX_MESSAGE)
        return self

    async def call(self, method, params=None, session=None, error=False):
        self.next_id += 1
        message = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        await self.ws.send(json.dumps(message))
        async with asyncio.timeout(10):
            while True:
                reply = json.loads(await self.ws.recv())
                if reply.get("id") == self.next_id:
                    if "error" in reply and not error:
                        raise AssertionError(f"{method}: {reply['error']}")
                    return reply if error else reply["result"]

    async def __aexit__(self, *_):
        await self.ws.close()


async def http_request(port, host=None, origin=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"GET /json/version HTTP/1.1\r\nHost: {host or f'127.0.0.1:{port}'}\r\nConnection: close\r\n"
    if origin is not None:
        request += f"Origin: {origin}\r\n"
    try:
        writer.write((request + "\r\n").encode("ascii"))
        await writer.drain()
        return (await asyncio.wait_for(reader.read(), 5)).decode("utf-8")
    finally:
        writer.close()
        await writer.wait_closed()


class LiveRun:
    def __init__(self, args):
        self.args = args
        self.chrome = PrivateChrome(args.chrome)
        self.focus = NoFocus()
        self.trace = []
        self.relay = None
        self.report = {"started": datetime.now(timezone.utc).isoformat(),
                       "scope": "Real private headless Chrome, in-process held Relay, no-op focus guard",
                       "limitations": ["No native consent, foreground, tray, host CLI or launch-primer validation"],
                       "python": sys.version, "websockets": websockets.__version__,
                       "sources": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                   for name in ("cdp_relay.py", "tests/windows_relay_live.py", "tests/windows_relay_clients.mjs")},
                       "cases": []}

    async def settled(self):
        await eventually(lambda: not self.relay.clients and self.relay.held.owner is None
                         and self.relay.held.reset_done.is_set())

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.relay.port}{cdp_relay.RELAY_PATH}"

    async def work(self, client):
        target = (await client.call("Target.createTarget", {"url": "about:blank"}))["targetId"]
        session = (await client.call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
        result = await client.call("Runtime.evaluate", {"expression": "6*7", "returnByValue": True}, session)
        assert result["result"]["value"] == 42, result
        return target, session

    async def sequential(self, result):
        sockets = []
        for index in range(3):
            async with Cdp(self.url) as client:
                version = await client.call("Browser.getVersion")
                target, session = await self.work(client)
                context = (await client.call("Target.createBrowserContext", {"disposeOnDetach": True}))["browserContextId"]
                await client.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
            await self.settled()
            sockets.append(str(self.relay.held.upstream.id))
            assert self.relay.held.open and not self.relay.held.sessions and not self.relay.held.contexts
            async with Cdp(self.url) as observer:
                stale = await observer.call("Runtime.evaluate", {"expression": "1"}, session, error=True)
                assert "error" in stale, stale
                assert context not in (await observer.call("Target.getBrowserContexts"))["browserContextIds"]
                await observer.call("Target.closeTarget", {"targetId": target})
            await self.settled()
        assert len(set(sockets)) == 1 and self.focus.calls == 1, (sockets, self.focus.calls)
        result.update(browser=version, held_socket_ids=sockets, upstream_connections=self.focus.calls,
                      session_detach_verified=True, dispose_on_detach_contexts_removed=True)
        self.report["browser"] = version

    async def node_clients(self, result):
        before = self.focus.calls
        socket_id = str(self.relay.held.upstream.id)
        results = []
        for kind, modules in (("playwright", self.args.playwright_modules),
                              ("puppeteer", self.args.puppeteer_modules)):
            trace_start = len(self.trace)
            process = await asyncio.create_subprocess_exec(
                str(self.args.node), str(Path(__file__).with_name("windows_relay_clients.mjs")),
                kind, self.relay.address, str(modules), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            assert process.returncode == 0, stderr.decode(errors="replace")
            await self.settled()
            assert self.relay.held.open and str(self.relay.held.upstream.id) == socket_id
            assert not self.relay.held.sessions and not self.relay.held.resets
            sent = [x["message"] for x in self.trace[trace_start:] if x["direction"] == "sent"]
            reset = [x for x in sent if x.get("method") == "Target.setAutoAttach"
                     and "sessionId" not in x and x.get("params", {}).get("autoAttach") is False]
            detached = [x["params"]["sessionId"] for x in sent if x.get("method") == "Target.detachFromTarget"]
            assert reset and detached, sent
            # A new page must run after the library's auto-attach owner leaves.
            # This also detects targets left waiting for a debugger to resume.
            async with Cdp(self.url) as observer:
                target, _ = await self.work(observer)
                await observer.call("Target.closeTarget", {"targetId": target})
            await self.settled()
            value = json.loads(stdout.decode().strip().splitlines()[-1])
            value.update(reset_autoattach_sent=True, detached_sessions=len(detached),
                         next_client_page_evaluated=True)
            results.append(value)
        assert self.focus.calls == before
        result.update(clients=results, same_held_socket=True, state_reset_complete=True)

    async def close_and_concurrent(self, result):
        before = self.focus.calls
        async with Cdp(self.url) as owner:
            await owner.call("Browser.getVersion")
            async with Cdp(self.url) as other:
                await other.call("Browser.getVersion")
                assert self.focus.calls == before + 1
                await other.call("Browser.close")
                await asyncio.wait_for(other.ws.wait_closed(), 5)
                assert other.ws.close_code == 1000
            version = await owner.call("Browser.getVersion")
            await owner.call("Browser.close")
            await asyncio.wait_for(owner.ws.wait_closed(), 5)
            assert owner.ws.close_code == 1000
        await self.settled()
        assert self.chrome.process.poll() is None
        result.update(concurrent_own_connection=True, browser_alive=True, browser=version,
                      close_codes=[owner.ws.close_code, other.ws.close_code])

    async def security(self, result):
        before = self.focus.calls
        good = await http_request(self.relay.port)
        assert good.startswith("HTTP/1.1 200"), good
        discovery = json.loads(good.split("\r\n\r\n", 1)[1])
        assert discovery["webSocketDebuggerUrl"] == self.url
        responses = {}
        for name, kwargs in (("origin", {"origin": "https://example.invalid"}),
                             ("host", {"host": "evil.invalid"})):
            response = await http_request(self.relay.port, **kwargs)
            assert response.startswith("HTTP/1.1 403"), response
            responses[name] = response.splitlines()[0]
        try:
            async with connect(self.url, proxy=None, origin="https://example.invalid"):
                raise AssertionError("Website websocket Origin was accepted")
        except InvalidStatus as exc:
            assert exc.response.status_code == 403, exc
            responses["websocket_origin"] = exc.response.status_code
        assert before == self.focus.calls
        result.update(discovery=discovery, rejected=responses, no_upstream_opened=True)

    async def collision(self, result):
        with socket.socket() as owner:
            owner.bind(("127.0.0.1", 0))
            owner.listen()
            port = owner.getsockname()[1]
            collision = cdp_relay.Relay(self.chrome.profile, NoFocus(), port=port, hold=True)
            try:
                with suppress(OSError):
                    await collision.start()
                assert collision.server is None, "Collision unexpectedly acquired port"
                probe = socket.socket()
                try:
                    probe.settimeout(2)
                    probe.connect(("127.0.0.1", port))
                finally:
                    probe.close()
                assert owner.fileno() >= 0
            finally:
                await collision.close()
            result.update(port=port, original_owner_still_accepts=True)

    async def restart(self, result):
        before = self.focus.calls
        old_endpoint = cdp_relay.read_endpoint(self.chrome.profile)
        async with Cdp(self.url) as client:
            await client.call("Browser.getVersion")
            old_pid = self.chrome.process.pid
            await self.chrome.stop()
            await asyncio.wait_for(client.ws.wait_closed(), 5)
        await self.settled()
        await self.chrome.start()
        new_endpoint = cdp_relay.read_endpoint(self.chrome.profile)
        assert old_endpoint != new_endpoint
        async with Cdp(self.url) as client:
            version = await client.call("Browser.getVersion")
            target, _ = await self.work(client)
            await client.call("Target.closeTarget", {"targetId": target})
        await self.settled()
        async with Cdp(self.url) as client:
            await client.call("Browser.getVersion")
        await self.settled()
        assert self.focus.calls == before + 1
        result.update(old_pid=old_pid, new_pid=self.chrome.process.pid, browser=version,
                      endpoint_changed=True, new_connection_opened=True, second_client_reused=True)

    async def stop_relay(self, result):
        await self.relay.close()
        assert self.chrome.process.poll() is None
        async with Cdp(cdp_relay.read_endpoint(self.chrome.profile).url) as direct:
            result["direct_browser_after_relay_stop"] = await direct.call("Browser.getVersion")
        result["browser_alive"] = True

    async def run(self):
        trace = self.trace

        class TraceConnection(ClientConnection):
            async def send(self, payload, *args, **kwargs):
                trace.append({"socket": str(self.id), "direction": "sent", "message": json.loads(payload)})
                return await super().send(payload, *args, **kwargs)

            async def recv(self, *args, **kwargs):
                payload = await super().recv(*args, **kwargs)
                trace.append({"socket": str(self.id), "direction": "received", "message": json.loads(payload)})
                return payload

        patched_connect = functools.partial(cdp_relay.connect, create_connection=TraceConnection)
        try:
            await self.chrome.start()
            with patch.object(cdp_relay, "connect", patched_connect):
                self.relay = await cdp_relay.Relay(self.chrome.profile, self.focus, port=0, hold=True).start()
                for name, callback in (("raw_sequential_cleanup", self.sequential),
                                       ("playwright_puppeteer_sequential", self.node_clients),
                                       ("browser_close_and_concurrent", self.close_and_concurrent),
                                       ("origin_host_discovery", self.security),
                                       ("port_collision", self.collision),
                                       ("browser_restart_reconnect", self.restart),
                                       ("relay_stop_browser_survives", self.stop_relay)):
                    case = {"name": name, "passed": False}
                    self.report["cases"].append(case)
                    started = time.monotonic()
                    try:
                        await callback(case)
                        case["passed"] = True
                    except BaseException:
                        case["error"] = traceback.format_exc()
                        raise
                    finally:
                        case["seconds"] = round(time.monotonic() - started, 3)
                        print(f"{name}: {'PASS' if case['passed'] else 'FAIL'}", flush=True)
        except BaseException:
            self.report["error"] = traceback.format_exc()
        finally:
            try:
                if self.relay is not None:
                    await self.relay.close()
            except BaseException:
                self.report["relay_cleanup_error"] = traceback.format_exc()
            try:
                await self.chrome.cleanup()
            except BaseException:
                self.report["browser_cleanup_error"] = traceback.format_exc()
            self.report["browser_launches"] = self.chrome.launches
            self.report["cleanup"] = {"owned_browser_stopped": self.chrome.process is None or self.chrome.process.poll() is not None,
                                      "temporary_profile_removed": not self.chrome.profile.exists()}
            self.report["trace"] = self.trace
            self.report["passed"] = not any(key.endswith("error") for key in self.report) and all(
                x["passed"] for x in self.report["cases"]) and all(self.report["cleanup"].values())
            self.args.output.parent.mkdir(parents=True, exist_ok=True)
            self.args.output.write_text(json.dumps(self.report, indent=2) + "\n", encoding="utf-8")
        return 0 if self.report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chrome", type=Path, default=Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe")
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--playwright-modules", type=Path, required=True)
    parser.add_argument("--puppeteer-modules", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This isolated child-process harness requires Windows")
    if not args.chrome.is_file() or not args.node.is_file():
        parser.error("Supply existing Chrome and Node executable paths")
    if args.output.resolve().is_relative_to(ROOT):
        parser.error("Keep the runtime report outside the repository")
    return asyncio.run(LiveRun(args).run())


if __name__ == "__main__":
    raise SystemExit(main())
