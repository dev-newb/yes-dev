"""Real-client relay checks against an explicitly supplied disposable Chrome.

The runner owns its watcher/relay and never reads the user's normal profile.
Chrome restart is optional and restricted to an explicitly supplied disposable
instance. Keep a non-Chrome app in front and input quiet. Requires Playwright
(test dependency, no downloaded browser needed).
"""
import argparse
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from AppKit import NSWorkspace
from Foundation import NSDate, NSRunLoop
from Quartz import CGEventSourceSecondsSinceLastEventType, kCGEventSourceStateCombinedSessionState
from playwright.async_api import async_playwright
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[2]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
sys.path.insert(0, str(ROOT))
from cdp_relay import RELAY_PATH


async def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    samples, cases, processes, files = [], [], [], []
    start = datetime.now()
    finished = asyncio.Event()
    def sample():
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.002))
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        value = {"t": time.time(), "pid": int(app.processIdentifier()), "name": str(app.localizedName()),
                 "bundle": str(app.bundleIdentifier()), "policy": int(app.activationPolicy()),
                 "idle": CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateCombinedSessionState, 0xffffffff)}
        samples.append(value)
        return value
    async def sampling():
        while not finished.is_set():
            sample()
            await asyncio.sleep(.01)
    async def quiet():
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            value = sample()
            if value["policy"] == 0 and not value["bundle"].startswith("com.google.Chrome") and value["idle"] >= 1:
                return value
            await asyncio.sleep(.05)
        raise RuntimeError("No quiet non-Chrome foreground app")
    def spawn(script, params, name):
        output = open(args.out / f"{name}-stdout.log", "w")
        files.append(output)
        process = subprocess.Popen([sys.executable, str(ROOT / script), *map(str, params)],
                                   cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
        processes.append(process)
        return process
    url = f"ws://127.0.0.1:{args.port}{RELAY_PATH}"
    async def command(ws, method="Browser.getVersion"):
        await ws.send(json.dumps({"id": 1, "method": method}))
        while True:
            reply = json.loads(await asyncio.wait_for(ws.recv(), 25))
            if reply.get("id") == 1:
                assert "result" in reply, reply
                return reply
    async def raw():
        async with connect(url, proxy=None) as ws:
            result = await command(ws)
            assert "product" in result["result"]
            return result
    async def parallel():
        async with connect(url, proxy=None) as a, connect(url, proxy=None) as b:
            one, two = await asyncio.gather(command(a), command(b, "Target.getTargets"))
            assert "product" in one["result"] and "targetInfos" in two["result"]
            await a.close()
            survivor = await command(b)
            assert "product" in survivor["result"]
            return {"version": one, "target_count": len(two["result"]["targetInfos"]), "survivor": survivor}
    async def playwright():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}", timeout=30000)
            session = await browser.new_browser_cdp_session()
            version = await session.send("Browser.getVersion")
            assert "product" in version
            page_count = sum(len(context.pages) for context in browser.contexts)
            await session.detach()
            await browser.close()
            return {"version": version, "page_count": page_count}
    async def restart_disposable():
        # This destructive test is opt-in and refuses non-temporary profiles.
        profile = args.profile.resolve()
        if not profile.is_relative_to(Path("/tmp").resolve()) or not profile.name.startswith("yesdev-"):
            raise RuntimeError("Restart test requires a yesdev-* profile under /tmp")
        command_line = subprocess.check_output(["ps", "-p", str(args.restart_disposable_pid), "-o", "command="], text=True).strip()
        if not command_line.startswith(CHROME + " ") or f"--user-data-dir={args.profile} " not in command_line:
            raise RuntimeError("Disposable Chrome PID/profile does not match; refusing restart")
        async with connect(url, proxy=None) as ws:
            await command(ws)
            os.kill(args.restart_disposable_pid, signal.SIGTERM)
            await asyncio.wait_for(ws.wait_closed(), 5)
            code = ws.close_code
        deadline = time.monotonic() + 5
        while subprocess.run(["ps", "-p", str(args.restart_disposable_pid), "-o", "pid="], capture_output=True).stdout.strip():
            if time.monotonic() >= deadline:
                raise RuntimeError("Disposable Chrome did not stop")
            await asyncio.sleep(.05)
        (args.profile / "DevToolsActivePort").unlink(missing_ok=True)
        subprocess.run(["open", "-gna", "Google Chrome", "--args", "--user-data-dir="+str(args.profile),
                        "--no-first-run", "about:blank"], check=True)
        deadline = time.monotonic() + 15
        while not (args.profile / "DevToolsActivePort").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Restarted Chrome did not publish its endpoint")
            await asyncio.sleep(.1)
        print("Browser restarted: waiting for a quiet non-Chrome app for the reconnect check", flush=True)
        previous = await quiet()
        result = await raw()
        return {"old_socket_close_code": code, "reconnected": result,
                "reconnect_previous": previous}
    sampler = asyncio.create_task(sampling())
    try:
        relay = spawn("relay_mac.py", ["--profile", args.profile, "--port", args.port,
                      "--status-path", args.out / "relay-status.json", "--log-path", args.out / "relay.log"], "relay")
        engine = spawn("watcher_mac.py", ["--diagnostics", "--log-path", args.out / "engine.log"], "engine")
        await asyncio.sleep(1)
        sequence = [("raw-first", raw), ("raw-reconnect", raw), ("parallel-and-one-leaves", parallel),
                    ("playwright", playwright), ("playwright-reconnect", playwright)]
        if args.restart_disposable_pid:
            sequence.append(("browser-restart-and-reconnect", restart_disposable))
        if args.only:
            sequence = [(name, callback) for name, callback in sequence if name == args.only]
            if not sequence:
                raise ValueError("Requested case is unavailable; restart requires its disposable PID")
        for name, callback in sequence:
            if relay.poll() is not None or engine.poll() is not None:
                raise RuntimeError("Relay or watcher stopped; inspect logs")
            print(f"{name}: waiting for a quiet non-Chrome app", flush=True)
            previous = await quiet()
            case = {"name": name, "start": time.time(), "previous": previous}
            try:
                case["result"] = await callback()
                case["passed"] = True
                await asyncio.sleep(2)
                case["after"] = sample()
                expected = case["result"].get("reconnect_previous", previous)
                case["expected_previous"] = expected
                assert case["after"]["pid"] == expected["pid"], "Previous app was not restored"
            except Exception as exc:
                case["passed"] = False
                case["error"] = str(exc)
                raise
            finally:
                case["end"] = time.time()
                cases.append(case)
                (args.out / "cases.json").write_text(json.dumps(cases, indent=2))
            print(f"{name}: passed; foreground={case['after']['name']}", flush=True)
    finally:
        finished.set()
        await sampler
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
        for output in files:
            output.close()
        (args.out / "observations.json").write_text(json.dumps(samples, indent=2))
        end = datetime.now() + timedelta(seconds=1)
        logs = await asyncio.create_subprocess_exec(
            "/usr/bin/log", "show", "--info", "--debug", "--style", "compact",
            "--start", (start-timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "--end", end.strftime("%Y-%m-%d %H:%M:%S"), "--predicate",
            'process == "WindowServer" AND eventMessage CONTAINS "Deferring events from frontmost process"',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await logs.communicate()
        (args.out / "windowserver.log").write_bytes(stdout)
        (args.out / "windowserver-stderr.txt").write_bytes(stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--port", type=int, default=19333)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--restart-disposable-pid", type=int, help="opt in to restarting only this /tmp/yesdev-* Chrome")
    p.add_argument("--only", choices=("raw-first", "raw-reconnect", "parallel-and-one-leaves",
                                    "playwright", "playwright-reconnect", "browser-restart-and-reconnect"))
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
