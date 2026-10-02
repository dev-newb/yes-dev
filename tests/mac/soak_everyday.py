"""Bounded mixed-client soak with the real tray and a disposable Chrome.

Results and isolated app data must be outside Git. The host runs the real
rumps event loop, settings application, and supervision tick. The driver reads
only app identities/input ages, process resources and CDP version/target data;
it never records document text, typed keys, screenshots or network payloads.
"""
import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import suppress
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def host(args):
    os.environ["YESDEV_DATA_DIR"] = str(args.out / "app-data")
    import platform_mac
    import rumps
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    from yes_dev_mac import YesDev
    assert platform_mac.is_trusted(), "Existing runtime needs Accessibility"
    assert platform_mac.acquire_single_instance("tray")
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app = YesDev()
    seen = None

    def supervise(_):
        nonlocal seen
        command = read_json(args.out / "command.json")
        if command.get("id") and command["id"] != seen:
            seen = command["id"]
            try:
                app.apply_settings({**app.cfg, **command["values"]}, platform_mac.autostart_enabled())
                write_json(args.out / "command-result.json", {"id": seen, "ok": True})
            except Exception as exc:
                write_json(args.out / "command-result.json", {"id": seen, "ok": False, "error": str(exc)})
        app._tick()
        def pid(proc):
            return proc.pid if proc is not None and proc.poll() is None else None
        write_json(args.out / "host-status.json", {
            "t": time.time(), "pid": os.getpid(), "engine": pid(app.proc),
            "relay": pid(app.relay), "normal_guard": pid(app.guard),
            "approvals": app.approvals, "paused": app.paused_reason,
            "enabled": app.cfg["enabled"], "observe": app.cfg["observe_only"],
            "relay_status": app.relay_status_text(),
        })

    def stop(*_):
        app.shutdown()
        rumps.quit_application()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    rumps.Timer(supervise, 1).start()
    try:
        app.run()
    finally:
        app.shutdown()


def processes(host_pid):
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss=,pcpu=,cputime=,command="], text=True)
    rows = []
    names = {"watcher_mac.py": "watcher", "relay_mac.py": "relay",
             "early_focus_guard_mac.py": "focus", "puffs_mac.py": "puffs"}
    all_rows = []
    for line in output.splitlines():
        fields = line.strip().split(None, 5)
        if len(fields) == 6:
            all_rows.append(fields)
    descendants = {host_pid}
    for _ in range(4):
        descendants.update(int(row[0]) for row in all_rows if int(row[1]) in descendants)
    for pid, ppid, rss, cpu, used, command in all_rows:
        if int(pid) not in descendants:
            continue
        role = "tray" if int(pid) == host_pid else next(
            (value for name, value in names.items() if str(ROOT / name) in command), None)
        if role:
            rows.append({"pid": int(pid), "ppid": int(ppid), "rss_kib": int(rss),
                         "cpu_percent": float(cpu), "cpu_time": used, "role": role})
    if rows:
        data = subprocess.run(["lsof", "-nP", "-a", "-p", ",".join(str(row["pid"]) for row in rows),
                               "-Fpf"], capture_output=True, text=True, timeout=5).stdout
        current, counts = None, Counter()
        for line in data.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current = int(line[1:])
            elif re.match(r"f\d", line) and current is not None:
                counts[current] += 1
        for row in rows:
            row["fds"] = counts[row["pid"]]
    return rows


async def driver(args):
    from AppKit import NSWorkspace
    from Foundation import NSDate, NSRunLoop
    from Quartz import CGEventSourceSecondsSinceLastEventType, kCGEventSourceStateCombinedSessionState
    from playwright.async_api import async_playwright
    from websockets.asyncio.client import connect
    from cdp_relay import read_endpoint, RELAY_PATH
    from settings_model import DEFAULTS, save_atomic

    assert args.profile.resolve().is_relative_to(Path("/tmp").resolve()) and args.profile.name.startswith("yesdev-")
    assert read_json(args.profile / "Local State").get("devtools", {}).get("remote_debugging", {}).get("user-enabled") is True
    args.out.mkdir(parents=True, exist_ok=False)
    app_data = args.out / "app-data"
    save_atomic(app_data / "config.json", {**DEFAULTS, "quiet_focus": True, "relay_enabled": True,
        "relay_profile": str(args.profile), "relay_port": args.port})
    original = Path.home() / "Library/Application Support/YesDev/config.json"
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None
    started = datetime.now()
    browser_pid, tray, files, tasks = None, None, [], []
    stopping = asyncio.Event()
    resources, events, cases, failures, known_pids = [], [], [], [], set()
    baseline_runtimes = set(Path("/tmp").glob("yesdev-relay-*"))
    url = f"ws://127.0.0.1:{args.port}{RELAY_PATH}"
    clock_start = time.monotonic()

    def elapsed():
        return round(time.monotonic() - clock_start, 3)

    def event(name, **values):
        row = {"elapsed": elapsed(), "name": name, **values}
        events.append(row)
        with (args.out / "events.jsonl").open("a") as output:
            output.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    async def wait_until(predicate, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(.1)
        raise TimeoutError("Condition did not become ready")

    async def start_browser():
        nonlocal browser_pid
        (args.profile / "DevToolsActivePort").unlink(missing_ok=True)
        subprocess.run(["open", "-gna", "Google Chrome", "--args", "--user-data-dir=" + str(args.profile),
                        "--no-first-run", "about:blank"], check=True)
        await wait_until(lambda: (args.profile / "DevToolsActivePort").exists())
        endpoint = read_endpoint(args.profile)
        pids = subprocess.check_output(["lsof", "-nP", "-t", f"-iTCP:{endpoint.port}", "-sTCP:LISTEN"], text=True).split()
        assert len(set(pids)) == 1
        browser_pid = int(pids[0])
        validate_browser()
        event("browser-started", pid=browser_pid)

    def validate_browser():
        command = subprocess.check_output(["ps", "-p", str(browser_pid), "-o", "command="], text=True).strip()
        assert command.startswith(CHROME + " ") and f"--user-data-dir={args.profile} " in command

    async def stop_browser():
        if browser_pid is not None:
            validate_browser()
            os.kill(browser_pid, signal.SIGTERM)
            await wait_until(lambda: not subprocess.run(["ps", "-p", str(browser_pid), "-o", "pid="], capture_output=True).stdout.strip(), 8)

    async def ready():
        def check():
            status = read_json(args.out / "host-status.json")
            relay = read_json(app_data / "relay-status.json")
            return (status.get("engine") and status.get("relay") == relay.get("pid")
                    and relay.get("state") == "listening" and not status.get("normal_guard"))
        await wait_until(check, 25)

    async def command(ws, method="Browser.getVersion"):
        await ws.send(json.dumps({"id": 1, "method": method}))
        while True:
            reply = json.loads(await asyncio.wait_for(ws.recv(), 25))
            if reply.get("id") == 1:
                assert "result" in reply, reply
                return reply["result"]

    async def raw(hold=0):
        async with connect(url, proxy=None, close_timeout=1) as ws:
            assert "product" in await command(ws)
            if hold:
                deadline = time.monotonic() + hold
                while time.monotonic() < deadline:
                    await asyncio.sleep(min(30, deadline-time.monotonic()))
                    assert "product" in await command(ws)

    async def parallel():
        async with connect(url, proxy=None, close_timeout=1) as a, connect(url, proxy=None, close_timeout=1) as b, connect(url, proxy=None, close_timeout=1) as c:
            result = await asyncio.gather(command(a), command(b, "Target.getTargets"), command(c))
            assert "product" in result[0] and "targetInfos" in result[1] and "product" in result[2]
            await b.close()
            assert "product" in await command(a) and "product" in await command(c)

    async def playwright():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}", timeout=30000)
            try:
                session = await browser.new_browser_cdp_session()
                assert "product" in await session.send("Browser.getVersion")
                await session.detach()
            finally:
                await browser.close()

    async def cancelled():
        async with connect(url, proxy=None, close_timeout=1) as ws:
            await asyncio.sleep(.04)
        await asyncio.sleep(.2)
        await raw()

    async def case(name, callback):
        row = {"name": name, "start": elapsed()}
        try:
            await callback()
            row["passed"] = True
        except Exception as exc:
            row.update(passed=False, error=repr(exc))
            failures.append(row)
        row["end"] = elapsed()
        cases.append(row)
        write_json(args.out / "cases.json", cases)
        event("case", case_name=name, **{key: value for key, value in row.items() if key != "name"})

    async def set_settings(values):
        identifier = str(uuid.uuid4())
        write_json(args.out / "command.json", {"id": identifier, "values": values})
        result = await wait_until(lambda: (lambda value: value if value.get("id") == identifier else None)(read_json(args.out / "command-result.json")))
        assert result["ok"], result

    async def restart_helper(role):
        rows = await asyncio.to_thread(processes, tray.pid)
        previous = next(row["pid"] for row in rows if row["role"] == role)
        os.kill(previous, signal.SIGKILL if role == "focus" else signal.SIGTERM)
        if role != "focus":
            await wait_until(lambda: read_json(args.out / "host-status.json").get("engine" if role == "watcher" else "relay") not in (None, previous))
            await ready()
        await raw()
        rows = await asyncio.to_thread(processes, tray.pid)
        current = next(row["pid"] for row in rows if row["role"] == role)
        assert current != previous
        event("helper-recovered", role=role, old_pid=previous, new_pid=current)

    async def restart_chrome():
        async with connect(url, proxy=None, close_timeout=1) as ws:
            await command(ws)
            await stop_browser()
            await asyncio.wait_for(ws.wait_closed(), 8)
        await start_browser()
        await raw()

    async def settings_cycle(key):
        await set_settings({key: key == "observe_only"})
        status = read_json(args.out / "host-status.json")
        assert status["relay"] is None and status["normal_guard"] is None
        if key == "enabled":
            assert status["engine"] is None
        await set_settings({key: key == "enabled"})
        await ready()
        await raw()

    async def sample_resources():
        while not stopping.is_set():
            rows = await asyncio.to_thread(processes, tray.pid)
            known_pids.update(row["pid"] for row in rows)
            row = {"elapsed": elapsed(), "processes": rows,
                   "host": read_json(args.out / "host-status.json"),
                   "relay": read_json(app_data / "relay-status.json")}
            resources.append(row)
            with (args.out / "resources.jsonl").open("a") as output:
                output.write(json.dumps(row) + "\n")
            await asyncio.sleep(5)

    async def sample_focus():
        with (args.out / "focus.jsonl").open("w") as output:
            while not stopping.is_set():
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(.001))
                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                if app is not None:
                    output.write(json.dumps({"elapsed": elapsed(), "t": time.time(),
                        "pid": int(app.processIdentifier()), "bundle": str(app.bundleIdentifier()),
                        "idle": CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateCombinedSessionState, 0xffffffff)}) + "\n")
                await asyncio.sleep(.1)

    try:
        await start_browser()
        output = (args.out / "host-stdout.log").open("w")
        files.append(output)
        tray = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--host", "--out", str(args.out)],
                                stdout=output, stderr=subprocess.STDOUT, cwd=ROOT)
        await ready()
        clock_start = time.monotonic()
        tasks = [asyncio.create_task(sample_resources()), asyncio.create_task(sample_focus())]
        duration = args.minutes * 60
        event("soak-started", duration_seconds=duration, idle_start=duration/3, idle_end=duration*2/3)
        work = [("raw", raw), ("raw-held", lambda: raw(4)), ("three-clients", parallel),
                ("playwright", playwright), ("cancel-and-reconnect", cancelled)]
        scheduled = [(duration*.10, "focus-recovery", lambda: restart_helper("focus")),
                     (duration*.20, "watcher-recovery", lambda: restart_helper("watcher")),
                     (duration*.27, "relay-recovery", lambda: restart_helper("relay")),
                     (duration*.84, "chrome-recovery", restart_chrome),
                     (duration*.90, "observe-settings-cycle", lambda: settings_cycle("observe_only")),
                     (duration*.94, "off-settings-cycle", lambda: settings_cycle("enabled"))]
        cycle, next_work, last_progress = 0, 0, -60
        persistent = asyncio.create_task(case("persistent-client", lambda: raw(min(240, duration*.12))))
        tasks.append(persistent)
        while elapsed() < duration:
            now = elapsed()
            for monitor in tasks[:2]:
                if monitor.done():
                    raise RuntimeError("A measurement task stopped") from monitor.exception()
            if tray.poll() is not None:
                raise RuntimeError("Tray exited unexpectedly")
            if now-last_progress >= 30:
                event("progress", phase="idle" if duration/3 <= now < duration*2/3 else "mixed",
                      completed=len(cases), failures=len(failures), approvals=read_json(args.out / "host-status.json").get("approvals"))
                last_progress = now
            if scheduled and now >= scheduled[0][0]:
                _, name, callback = scheduled.pop(0)
                await case(name, callback)
            elif not duration/3 <= now < duration*2/3 and now >= next_work:
                name, callback = work[cycle % len(work)]
                await case(name, callback)
                cycle += 1
                next_work = now + args.interval
            else:
                await asyncio.sleep(.2)
        await persistent
        event("soak-finished", completed=len(cases), failures=len(failures))
    except BaseException as exc:
        failures.append({"name": "driver", "error": repr(exc)})
        raise
    finally:
        stopping.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if tray is not None and tray.poll() is None:
            tray.terminate()
            try:
                await asyncio.to_thread(tray.wait, 8)
            except subprocess.TimeoutExpired:
                tray.kill()
                await asyncio.to_thread(tray.wait)
        for output in files:
            output.close()
        browser_stopped = browser_pid is None
        try:
            await stop_browser()
            browser_stopped = True
        except Exception as exc:
            failures.append({"name": "browser-cleanup", "error": repr(exc)})
        # The puff process deliberately drains its short fade-out animations
        # after EOF. Give all owned helpers a bounded interval to finish.
        cleanup_started = time.monotonic()
        while True:
            survivors = []
            for pid in known_pids:
                with suppress(ProcessLookupError):
                    os.kill(pid, 0)
                    survivors.append(pid)
            if not survivors or time.monotonic()-cleanup_started >= 10:
                break
            await asyncio.sleep(.1)
        leftover_runtimes = [str(path) for path in set(Path("/tmp").glob("yesdev-relay-*")) - baseline_runtimes]
        unchanged = (hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None) == original_hash
        by_role = defaultdict(list)
        for sample in resources:
            for row in sample["processes"]:
                by_role[row["role"]].append({**row, "elapsed": sample["elapsed"]})
        resource_summary = {}
        for role, rows in by_role.items():
            resource_summary[role] = {"pids": sorted({r["pid"] for r in rows}),
                "rss_min_mib": round(min(r["rss_kib"] for r in rows)/1024, 2),
                "rss_max_mib": round(max(r["rss_kib"] for r in rows)/1024, 2),
                "last_rss_mib": round(rows[-1]["rss_kib"]/1024, 2),
                "max_fds": max(r["fds"] for r in rows), "last_fds": rows[-1]["fds"],
                "median_cpu_percent": statistics.median(r["cpu_percent"] for r in rows)}
        summary = {"duration_seconds": elapsed(), "cases": len(cases), "failed_cases": failures,
                   "passed": not failures and not survivors and not leftover_runtimes and unchanged,
                   "resources": resource_summary, "surviving_helpers": survivors,
                   "leftover_runtimes": leftover_runtimes, "original_config_unchanged": unchanged,
                   "browser_stopped": browser_stopped,
                   "helper_drain_seconds": round(time.monotonic()-cleanup_started, 3),
                   "final_host": read_json(args.out / "host-status.json")}
        write_json(args.out / "summary.json", summary)
        logs = await asyncio.create_subprocess_exec("/usr/bin/log", "show", "--info", "--debug", "--style", "compact",
            "--start", (started-timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "--end", (datetime.now()+timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"), "--predicate",
            'process == "WindowServer" AND eventMessage CONTAINS "Deferring events from frontmost process"',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await logs.communicate()
        (args.out / "windowserver.log").write_bytes(stdout)
        (args.out / "windowserver-stderr.txt").write_bytes(stderr)
        print(json.dumps(summary, indent=2), flush=True)
    if not summary["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=Path("/tmp/yesdev-test-profile"))
    parser.add_argument("--port", type=int, default=19336)
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--interval", type=float, default=30)
    options = parser.parse_args()
    if options.minutes <= 0 or options.interval < 5:
        parser.error("minutes must be positive and interval must be at least five seconds")
    if options.host:
        host(options)
    else:
        asyncio.run(driver(options))
