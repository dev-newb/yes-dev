"""Exercise real tray settings/helper lifetimes in a fresh data directory.

Starts the normal watcher and relay, but opens no CDP connection and causes no
Chrome activation. Never changes the user's configuration or login settings.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--port", type=int, default=19335)
    args = p.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=False)
    os.environ["YESDEV_DATA_DIR"] = str(args.data_dir.resolve())
    from settings_model import DEFAULTS, save_atomic
    initial = {**DEFAULTS, "enabled": False, "quiet_focus": True, "relay_enabled": True,
               "relay_profile": str(args.profile.resolve()), "relay_port": args.port, "notify_style": "none"}
    save_atomic(args.data_dir / "config.json", initial)
    import platform_mac
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    from yes_dev_mac import YesDev
    assert platform_mac.is_trusted(), "Existing runtime needs Accessibility for the watcher"
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app = YesDev()
    result = {}
    login = platform_mac.autostart_enabled()
    try:
        app.apply_settings({**initial, "enabled": True}, login)
        deadline = time.monotonic() + 5
        status_path = args.data_dir / "relay-status.json"
        while time.monotonic() < deadline:
            if status_path.exists() and json.loads(status_path.read_text()).get("state") == "listening":
                break
            time.sleep(.05)
        else:
            raise RuntimeError("Tray did not start the relay")
        assert app.engine_running() and app.relay.poll() is None and app.guard is None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{args.port}/json/version", timeout=2) as response:
            discovery = json.load(response)
        assert str(args.port) in discovery["webSocketDebuggerUrl"]
        result["settings_start_engine_and_relay"] = True
        result["no_competing_normal_guard"] = True
        old_relay, old_engine = app.relay, app.proc
        app.apply_settings({**app.cfg, "observe_only": True}, login)
        assert old_relay.poll() is not None and old_engine.poll() is not None
        assert app.relay is None and app.guard is None and app.engine_running()
        result["observe_stops_relay_and_restarts_watcher"] = True
        observer = app.proc
        app.apply_settings({**app.cfg, "enabled": False}, login)
        assert observer.poll() is not None and app.proc is None
        result["off_stops_remaining_helpers"] = True
    finally:
        app.shutdown()

    # Use the real native helper without asking Chrome to activate. A crashed
    # helper must be replaced on the next request, and no runtime can remain.
    from relay_mac import MacFocus
    from cdp_relay import read_endpoint
    async def helper_recovery():
        focus = MacFocus(args.profile, args.data_dir / "focus-recovery.log")
        try:
            async with focus.request(read_endpoint(args.profile)):
                first = focus.process
                first_runtime = Path(focus.runtime.name)
            first.kill()
            await first.wait()
            async with focus.request(read_endpoint(args.profile)):
                assert focus.process.pid != first.pid
                assert not first_runtime.exists()
                final_runtime = Path(focus.runtime.name)
            result["crashed_focus_helper_replaced"] = True
        finally:
            await focus.close()
        assert not final_runtime.exists()
        result["focus_runtime_cleaned"] = True
    asyncio.run(helper_recovery())
    (args.data_dir / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
