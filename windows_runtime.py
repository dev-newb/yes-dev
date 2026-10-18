"""Independent Windows launch and per-run diagnostics for the tray app."""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime
import faulthandler
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time
import traceback
import uuid


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def write_json(path, value):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    # Windows readers and virus scanners can briefly deny rename/delete sharing.
    for attempt in range(40):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.025)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Native:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        k = self.kernel
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetShellWindow.restype = wintypes.HWND
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    def creation(self, handle):
        values = [wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime

    def alive(self, pid, creation):
        handle = self.kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            return False
        try:
            return self.creation(handle) == creation and self.kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            self.kernel.CloseHandle(handle)

    def context(self):
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                        ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class Limits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        in_job = wintypes.BOOL()
        if not self.kernel.IsProcessInJob(self.kernel.GetCurrentProcess(), None, ctypes.byref(in_job)):
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Limits()
        flags = None
        if in_job.value:
            if self.kernel.QueryInformationJobObject(None, 9, ctypes.byref(limits), ctypes.sizeof(limits), None):
                flags = limits.basic.flags
        desktop_pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(self.user.GetShellWindow(), ctypes.byref(desktop_pid))
        parent = self.kernel.OpenProcess(0x1000, False, os.getppid())
        parent_image = None
        if parent:
            try:
                image = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(image))
                if self.kernel.QueryFullProcessImageNameW(parent, 0, image, ctypes.byref(size)):
                    parent_image = image.value
            finally:
                self.kernel.CloseHandle(parent)
        return {"pid": os.getpid(), "parent_pid": os.getppid(), "parent_image": parent_image,
                "desktop_pid": desktop_pid.value, "in_job": bool(in_job.value),
                "current_job_limit_flags": flags,
                "creation_time": self.creation(self.kernel.GetCurrentProcess())}


def independent(context):
    # A generic ShellExecute or one BREAKAWAY flag can leave an outer Codex job.
    # Use the desktop Explorer broker and verify its identity in the child.
    return (context["desktop_pid"] != 0 and context["parent_pid"] == context["desktop_pid"]
            and Path(context.get("parent_image") or "").name.lower() == "explorer.exe"
            and (not context["in_job"] or (context["current_job_limit_flags"] is not None
                                          and not context["current_job_limit_flags"] & 0x2000)))


def broker(entry, session_id):
    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"

    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.exists():
        python = Path(sys.executable)
    arguments = subprocess.list2cmdline([str(entry), "--yesdev-brokered", session_id])
    script = (
        "$ErrorActionPreference='Stop';"
        "$windows=(New-Object -ComObject Shell.Application).Windows();$hwnd=0;"
        "$desktop=$windows.FindWindowSW([ref]0,[ref]0,8,[ref]$hwnd,1);"
        "if(-not $desktop){throw 'Desktop Explorer is unavailable'};"
        "$desktop.Document.Application.ShellExecute("
        + ",".join(map(literal, (python, arguments, entry.parent, "open"))) + ",0)"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    powershell = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", encoded],
                            creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f"Explorer launch failed: {result.stderr.decode(errors='replace')}")


class Session:
    def __init__(self, root, session_id, entry, context, native):
        self.root = root
        self.directory = root / "sessions" / session_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / "state.json"
        self.native = native
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.reason = None
        self.state = {"session_id": session_id, **context, "entry": str(entry),
                      "executable": sys.executable, "python": sys.version, "cwd": os.getcwd(), "status": "starting",
                      "started_at": timestamp(), "last_heartbeat": timestamp()}
        self.stderr_file = (self.directory / "stderr.log").open("a", encoding="utf-8", buffering=1)
        self.fault_file = (self.directory / "native-fault.log").open("a", encoding="utf-8", buffering=1)
        self.old_stderr = sys.stderr
        self.old_thread_hook = threading.excepthook
        self.old_sys_hook = sys.excepthook
        sys.stderr = self.stderr_file
        faulthandler.enable(file=self.fault_file, all_threads=True)
        threading.excepthook = self.thread_exception
        sys.excepthook = self.exception_hook
        self.event("startup", **self.state)
        self.save()
        self.reconcile()
        self.thread = threading.Thread(target=self.heartbeat, name="YesDev lifecycle heartbeat", daemon=True)
        self.thread.start()

    def start_exit_watcher(self):
        context = self.state
        command = [sys.executable, str(Path(__file__).resolve()), "--watch-session", str(self.directory),
                   "--pid", str(context["pid"]), "--creation", str(context["creation_time"])]
        watcher = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.stderr_file)
        self.state["exit_watcher_pid"] = watcher.pid
        self.state["exit_watcher_creation"] = self.native.creation(watcher._handle)
        self.save()
        deadline = time.monotonic() + 5
        while not (self.directory / "watcher-ready.json").exists():
            if watcher.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Exit watcher did not open the tray process; see stderr.log")
            time.sleep(0.025)

    def save(self):
        write_json(self.state_path, self.state)

    def event(self, event, **fields):
        with self.lock:
            path = self.directory / "events.jsonl"
            if path.exists() and path.stat().st_size > 1_048_576:
                path.replace(path.with_suffix(".jsonl.1"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": timestamp(), "event": event, **fields}, ensure_ascii=False) + "\n")
                stream.flush()

    def reconcile(self):
        for path in (self.root / "sessions").glob("*/state.json"):
            if path == self.state_path:
                continue
            old = read_json(path)
            if old.get("status") not in ("starting", "ready"):
                continue
            if self.native.alive(old.get("pid", 0), old.get("creation_time", 0)):
                continue
            if self.native.alive(old.get("exit_watcher_pid", 0), old.get("exit_watcher_creation", 0)):
                continue
            old.update(status="exit_unrecorded", exit_reason="unknown; no cleanup or exit-watcher record",
                       disappearance_observed_at=timestamp())
            write_json(path, old)
            self.event("previous_exit_unrecorded", previous_session=old.get("session_id"),
                       last_heartbeat=old.get("last_heartbeat"))

    def heartbeat(self):
        while not self.stop.wait(5):
            try:
                with self.lock:
                    self.state["last_heartbeat"] = timestamp()
                    self.save()
            except OSError:
                traceback.print_exc(file=self.stderr_file)

    def ready(self):
        with self.lock:
            if self.stop.is_set():
                return
            self.state["status"] = "ready"
            self.state["ready_at"] = timestamp()
            self.event("ready")
            self.save()
            write_json(self.root / "active.json", {"session_id": self.state["session_id"],
                       "pid": os.getpid(), "directory": str(self.directory), "started_at": self.state["started_at"]})

    def request_exit(self, reason):
        if self.reason is None:
            self.reason = reason
            self.event("exit_requested", reason=reason)

    def exception_hook(self, kind, error, tb):
        self.event("unhandled_exception", exception_type=kind.__name__, message=str(error),
                   traceback="".join(traceback.format_exception(kind, error, tb)))

    def thread_exception(self, args):
        self.event("thread_exception", thread=args.thread.name, exception_type=args.exc_type.__name__,
                   traceback="".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    def finish(self, code, reason=None):
        self.stop.set()
        if hasattr(self, "thread"):
            self.thread.join(timeout=1)
        with self.lock:
            self.state.update(status="stopped", exit_code=code, exit_reason=reason or self.reason or "main_returned",
                              exit_recorded_at=timestamp(), last_heartbeat=timestamp())
            self.event("exit", code=code, reason=self.state["exit_reason"])
            self.save()
        sys.stderr = self.old_stderr
        sys.excepthook = self.old_sys_hook
        threading.excepthook = self.old_thread_hook
        faulthandler.disable()
        self.fault_file.close()
        self.stderr_file.close()


def watch_session(directory, pid, creation):
    native = Native()
    handle = native.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if native.creation(handle) != creation:
            raise RuntimeError("Tray PID was reused before the exit watcher opened it")
        write_json(directory / "watcher-ready.json", {"pid": os.getpid(), "time": timestamp()})
        while True:
            result = native.kernel.WaitForSingleObject(handle, 1000)
            if result == 0:
                break
            if result != 258:
                raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not native.kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        state = read_json(directory / "state.json")
        clean = state.get("status") == "stopped"
        event = {"time": timestamp(), "event": "process_exit", "actual_exit_code": code.value,
                 "actual_exit_hex": f"0x{code.value:08X}", "cleanup_record_present": clean}
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        state.update(actual_exit_at=event["time"], actual_exit_code=code.value, actual_exit_hex=event["actual_exit_hex"])
        if not clean:
            state.update(status="exit_without_cleanup", exit_reason="unknown termination; no cleanup record")
        write_json(directory / "state.json", state)
    finally:
        native.kernel.CloseHandle(handle)


def run(entry, data_dir=None):
    entry = Path(entry).resolve()
    root = Path(data_dir) if data_dir else Path(os.environ["LOCALAPPDATA"]) / "YesDev" / "lifecycle"
    root.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    brokered = len(args) == 2 and args[0] == "--yesdev-brokered"
    session_id = str(uuid.UUID(args[1])) if brokered else str(uuid.uuid4())
    native = Native()
    context = native.context()
    if not independent(context):
        request = {"time": timestamp(), "event": "independent_launch_requested", **context, "entry": str(entry)}
        launches = root / "launches"
        launches.mkdir(exist_ok=True)
        request_path = launches / (session_id + ".json")
        write_json(request_path, request)
        try:
            if brokered:
                raise RuntimeError("Explorer child failed parent/job verification; refusing an attached tray")
            broker(entry, session_id)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                child_failure = read_json(request_path)
                if child_failure.get("event") == "independent_launch_failed" and child_failure.get("pid") != os.getpid():
                    request["child_failure"] = child_failure
                    raise RuntimeError("Explorer child rejected its launch context; see child_failure")
                state = read_json(root / "sessions" / session_id / "state.json")
                if state.get("status") in ("ready", "stopped", "exit_without_cleanup"):
                    request.update(event="independent_launch_forwarded", child_pid=state.get("pid"),
                                   child_status=state.get("status"))
                    write_json(request_path, request)
                    return 0 if state.get("status") == "ready" or state.get("exit_code") == 0 else 1
                time.sleep(0.05)
            raise TimeoutError("Independent tray did not report ready; inspect the session diagnostics")
        except Exception:
            request.update(event="independent_launch_failed", traceback=traceback.format_exc())
            write_json(request_path, request)
            return 1
    session = None
    code = 0
    reason = None
    try:
        session = Session(root, session_id, entry, context, native)
        session.start_exit_watcher()
        sys.path.insert(0, str(entry.parent))
        namespace = runpy.run_path(str(entry), run_name="yesdev_application")
        code = namespace["main"](lifecycle=session) or 0
    except KeyboardInterrupt:
        code, reason = 130, "keyboard_interrupt"
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
        reason = "system_exit"
        session.event("system_exit", value=str(error.code))
    except BaseException:
        if session:
            session.exception_hook(*sys.exc_info())
        else:
            failures = root / "launches"
            failures.mkdir(exist_ok=True)
            write_json(failures / (session_id + ".json"), {"time": timestamp(), "event": "startup_failed",
                       **context, "traceback": traceback.format_exc()})
        code, reason = 1, "unhandled_exception"
    finally:
        if session:
            session.finish(code, reason)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch-session", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--creation", type=int, required=True)
    options = parser.parse_args()
    watch_session(options.watch_session, options.pid, options.creation)
