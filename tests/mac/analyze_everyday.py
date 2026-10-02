"""Collect physical footprint during, or summarize after, an everyday soak."""
import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import time


def records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def observe(out):
    with (out / "focus.jsonl").open() as source:
        first = json.loads(source.readline())
    started = first["t"] - first["elapsed"]
    host_pid = json.loads((out / "host-status.json").read_text())["pid"]
    while True:
        try:
            os.kill(host_pid, 0)
        except ProcessLookupError:
            break
        rows = records(out / "resources.jsonl")[-1]["processes"]
        pids = {row["pid"]: row["role"] for row in rows}
        capture = out / "footprint-current.json"
        command = ["/usr/bin/footprint", "--noCategories", "--swapped", "-j", str(capture)]
        for pid in pids:
            command += ["-p", str(pid)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        sample = {"elapsed": round(time.time()-started, 3), "exit_code": result.returncode}
        if capture.exists():
            payload = json.loads(capture.read_text())
            sample["processes"] = [{"pid": value["pid"], "role": pids.get(value["pid"], "unknown"),
                "footprint_bytes": value["footprint"],
                "physical_peak_bytes": value.get("auxiliary", {}).get("phys_footprint_peak")}
                for value in payload.get("processes", [])]
            sample["errors"] = payload.get("errors", [])
        with (out / "footprint.jsonl").open("a") as output:
            output.write(json.dumps(sample) + "\n")
        time.sleep(30)


def cpu_seconds(value):
    fields = value.split(":")
    return sum(float(field)*60**index for index, field in enumerate(reversed(fields)))


def analyze(out):
    resources = records(out / "resources.jsonl")
    footprints = records(out / "footprint.jsonl")
    cases = json.loads((out / "cases.json").read_text())
    summary = json.loads((out / "summary.json").read_text())
    groups = defaultdict(list)
    for sample in resources:
        for row in sample["processes"]:
            groups[(row["role"], row["pid"])].append({**row, "elapsed": sample["elapsed"]})
    stable = {}
    for (role, pid), rows in groups.items():
        span = rows[-1]["elapsed"]-rows[0]["elapsed"]
        if span < stable.get(role, {}).get("span_seconds", -1):
            continue
        # Compare one-minute medians after initial setup, within one PID.
        start_at = min(rows[0]["elapsed"]+60, rows[-1]["elapsed"])
        opening = [row for row in rows if start_at <= row["elapsed"] < start_at+60]
        closing = [row for row in rows if row["elapsed"] >= rows[-1]["elapsed"]-60]
        opening = opening or rows[:1]
        rss_start = statistics.median(row["rss_kib"] for row in opening)/1024
        rss_end = statistics.median(row["rss_kib"] for row in closing)/1024
        stable[role] = {"pid": pid, "span_seconds": round(span, 1),
            "rss_start_mib": round(rss_start, 2), "rss_end_mib": round(rss_end, 2),
            "rss_change_mib": round(rss_end-rss_start, 2),
            "fds_start_median": statistics.median(row["fds"] for row in opening),
            "fds_end_median": statistics.median(row["fds"] for row in closing),
            "average_cpu_percent": round(100*(cpu_seconds(rows[-1]["cpu_time"])-cpu_seconds(rows[0]["cpu_time"]))/max(span,1), 3)}
        native = [{**row, "elapsed": sample["elapsed"]} for sample in footprints for row in sample.get("processes", [])
                  if row["pid"] == pid]
        if native:
            after_warmup = [row for row in native if row["elapsed"] >= start_at] or native
            initial = after_warmup[:2]
            final = native[-2:]
            first_size = statistics.median(row["footprint_bytes"] for row in initial)/2**20
            last_size = statistics.median(row["footprint_bytes"] for row in final)/2**20
            stable[role].update(footprint_start_mib=round(first_size,2), footprint_end_mib=round(last_size,2),
                footprint_change_mib=round(last_size-first_size,2),
                footprint_peak_mib=round(max(row["footprint_bytes"] for row in native)/2**20,2))
    data = out / "app-data"
    focus = (data / "relay-focus.log").read_text() if (data / "relay-focus.log").exists() else ""
    relay = (data / "relay.log").read_text() if (data / "relay.log").exists() else ""
    restores = re.findall(r"EARLY_RESTORE request=([\w-]+)", focus)
    duplicated = [key for key, count in Counter(restores).items() if count > 1]
    # Recoveries and deliberate cancellations may generate expected warnings.
    # Unhandled Python tracebacks and tray tick failures are never expected.
    error_logs, handled_approval_retries = [], []
    for path in [out / "host-stdout.log", *data.glob("*.log")]:
        content = path.read_text(errors="replace")
        handled_approval_retries += [line for line in content.splitlines()
                                     if "[ERROR]" in line and "retrying next sweep" in line]
        if any(marker in content for marker in ("Traceback (most recent call last)", "tick error:",
                                                "loop error:", "frame failed:", "spawn failed:")):
            error_logs.append(path.name)
    outcomes = Counter()
    for case in cases:
        outcomes[case["name"]] += 1
    start_event = next(row for row in records(out / "events.jsonl") if row["name"] == "soak-started")
    idle_groups = defaultdict(list)
    for sample in footprints:
        if start_event["idle_start"]+30 <= sample["elapsed"] <= start_event["idle_end"]-30:
            for row in sample.get("processes", []):
                idle_groups[(row["role"], row["pid"])].append({**row, "elapsed": sample["elapsed"]})
    idle = {}
    for (role, pid), rows in idle_groups.items():
        if len(rows) < 3:
            continue
        span = rows[-1]["elapsed"]-rows[0]["elapsed"]
        change = (rows[-1]["footprint_bytes"]-rows[0]["footprint_bytes"])/2**20
        idle[role] = {"pid": pid, "span_seconds": round(span, 1),
                      "footprint_change_mib": round(change, 3),
                      "change_mib_per_minute": round(change/(span/60), 3)}
    memory_review = [role for role, value in idle.items()
                     if value["span_seconds"] >= 120 and value["footprint_change_mib"] >= 1]
    result = {"functional_and_cleanup_passed": summary["passed"], "case_counts": dict(outcomes),
        "successful_cdp_upstreams": relay.count("Client connected ("),
        "focus": {"arms": focus.count("result=armed"), "restores": len(restores),
                  "recent_input_skips": relay.count("Connection focus: recent-input"),
                  "input_cancellations": focus.count("request cancelled by input after ARM") + focus.count("decision=input-after-arm") + focus.count("CANCEL input before restore"),
                  "already_browser": relay.count("Connection focus: previous-app-is-browser"),
                  "duplicate_restore_requests": duplicated},
        "longest_stable_processes": stable, "idle_physical_footprint": idle,
        "idle_memory_growth_needing_review": memory_review,
        "unhandled_error_logs": error_logs,
        "handled_approval_retries": handled_approval_retries,
        "resource_samples": len(resources), "footprint_samples": len(footprints),
        "scope": "Thirty-minute bounded workload; process RSS and physical footprint, not proof of day-long stability."}
    (out / "analysis.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("--observe", action="store_true")
    args = parser.parse_args()
    if args.observe:
        observe(args.out)
    else:
        analyze(args.out)
