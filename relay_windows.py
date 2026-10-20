"""Demand-open Windows CDP relay and its owned child process manager.

Importing this module needs only Python's standard library. The tray can run
without websockets; only an enabled helper imports the relay transport.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

LOGGER = logging.getLogger("yesdev.relay")


def write_json(path, value):
    """Replace one complete JSON document; never publish a partial document."""
    path = Path(path)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    try:
        for attempt in range(5):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.01)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        if Path(path).stat().st_size > 64 * 1024:
            return {}
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class Status:
    def __init__(self, path, run_id):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.value = dict(run_id=run_id, pid=os.getpid(), state="starting")

    def __call__(self, **values):
        self.value.update(values, updated=time.time())
        try:
            write_json(self.path, self.value)
        except OSError:
            LOGGER.exception("Cannot write relay status")


class NoFocus:
    """Leave focus to Chrome and the approval engine; never restore focus."""
    @asynccontextmanager
    async def request(self, endpoint):
        yield

    async def close(self):
        pass


class ParentProcess:
    """Keep the original process handle open so PID reuse cannot change owner."""
    def __init__(self, pid, creation, native=None):
        if native is None:
            from windows_runtime import Native
            native = Native()
        self.native = native
        self.handle = native.kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not self.handle:
            raise RuntimeError("Cannot open the relay parent process")
        try:
            if native.creation(self.handle) != creation or not self.alive():
                raise RuntimeError("Relay parent exited or its process identity changed")
        except BaseException:
            self.close()
            raise

    def alive(self):
        return self.native.kernel.WaitForSingleObject(self.handle, 0) == 258

    def close(self):
        if self.handle:
            self.native.kernel.CloseHandle(self.handle)
            self.handle = None


def parent_creation():
    from windows_runtime import Native
    native = Native()
    return native.creation(native.kernel.GetCurrentProcess())


def stop_reason(path, run_id):
    value = read_json(path)
    if value.get("run_id") == run_id:
        return str(value.get("reason") or "owned stop request")[:300]
    return None


async def run(args):
    status = Status(args.status_path, args.run_id)
    status(profile=str(args.profile), port=args.port, hold=True)
    parent = relay = None
    reason = "host stopped"
    failure = None
    try:
        if not 1024 <= args.port <= 65535:
            raise ValueError("Relay port must be from 1024 to 65535")
        if not args.profile.is_absolute():
            raise ValueError("Chrome USER DATA directory must be an absolute path")
        parent = ParentProcess(args.parent_pid, args.parent_creation)
        # Log and status already work if the optional transport is absent.
        from cdp_relay import Relay
        relay = Relay(args.profile, NoFocus(), port=args.port, status=status, hold=True)
        await relay.start()
        LOGGER.info("Listening at %s; run=%s; parent=%s", relay.address, args.run_id, args.parent_pid)
        while True:
            if not parent.alive():
                reason = "parent exited"
                break
            requested = stop_reason(args.stop_path, args.run_id)
            if requested:
                reason = requested
                break
            await asyncio.sleep(.2)
    except asyncio.CancelledError:
        reason = "host cancelled"
        raise
    except Exception as exc:
        failure = str(exc)
        reason = "startup or transport failure"
        LOGGER.exception("Relay failed: %s", exc)
    finally:
        if relay is not None:
            try:
                await asyncio.wait_for(relay.close(), 4)
            except Exception as exc:
                failure = failure or str(exc)
                LOGGER.exception("Relay close failed")
        if parent is not None:
            parent.close()
        status(state="error" if failure else "stopped", last_error=failure or "",
               exit_reason=reason, connections=0, held=False, held_state="stopped")
        LOGGER.info("Relay exit: %s", reason)
    return 1 if failure else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True, help="Chrome USER DATA directory")
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--status-path", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-creation", type=int, required=True)
    parser.add_argument("--stop-path", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--hold", action="store_true", help="one connection is always held on Windows")
    args = parser.parse_args(argv)
    args.log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(args.log_path, maxBytes=1_048_576, backupCount=2, encoding="utf-8")
    handlers = [handler]
    if sys.stderr is not None:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.ERROR)
        handlers.append(stderr_handler)
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    LOGGER.info("Relay startup: run=%s profile=%s port=%s", args.run_id, args.profile, args.port)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


class RelayManager:
    """Manage only our Popen, with bounded background start/stop and retry.

    Public calls are thread safe. sync() never waits for child exit or startup.
    stop() requests a graceful stop and returns at once. The worker escalates
    only through the stored Popen; it never searches for or kills port owners.
    """
    RETRY_MIN = 2.0
    RETRY_MAX = 60.0
    START_TIMEOUT = 15.0

    def __init__(self, root: Path, data_dir: Path, log=None):
        self.root = Path(root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.log = log or (lambda message: None)
        self._lock = threading.RLock()
        self._desired = None
        self._config = None
        self._process = None
        self._worker = None
        self._run_id = None
        self._runtime = None
        self._error = ""
        self._retry_at = 0.0
        self._delay = self.RETRY_MIN
        self._started = 0.0
        self._stop_reason = "disabled"

    def _event(self, message):
        try:
            self.log(message)
        except Exception:
            pass

    def _status(self):
        if self._runtime is None or self._process is None:
            return {}
        state = read_json(self._runtime / "status.json")
        if state.get("run_id") != self._run_id or state.get("pid") != self._process.pid:
            return {}
        return state

    def sync(self, enabled: bool, profile: str, port: int):
        with self._lock:
            if enabled:
                try:
                    if isinstance(port, bool) or int(port) != port or not 1024 <= port <= 65535:
                        raise ValueError("Relay port must be from 1024 to 65535")
                    if not profile or not Path(profile).is_absolute():
                        raise ValueError("Set an absolute Chrome USER DATA directory")
                    desired = (str(Path(profile)), int(port))
                except (TypeError, ValueError) as exc:
                    self._desired = None
                    self._error = str(exc)
                    self._stop_reason = "invalid relay settings"
                    self._wake()
                    return
            else:
                desired = None
            if desired != self._desired:
                self._retry_at = 0.0
                self._delay = self.RETRY_MIN
                self._error = ""
            self._desired = desired
            self._stop_reason = "settings changed" if desired else "disabled"
            self._wake()

    def stop(self, reason="disabled"):
        with self._lock:
            self._desired = None
            self._error = ""
            self._stop_reason = str(reason)
            self._wake()

    def _wake(self):
        if self._worker is not None and self._worker.is_alive():
            return
        if self._desired is None and self._process is None:
            return
        if self._process is None and time.monotonic() < self._retry_at:
            return
        if (self._process is not None and self._config == self._desired
                and self._process.poll() is None):
            state = self._status()
            if (state.get("state") in ("listening", "error")
                    or time.monotonic() - self._started < self.START_TIMEOUT):
                return
        self._worker = threading.Thread(target=self._reconcile, name="YesDev relay lifecycle", daemon=True)
        self._worker.start()

    def _failed(self, error):
        self._error = error
        self._retry_at = time.monotonic() + self._delay
        self._delay = min(self.RETRY_MAX, self._delay * 2)
        self._event("Relay error: " + error)

    def _stop_child(self, process, runtime, run_id, reason):
        self._event("Relay stop: " + reason)
        try:
            write_json(runtime / "stop.json", dict(run_id=run_id, reason=reason))
        except OSError as exc:
            self._event("Relay stop file error: " + str(exc))
        for action, timeout in ((None, 5), (process.terminate, 2), (process.kill, 1)):
            try:
                if process.poll() is not None:
                    return
                if action is not None:
                    action()
                process.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                if process.poll() is not None:
                    return
        raise RuntimeError("Owned relay process did not stop")

    def _reconcile(self):
        try:
            while True:
                with self._lock:
                    process = self._process
                    desired = self._desired
                    runtime, run_id = self._runtime, self._run_id
                    reason = self._stop_reason
                    exited = process is not None and process.poll() is not None
                    state = self._status()
                    timed_out = (process is not None and not exited and desired == self._config
                                 and state.get("state") not in ("listening", "error")
                                 and time.monotonic() - self._started > self.START_TIMEOUT)
                    if exited:
                        if desired == self._config:
                            self._failed(state.get("last_error") or f"Relay exited (code {process.returncode})")
                        self._process = self._config = None
                        process = None
                    must_stop = process is not None and (desired != self._config or timed_out)
                    if timed_out:
                        reason = "relay startup timed out"
                        self._failed(reason)
                if must_stop:
                    self._stop_child(process, runtime, run_id, reason)
                    with self._lock:
                        self._process = self._config = None
                    continue
                with self._lock:
                    # Recheck desired after stop work; changes can arrive at any time.
                    if self._process is not None or self._desired is None or time.monotonic() < self._retry_at:
                        return
                    config = self._desired
                self.data_dir.mkdir(parents=True, exist_ok=True)
                run_id = str(uuid.uuid4())
                runtime = self.data_dir / "relay-runs" / run_id
                runtime.mkdir(parents=True)
                command = [sys.executable, str(self.root / "relay_windows.py"),
                           "--profile", config[0], "--port", str(config[1]),
                           "--status-path", str(runtime / "status.json"),
                           "--log-path", str(self.data_dir / "relay.log"),
                           "--parent-pid", str(os.getpid()), "--parent-creation", str(parent_creation()),
                           "--stop-path", str(runtime / "stop.json"), "--run-id", run_id, "--hold"]
                with (runtime / "stderr.log").open("ab") as stderr:
                    child = subprocess.Popen(command, cwd=self.root, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=stderr,
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                with self._lock:
                    self._process, self._config = child, config
                    self._runtime, self._run_id = runtime, run_id
                    self._started = time.monotonic()
                    self._error = ""
                    self._event(f"Relay start: pid={child.pid} run={run_id}")
                    # Check settings once more before leaving the worker.
                    if self._desired == config:
                        return
        except Exception as exc:
            with self._lock:
                self._failed(str(exc))
        finally:
            with self._lock:
                self._worker = None
                # A setting can change after the last check but before this
                # worker exits. Do not lose that stop/restart request.
                if self._process is not None and self._config != self._desired and not self._error:
                    self._wake()

    def status_text(self) -> str:
        with self._lock:
            if self._error:
                return "Relay: error - " + self._error
            if self._desired is None:
                return "Relay: off"
            if self._process is None:
                return "Relay: starting"
            state = self._status()
            if state.get("state") == "error":
                return "Relay: error - " + str(state.get("last_error") or "helper failed")
            if self._process.poll() is not None:
                return "Relay: error - " + str(state.get("last_error") or "helper exited")
            if state.get("last_error"):
                return "Relay: error - " + str(state["last_error"])
            if state.get("connections", 0):
                return "Relay: client active"
            held_state = state.get("held_state", "")
            if held_state in ("held", "idle", "ready") or state.get("held"):
                return "Relay: held"
            if held_state in ("opening", "connecting", "resetting"):
                return "Relay: " + held_state
            if state.get("state") == "listening":
                return "Relay: listening"
            return "Relay: starting"


if __name__ == "__main__":
    raise SystemExit(main())
