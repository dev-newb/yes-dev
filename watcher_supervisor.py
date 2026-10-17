"""Keep blocking UI Automation calls out of the Windows dialog discovery loop."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


DIALOG_PATTERN = r"^(allow remote debugging\?|是否允许远程调试\?|要允许远程调试吗？)$"


class Windows:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE

    def dialogs(self, browsers):
        found = []

        @self.callback_type
        def visit(hwnd, _):
            if not self.user.IsWindowVisible(hwnd):
                return True
            name = ctypes.create_unicode_buffer(256)
            self.user.GetClassNameW(hwnd, name, len(name))
            if name.value != "Chrome_WidgetWin_1":
                return True
            title = ctypes.create_unicode_buffer(512)
            self.user.GetWindowTextW(hwnd, title, len(title))
            if not re.fullmatch(DIALOG_PATTERN, title.value, re.IGNORECASE):
                return True
            pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            handle = self.kernel.OpenProcess(0x1000, False, pid.value)
            if handle:
                try:
                    image = ctypes.create_unicode_buffer(32768)
                    size = wintypes.DWORD(len(image))
                    if self.kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                        if Path(image.value).stem.lower() in browsers:
                            found.append((int(hwnd), pid.value))
                finally:
                    self.kernel.CloseHandle(handle)
            return True

        if not self.user.EnumWindows(visit, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return found


class WorkerJob:
    """Closing the supervisor also terminates its workers, even during COM calls."""
    def __init__(self, kernel):
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.kernel = kernel
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def add(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            error = ctypes.get_last_error()
            process.kill()
            process.wait(timeout=5)
            raise ctypes.WinError(error)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


@dataclass
class Worker:
    process: object
    progress: Path
    started: float
    last_progress: float
    stamp: int | None = None
    stopping: bool = False


class Supervisor:
    def __init__(self, directory, launch, log, max_workers=4, stall_seconds=5, startup_seconds=10):
        self.directory = Path(directory)
        self.launch = launch
        self.log = log
        self.max_workers = max_workers
        self.stall_seconds = stall_seconds
        self.startup_seconds = startup_seconds
        self.workers = {}
        self.retry_at = {}
        self.legacy_first = set()
        self.sequence = 0

    def tick(self, dialogs, now):
        dialogs = set(dialogs)
        for key, worker in list(self.workers.items()):
            code = worker.process.poll()
            if code is not None:
                worker.progress.unlink(missing_ok=True)
                del self.workers[key]
                self.retry_at[key] = now + 0.25
                if code:
                    self.log(f"worker pid={worker.process.pid} exited code={code}; retrying hwnd={key[0]}")
                continue
            try:
                stamp = worker.progress.stat().st_mtime_ns
            except FileNotFoundError:
                stamp = None
            if stamp is not None and stamp != worker.stamp:
                worker.stamp = stamp
                worker.last_progress = now
            limit = self.startup_seconds if worker.stamp is None else self.stall_seconds
            elapsed = now - (worker.started if worker.stamp is None else worker.last_progress)
            if not worker.stopping and elapsed >= limit:
                self.log(f"worker stalled for {elapsed:.1f}s; stopping pid={worker.process.pid} hwnd={key[0]}")
                worker.process.kill()
                worker.stopping = True
                self.legacy_first.add(key)
        # A sorted scan plus a short retry delay lets waiting dialogs take a free
        # slot before a repeatedly failing dialog is tried again.
        for key in sorted(dialogs):
            if len(self.workers) >= self.max_workers:
                break
            if key in self.workers or now < self.retry_at.get(key, 0):
                continue
            self.sequence += 1
            progress = self.directory / f"progress-{self.sequence}"
            process = self.launch(key, progress, key in self.legacy_first)
            self.workers[key] = Worker(process, progress, now, now)
        for key in list(self.retry_at):
            if key not in dialogs and key not in self.workers:
                del self.retry_at[key]
                self.legacy_first.discard(key)

    def close(self):
        for worker in self.workers.values():
            if worker.process.poll() is None:
                worker.process.kill()
        for worker in self.workers.values():
            worker.process.wait(timeout=5)
        self.workers.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-IntervalMs", type=int, default=250)
    parser.add_argument("-LogPath", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "YesDev/yes-dev.log")
    parser.add_argument("-BrowserProcess", default="chrome")
    parser.add_argument("-ParentPid", type=int, default=0)
    parser.add_argument("-Observe", action="store_true")
    args = parser.parse_args()
    if args.IntervalMs <= 0:
        parser.error("-IntervalMs must be positive")
    windows = Windows()
    lock = windows.kernel.CreateMutexW(None, True, "Global\\YesDevEngine")
    if not lock:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        windows.kernel.CloseHandle(lock)
        return 0
    parent = None
    job = None
    supervisor = None
    browsers = {name.strip().lower() for name in args.BrowserProcess.split(",") if name.strip()}

    def log(message):
        # Keep supervisor records separate from persisted ACTION events and log rotation.
        path = args.LogPath.with_name("supervisor.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_048_576:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")

    try:
        if args.ParentPid:
            parent = windows.kernel.OpenProcess(0x100000, False, args.ParentPid)
            if not parent:
                return 0
        job = WorkerJob(windows.kernel)
        powershell = str(Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        watcher = Path(__file__).with_name("watcher.ps1")

        def launch(key, progress, legacy_first):
            command = [powershell, "-NoProfile", "-Mta", "-ExecutionPolicy", "Bypass", "-File", str(watcher),
                       "-IntervalMs", str(args.IntervalMs), "-LogPath", str(args.LogPath),
                       "-BrowserProcess", ",".join(sorted(browsers)), "-ParentPid", str(os.getpid()),
                       "-WorkerHwnd", str(key[0]), "-WorkerOwnerPid", str(key[1]), "-ProgressPath", str(progress)]
            if args.Observe:
                command.append("-Observe")
            if legacy_first:
                command.append("-LegacyFirst")
            process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            job.add(process)
            log(f"worker started pid={process.pid} hwnd={key[0]} owner={key[1]} legacy_first={legacy_first}")
            return process

        with tempfile.TemporaryDirectory(prefix="yesdev-workers-") as folder:
            supervisor = Supervisor(folder, launch, log)
            log(f"supervisor started pid={os.getpid()} interval={args.IntervalMs}ms")
            try:
                while not parent or windows.kernel.WaitForSingleObject(parent, 0) == 258:
                    supervisor.tick(windows.dialogs(browsers), time.monotonic())
                    time.sleep(args.IntervalMs / 1000)
            finally:
                supervisor.close()
    except Exception as error:
        log(f"supervisor failed: {error!r}")
        raise
    finally:
        if job:
            job.close()
        if parent:
            windows.kernel.CloseHandle(parent)
        windows.kernel.CloseHandle(lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
