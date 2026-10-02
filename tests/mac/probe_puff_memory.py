"""Matched native puff lifetime probe in offscreen test windows.

The real _Puff constructor, animation and destruction run unchanged. Only its
screen geometry is replaced so test clouds never cover the user's desktop.
"""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--pool", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    os.environ["YESDEV_DATA_DIR"] = str(args.out / "app-data")
    import objc
    import puffs_mac as puffs
    from AppKit import NSApplication, NSApplicationActivationPolicyProhibited
    from Foundation import NSDate, NSRunLoop, NSDefaultRunLoopMode, NSMakeRect

    class Offscreen:
        def visibleFrame(self):
            return NSMakeRect(-20000, -20000, 1920, 1080)
        def backingScaleFactor(self):
            return 2.0
    puffs._status_screen = lambda: Offscreen()
    random.seed(4391)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    app.finishLaunching()
    start, next_puff, next_sample, created = time.monotonic(), 0, 0, 0
    live, samples = [], []

    def sample():
        path = args.out / "footprint-current.json"
        subprocess.run(["/usr/bin/footprint", "-p", str(os.getpid()), "--noCategories", "--swapped",
                        "-j", str(path)], capture_output=True, text=True, timeout=10, check=True)
        value = json.loads(path.read_text())["processes"][0]
        row = {"elapsed": round(time.monotonic()-start, 3), "created": created,
               "footprint_bytes": value["footprint"]}
        samples.append(row)
        (args.out / "samples.json").write_text(json.dumps(samples, indent=2)+"\n")
        print(json.dumps(row), flush=True)

    while time.monotonic()-start < args.seconds:
        with objc.autorelease_pool() if args.pool else nullcontext():
            now = time.monotonic()
            if now-start >= next_puff and now-start < args.seconds-8:
                live.append(puffs._Puff())
                created += 1
                next_puff += 3
            live[:] = [puff for puff in live if puff.step(time.perf_counter())]
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(.03))
        if time.monotonic()-start >= next_sample:
            sample()
            next_sample += 30
    for puff in live:
        puff.destroy()
    sample()
    result = {"pool_per_frame": args.pool, "created": created, "seconds": args.seconds,
              "growth_after_30s_mib": round((samples[-1]["footprint_bytes"]-samples[1]["footprint_bytes"])/2**20, 3)}
    (args.out / "result.json").write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
