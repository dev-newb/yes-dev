"""Alternate normal/early guards against an already-running disposable Chrome.

Requires its PID and profile explicitly. Waits for quiet input and a non-browser
foreground app before each request. Never launches or stops Chrome itself.
JSON, logs and WindowServer evidence are written only to a new output directory.
"""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from AppKit import NSWorkspace
from Foundation import NSDate, NSRunLoop
from Quartz import CGEventSourceSecondsSinceLastEventType, kCGEventSourceStateCombinedSessionState

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--browser-pid", type=int, required=True)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--count", type=int, default=3, help="trials per mode")
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    trials = []
    session_start = datetime.now()
    for i in range(1, a.count + 1):
        for mode in ("normal", "early"):
            name = f"{i}-{mode}"
            samples = []
            processes = []
            files = []
            runtime_parent = tempfile.TemporaryDirectory(prefix="ydef-", dir="/tmp")
            runtime = Path(runtime_parent.name) / "run"
            def start(args, label):
                output = open(a.out / f"{name}-{label}-stdout.txt", "w")
                files.append(output)
                process = subprocess.Popen([sys.executable, *map(str, args)], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
                processes.append(process)
                return process
            def sample():
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.01))
                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                value = {"t": time.time(), "pid": int(app.processIdentifier()), "name": str(app.localizedName()),
                         "bundle": str(app.bundleIdentifier()), "policy": int(app.activationPolicy()),
                         "idle": CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateCombinedSessionState, 0xffffffff)}
                samples.append(value)
                return value
            def pump(seconds):
                until = time.monotonic() + seconds
                while time.monotonic() < until:
                    sample()
            trial = {"name": name, "mode": mode}
            try:
                if mode == "early":
                    guard = start([HERE / "early_focus_guard.py", "--browser-pid", a.browser_pid,
                                   "--profile", a.profile, "--runtime-dir", runtime,
                                   "--log-path", a.out / f"{name}-guard.log"], "guard")
                else:
                    guard = start([ROOT / "focus_guard_mac.py", "--log-path", a.out / f"{name}-guard.log"], "guard")
                engine = start([ROOT / "watcher_mac.py", "--diagnostics", "--log-path", a.out / f"{name}-engine.log"], "engine")
                pump(1)
                print(f"{name}: waiting for a non-browser app and one second without input", flush=True)
                deadline = time.monotonic() + 90
                while True:
                    value = sample()
                    if guard.poll() is not None or engine.poll() is not None:
                        raise RuntimeError("guard or engine exited; see stdout log")
                    if value["policy"] == 0 and value["pid"] != a.browser_pid and not value["bundle"].startswith("com.google.Chrome") and value["idle"] >= 1:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("no quiet foreground interval")
                trial.update(start=time.time(), previous_pid=value["pid"], previous_name=value["name"])
                client_args = [HERE / "early_focus_client.py", "--output", a.out / f"{name}-client.json"]
                client_args += ["--session", runtime / "session.json"] if mode == "early" else ["--plain-profile", a.profile]
                client = start(client_args, "client")
                deadline = time.monotonic() + 20
                while client.poll() is None and time.monotonic() < deadline:
                    sample()
                if client.poll() is None:
                    raise RuntimeError("client did not complete")
                pump(2)
                trial.update(end=time.time(), client_exit=client.returncode, after=samples[-1])
                print(f"{name}: client exit={client.returncode}, final foreground={samples[-1]['name']}", flush=True)
            except Exception as exc:
                trial["error"] = str(exc)
                raise
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                for f in files:
                    f.close()
                trial["runtime_cleaned_by_guard"] = not runtime.exists() if mode == "early" else None
                runtime_parent.cleanup()
                (a.out / f"{name}-observations.json").write_text(json.dumps(samples, indent=2))
                trials.append(trial)
                (a.out / "trials.json").write_text(json.dumps(trials, indent=2))
    end = datetime.now() + timedelta(seconds=1)
    result = subprocess.run(["/usr/bin/log", "show", "--info", "--debug", "--style", "compact",
                             "--start", (session_start-timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
                             "--end", end.strftime("%Y-%m-%d %H:%M:%S"), "--predicate",
                             'process == "WindowServer" AND eventMessage CONTAINS "Deferring events from frontmost process"'],
                            capture_output=True, text=True, timeout=30)
    (a.out / "windowserver.log").write_text(result.stdout)
    (a.out / "windowserver-stderr.txt").write_text(result.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
