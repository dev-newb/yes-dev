"""Verify actual input after ARM cancels a live Chrome focus restore.

Use a blank TextEdit window. When ARMED appears, press Left once there within
four seconds. The test then connects to the disposable Chrome and verifies no
automatic restore, allowing subsequent user-driven app switches. The five-second
test TTL gives the operator time to act;
the production policy and input check are unchanged (production TTL is two).
"""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from AppKit import NSWorkspace
from Foundation import NSDate, NSRunLoop
from Quartz import CGEventSourceSecondsSinceLastEventType, kCGEventSourceStateCombinedSessionState

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from early_focus_client import command


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--browser-pid", type=int, required=True)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--allow-other-foreground", action="store_true",
                   help="Allow another non-Chrome app when test input is directed to a blank TextEdit window")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    processes, outputs, samples = [], [], []
    result = {"passed": False}
    def spawn(script, parameters, name):
        output = open(args.out / f"{name}-stdout.log", "w")
        outputs.append(output)
        proc = subprocess.Popen([sys.executable, str(ROOT / script), *map(str, parameters)],
                                cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
        processes.append(proc)
        return proc
    def sample():
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.01))
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        value = {"t": time.monotonic(), "pid": int(app.processIdentifier()),
                 "name": str(app.localizedName()), "bundle": str(app.bundleIdentifier()),
                 "policy": int(app.activationPolicy()),
                 "idle": CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateCombinedSessionState, 0xffffffff)}
        samples.append(value)
        return value
    with tempfile.TemporaryDirectory(prefix="ydef-input-", dir="/tmp") as temporary:
        runtime = Path(temporary) / "run"
        try:
            spawn("early_focus_guard_mac.py", ["--browser-pid", args.browser_pid,
                  "--profile", args.profile, "--runtime-dir", runtime, "--ttl", "5",
                  "--log-path", args.out / "guard.log"], "guard")
            spawn("watcher_mac.py", ["--diagnostics", "--log-path", args.out / "engine.log"], "watcher")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                value = sample()
                valid_app = (value["bundle"] == "com.apple.TextEdit" or
                             (args.allow_other_foreground and value["policy"] == 0
                              and not value["bundle"].startswith("com.google.Chrome")))
                if valid_app and value["idle"] >= 1 and (runtime / "session.json").exists():
                    break
            else:
                raise RuntimeError("No quiet eligible foreground interval")
            session = json.loads((runtime / "session.json").read_text())
            with ExitStack() as cleanup:
                control = cleanup.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                control.settimeout(2)
                control.connect(session["socket"])
                reader = cleanup.enter_context(control.makefile("rb"))
                request_id = str(uuid.uuid4())
                status = command(control, reader, session["token"], request_id, "ARM")
                assert status == "armed", status
                armed = time.monotonic()
                result["previous"] = value
                (args.out / "armed.json").write_text(json.dumps({"armed_at": armed}) + "\n")
                print("ARMED — press Left once in the blank TextEdit window now", flush=True)
                while time.monotonic() - armed < 4:
                    value = sample()
                    if value["idle"] <= value["t"] - armed:
                        result["input_after_arm_ms"] = round((value["t"] - armed - value["idle"]) * 1000, 2)
                        break
                else:
                    raise RuntimeError("No input was observed inside the test interval; repeat the test")
                # Let the guard's poll observe the input, then request real CDP.
                until = time.monotonic() + .1
                while time.monotonic() < until:
                    sample()
                client = spawn("tests/mac/early_focus_client.py", ["--plain-profile", args.profile,
                               "--output", args.out / "client.json"], "client")
                deadline = time.monotonic() + 20
                while client.poll() is None and time.monotonic() < deadline:
                    sample()
                assert client.poll() == 0, "CDP client did not succeed"
                until = time.monotonic() + 2
                while time.monotonic() < until:
                    sample()
                text = (args.out / "guard.log").read_text()
                assert "request cancelled by input after ARM" in text, text
                assert "EARLY_RESTORE" not in text, text
                chrome_samples = [item for item in samples if item["t"] > armed and item["pid"] == args.browser_pid]
                assert chrome_samples, "Chrome never became foreground for the connection"
                # A later user action may select another app. The guard must
                # respect that too; it must never restore after cancellation.
                if samples[-1]["pid"] != args.browser_pid:
                    last_input = samples[-1]["t"] - samples[-1]["idle"]
                    assert last_input >= chrome_samples[0]["t"], "Focus changed without subsequent input"
                    result["subsequent_user_input"] = True
                result.update(passed=True, cdp_granted=True, early_restores=0,
                              input_cancellation_logged=True, final_foreground=samples[-1]["name"])
        except Exception as exc:
            result["error"] = str(exc)
            raise
        finally:
            for proc in reversed(processes):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            for output in outputs:
                output.close()
            result["runtime_cleaned"] = not runtime.exists()
            (args.out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
            (args.out / "observations.json").write_text(json.dumps(samples, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
