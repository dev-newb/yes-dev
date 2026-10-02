"""Read-only paired experiment for watcher autorelease accumulation.

Run once with --pool and once without, with separate output directories.
Neither process approves prompts or changes production source/settings.
"""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=240)
    parser.add_argument("--pool", action="store_true")
    parser.add_argument("--legacy-attribute-copy", action="store_true",
                        help="compare the old single-value AX read with the repaired array read")
    parser.add_argument("--leaks", action="store_true",
                        help="write a native leak report without allocation contents")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    os.environ["YESDEV_DATA_DIR"] = str(args.out / "app-data")
    import objc
    import watcher_mac as watcher
    if args.legacy_attribute_copy:
        from ApplicationServices import AXUIElementCopyAttributeValue
        watcher._copy_attribute = lambda element, name: AXUIElementCopyAttributeValue(element, name, None)
    engine = watcher.Engine(observe=True, poll_ms=250, log_path=args.out / "observer.log")
    start, next_sample, sweeps = time.monotonic(), 0, 0
    samples = []

    def sample(phase):
        path = args.out / "footprint-current.json"
        subprocess.run(["/usr/bin/footprint", "-p", str(os.getpid()), "--noCategories", "--swapped",
                        "-j", str(path)], capture_output=True, text=True, timeout=10, check=True)
        value = json.loads(path.read_text())["processes"][0]
        row = {"elapsed": round(time.monotonic()-start, 3), "phase": phase,
               "footprint_bytes": value["footprint"], "sweeps": sweeps}
        samples.append(row)
        (args.out / "samples.json").write_text(json.dumps(samples, indent=2) + "\n")
        print(json.dumps(row), flush=True)

    while time.monotonic()-start < args.seconds:
        with objc.autorelease_pool() if args.pool else nullcontext():
            engine.sweep()
        sweeps += 1
        if time.monotonic()-start >= next_sample:
            sample("loop")
            next_sample += 30
        time.sleep(engine.poll_s)
    sample("before-final-drain")
    objc.recycleAutoreleasePool()
    sample("after-final-drain")
    result = {"pool_per_sweep": args.pool, "legacy_attribute_copy": args.legacy_attribute_copy,
              "seconds": args.seconds, "sweeps": sweeps,
              "growth_mib": round((samples[-2]["footprint_bytes"]-samples[1]["footprint_bytes"])/2**20, 3),
              "drain_reclaimed_mib": round((samples[-2]["footprint_bytes"]-samples[-1]["footprint_bytes"])/2**20, 3)}
    if args.leaks:
        report = subprocess.run(["/usr/bin/leaks", "--noContent", "--groupByType", str(os.getpid())],
                                capture_output=True, text=True, timeout=45)
        (args.out / "leaks-no-content.txt").write_text(report.stdout + report.stderr)
        result["leaks_exit_code"] = report.returncode
    (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
