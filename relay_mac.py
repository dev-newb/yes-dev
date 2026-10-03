"""Supervised macOS entry point for the optional CDP relay."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import time
import uuid

from cdp_relay import LaunchPrimer, Relay, read_endpoint
from platform_mac import DATA_DIR

ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("yesdev.relay")


async def listener_pid(port):
    """The one process listening on Chrome's debugging port, or None."""
    lookup = await asyncio.create_subprocess_exec(
        "/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(lookup.communicate(), 2)
    finally:
        if lookup.returncode is None:
            with suppress(ProcessLookupError):
                lookup.kill()
            await lookup.wait()
    pids = set(stdout.decode().split())
    if len(pids) != 1 or not next(iter(pids)).isdigit():
        return None
    return int(next(iter(pids)))


async def _output(*command):
    """A short helper command's stdout, or b"" if it fails or takes over two seconds."""
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 2)
        return stdout
    except asyncio.TimeoutError:
        return b""
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def front_pid_with_window():
    """The active app's pid, if its window is the one on top; else None.

    Two sources must agree. Launch Services names the active app, and the
    window server's topmost visible normal window must belong to it, so a
    windowless active app is never mistaken for the Chrome window behind it.
    Not NSWorkspace, which goes stale in a process with no AppKit event loop,
    and not the system-wide AXFocusedApplication, which answers "cannot
    complete" on this Mac (2026-10-03).
    """
    from Quartz import (CGWindowListCopyWindowInfo, kCGNullWindowID,
                        kCGWindowListExcludeDesktopElements, kCGWindowListOptionOnScreenOnly)
    front = re.search(rb"ASN:[0-9a-fx-]+", await _output("/usr/bin/lsappinfo", "front"))
    if front is None:
        return None
    match = re.search(rb'"pid"=(\d+)', await _output("/usr/bin/lsappinfo", "info", "-only", "pid",
                                                      front.group(0).decode()))
    if match is None:
        return None
    active = int(match.group(1))
    windows = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID) or []
    top = next((w for w in windows
                if w.get("kCGWindowLayer") == 0 and (w.get("kCGWindowAlpha") or 0) > 0), None)
    if top is None or int(top.get("kCGWindowOwnerPID", -1)) != active:
        return None
    return active


def idle_seconds():
    from Quartz import CGEventSourceSecondsSinceLastEventType, kCGEventSourceStateCombinedSessionState
    return CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateCombinedSessionState, 0xFFFFFFFF)


class MacFocus:
    def __init__(self, profile, log_path):
        self.profile = Path(profile)
        self.log_path = Path(log_path)
        self.process = None
        self.runtime = None
        self.key = None
        self.session = None

    async def prepare(self, endpoint):
        pid = await listener_pid(endpoint.port)
        if pid is None:
            raise RuntimeError("Cannot identify the Chrome process; reopen Chrome and try again")
        key = (pid, endpoint)
        if self.key == key and self.process is not None and self.process.returncode is None:
            return
        await self.close()
        # Keep Unix socket paths short even when the user data path is long.
        self.runtime = tempfile.TemporaryDirectory(prefix="yesdev-relay-", dir="/tmp")
        run = Path(self.runtime.name) / "run"
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "early_focus_guard_mac.py"),
            "--browser-pid", str(pid), "--profile", str(self.profile),
            "--runtime-dir", str(run), "--log-path", str(self.log_path), "--exit-with-parent",
            "--parent-pid", str(os.getpid()),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 4
            while not (run / "session.json").exists():
                if self.process.returncode is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Fast focus could not start; check Accessibility permission and Chrome profile")
                await asyncio.sleep(.02)
            self.session = json.loads((run / "session.json").read_text())
            self.key = key
        except BaseException:
            await self.close()
            raise

    @asynccontextmanager
    async def request(self, endpoint):
        await self.prepare(endpoint)
        reader, writer = await asyncio.open_unix_connection(self.session["socket"], limit=4096)
        request_id = str(uuid.uuid4())
        async def command(op):
            writer.write((json.dumps({"op": op, "request_id": request_id,
                                      "token": self.session["token"]}) + "\n").encode())
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
            if reply.get("request_id") != request_id:
                raise RuntimeError("Invalid focus acknowledgement")
            return reply["status"]
        try:
            status = await command("ARM")
            if status not in {"armed", "recent-input", "previous-app-is-browser", "no-previous-app"}:
                raise RuntimeError("Focus guard refused request: " + status)
            # Recent input skips restoration, never blocks legitimate CDP use.
            LOGGER.info("Connection focus: %s", status)
            yield
        finally:
            try:
                await command("DONE")
            except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
                pass
            # EOF cancels the pending arm as well, including cancellation while
            # waiting for Chrome's consent or a disconnected downstream client.
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def close(self):
        process, self.process = self.process, None
        try:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        finally:
            if self.runtime is not None:
                self.runtime.cleanup()
                self.runtime = None
            self.key = self.session = None


class Status:
    def __init__(self, path):
        self.path = Path(path)
        self.value = {"pid": os.getpid()}

    def __call__(self, **values):
        self.value.update(values, updated=time.time())
        temp = self.path.with_suffix(".tmp")
        try:
            with open(temp, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
                json.dump(self.value, f)
            temp.replace(self.path)
        except OSError:
            LOGGER.exception("Cannot write relay status")


async def run(args):
    status = Status(args.status_path)
    focus = MacFocus(args.profile, args.log_path.with_name("relay-focus.log"))
    relay = Relay(args.profile, focus, args.port, status, hold=args.hold)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stopping.set)
    # The supervisor's pid when it passed one; see watcher_mac.Engine.
    parent = args.parent_pid or os.getppid()
    primer = priming = None
    try:
        await relay.start()
        LOGGER.info("Listening at %s", relay.address)
        if args.prime and relay.held is not None:
            def current_endpoint():
                try:
                    return read_endpoint(args.profile)
                except (OSError, ValueError):
                    return None
            primer = LaunchPrimer(relay.held, current_endpoint,
                                  lambda endpoint: listener_pid(endpoint.port),
                                  front_pid_with_window, idle_seconds)
        while not stopping.is_set():
            if args.exit_with_parent and os.getppid() != parent:
                break
            # A tick that opens the connection waits for the prompt to be
            # approved; run it beside the loop so the parent check carries on.
            if primer is not None and (priming is None or priming.done()):
                priming = asyncio.create_task(primer.tick())
            try:
                await asyncio.wait_for(stopping.wait(), 1)
            except asyncio.TimeoutError:
                pass
    except Exception as exc:
        status(state="error", last_error=str(exc))
        LOGGER.exception("Relay stopped")
        return 1
    finally:
        if priming is not None and not priming.done():
            priming.cancel()
            await asyncio.gather(priming, return_exceptions=True)
        await relay.close()
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--port", type=int, default=9333)
    p.add_argument("--status-path", type=Path, default=DATA_DIR / "relay-status.json")
    p.add_argument("--log-path", type=Path, default=DATA_DIR / "relay.log")
    p.add_argument("--exit-with-parent", action="store_true")
    p.add_argument("--parent-pid", type=int, default=0,
                   help="the supervisor's pid, passed by the supervisor itself. Without it the "
                         "parent is read at startup, which is too late if the parent has "
                         "already exited: the helper then records launchd and never stops")
    p.add_argument("--hold", action="store_true",
                   help="keep one approved Chrome connection open and lend it to one client at a time")
    p.add_argument("--prime", action="store_true",
                   help="with --hold, open that connection as soon as a newly started Chrome is "
                        "frontmost and idle, so no client triggers the prompt later")
    a = p.parse_args()
    if not 1024 <= a.port <= 65535:
        p.error("port must be between 1024 and 65535")
    a.profile = a.profile.expanduser().resolve()
    for path in (a.log_path, a.status_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(a.log_path, maxBytes=1_000_000, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    return asyncio.run(run(a))


if __name__ == "__main__":
    raise SystemExit(main())
