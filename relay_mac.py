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
import signal
import sys
import tempfile
import time
import uuid

from cdp_relay import Relay
from platform_mac import DATA_DIR

ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("yesdev.relay")


class MacFocus:
    def __init__(self, profile, log_path):
        self.profile = Path(profile)
        self.log_path = Path(log_path)
        self.process = None
        self.runtime = None
        self.key = None
        self.session = None

    async def prepare(self, endpoint):
        lookup = await asyncio.create_subprocess_exec(
            "/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{endpoint.port}", "-sTCP:LISTEN",
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
            raise RuntimeError("Cannot identify the Chrome process; reopen Chrome and try again")
        pid = int(next(iter(pids)))
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
    parent = os.getppid()
    try:
        await relay.start()
        LOGGER.info("Listening at %s", relay.address)
        while not stopping.is_set():
            if args.exit_with_parent and os.getppid() != parent:
                break
            try:
                await asyncio.wait_for(stopping.wait(), 1)
            except asyncio.TimeoutError:
                pass
    except Exception as exc:
        status(state="error", last_error=str(exc))
        LOGGER.exception("Relay stopped")
        return 1
    finally:
        await relay.close()
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--port", type=int, default=9333)
    p.add_argument("--status-path", type=Path, default=DATA_DIR / "relay-status.json")
    p.add_argument("--log-path", type=Path, default=DATA_DIR / "relay.log")
    p.add_argument("--exit-with-parent", action="store_true")
    p.add_argument("--hold", action="store_true",
                   help="keep one approved Chrome connection open and lend it to one client at a time")
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
