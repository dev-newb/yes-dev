"""Measure the macOS focus guard against the flasher, with WindowServer as the judge.

Launches tests/mac/focus_flasher.py, then focus_guard_mac.py watching that pid
with the sheet check off, lets the flasher steal focus N times, and lines up
three records: the flasher's activation times, the guard's restore lines, and
WindowServer's own "frontmost process" log, which is the only one of the three
that says what the user actually saw.

Per flash it reports how long the stand-in was frontmost (the visible blink),
how long the guard took to react, and whether focus came back to the app that
had it. Run it with nothing in your hands: a click or keypress during the run
is read by the guard as a deliberate switch and the restore is skipped, which
is correct behaviour but ruins the measurement.

    python3 tests/mac/measure_quiet_focus.py --count 10 --interval 2
"""
from __future__ import annotations

import argparse
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})")


def ts(line: str) -> datetime | None:
    m = TS.match(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f") if m else None


def windowserver_frontmost(start: datetime, end: datetime) -> list[tuple[datetime, int, str]]:
    cmd = ["/usr/bin/log", "show", "--info", "--debug", "--style", "compact",
           "--start", start.strftime("%Y-%m-%d %H:%M:%S"), "--end", end.strftime("%Y-%m-%d %H:%M:%S"),
           "--predicate", 'process == "WindowServer" AND eventMessage CONTAINS "Deferring events from frontmost process"']
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        m = re.search(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+).*frontmost process PSN \S+ \(([^)]*)\) -> <pid: (\d+)>", line)
        if m:
            t = datetime.strptime(m.group(1)[:23], "%Y-%m-%d %H:%M:%S.%f")
            rows.append((t, int(m.group(3)), m.group(2)))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--delay", type=float, default=4.0)
    ap.add_argument("--quiet-s", type=float, default=0.5)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--out", default=None, help="directory for the logs (default: a temp dir)")
    opts = ap.parse_args(argv)

    out = Path(opts.out or tempfile.mkdtemp(prefix="yesdev-focus-")); out.mkdir(parents=True, exist_ok=True)
    flasher_log, guard_log = out / "flasher.log", out / "guard.log"
    for p in (flasher_log, guard_log):
        p.write_text("")

    # Whatever is frontmost now is the app the guard must give focus back to.
    # Anything we launch from here inherits activation at launch (we are a child
    # of the active app), so after both processes are up we hand focus back to
    # this app ourselves, the same way the guard does, before any flash.
    from AppKit import NSWorkspace
    from ApplicationServices import AXUIElementCreateApplication, AXUIElementSetAttributeValue
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    prev_pid, prev_name = int(front.processIdentifier()), str(front.localizedName())
    print(f"frontmost before launch: {prev_name} pid={prev_pid}")

    t0 = datetime.now()
    flasher = subprocess.Popen([opts.python, str(HERE / "focus_flasher.py"), "--delay", str(opts.delay),
                                "--count", str(opts.count), "--interval", str(opts.interval), "--log", str(flasher_log)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(0.8)   # let the flasher's window exist before the guard seeds "current frontmost"
    guard = subprocess.Popen([opts.python, str(ROOT / "focus_guard_mac.py"), "--watch-pid", str(flasher.pid),
                              "--no-require-sheet", "--quiet-s", str(opts.quiet_s), "--log-path", str(guard_log)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    print(f"flasher pid={flasher.pid}  guard pid={guard.pid}  logs in {out}")
    time.sleep(1.0)
    err = AXUIElementSetAttributeValue(AXUIElementCreateApplication(prev_pid), "AXFrontmost", True)
    print(f"handed focus back to {prev_name}: AXFrontmost err={err}")
    try:
        flasher.wait(timeout=opts.delay + opts.interval * (opts.count + 2) + 10)
    except subprocess.TimeoutExpired:
        flasher.kill()
    time.sleep(1.0)
    guard.terminate()
    t1 = datetime.now()
    time.sleep(2.0)   # let the unified log catch up

    acts = [(ts(l), re.search(r"before=(.*?) pid=(\d+)", l)) for l in flasher_log.read_text().splitlines() if "ACTIVATE" in l]
    acts = [(t, m.group(1), int(m.group(2))) for t, m in acts if t and m]
    restores = [ts(l) for l in guard_log.read_text().splitlines() if "[ACTION] RESTORE" in l]
    skips = [l for l in guard_log.read_text().splitlines() if "skip:" in l]
    verified = [l for l in guard_log.read_text().splitlines() if "restored" in l and "after" in l]
    ws = windowserver_frontmost(t0 - timedelta(seconds=1), t1 + timedelta(seconds=1))

    print(f"\nflashes={len(acts)} guard restores={len(restores)} guard skips={len(skips)} "
          f"windowserver frontmost events={len(ws)}\n")
    # "visible blink" is WindowServer's own interval between the stand-in becoming
    # frontmost and the previous app taking it back: the only column that says
    # what a person would have seen. "WS front->guard" is the guard's reaction,
    # measured to its restore log line, which is written after the restore call
    # returns, so it slightly overstates the reaction.
    print(f"{'#':>2}  {'prev app':<22} {'flash->WS front':>15} {'WS front->guard':>15} {'visible blink':>13}  back to prev?")
    blinks, reacts = [], []
    for i, (t_act, prev_name, prev_pid) in enumerate(acts, 1):
        ws_front = next((r for r in ws if r[1] == flasher.pid and r[0] >= t_act - timedelta(milliseconds=50)), None)
        ws_back = next((r for r in ws if ws_front and r[0] > ws_front[0] and r[1] != flasher.pid), None)
        g = next((r for r in restores if r >= t_act - timedelta(milliseconds=50)), None)
        def d(a, b): return f"{(b - a).total_seconds() * 1000:6.0f}ms" if a and b else "     -"
        blink = (ws_back[0] - ws_front[0]).total_seconds() * 1000 if ws_front and ws_back else None
        if blink is not None: blinks.append(blink)
        if ws_front and g: reacts.append((g - ws_front[0]).total_seconds() * 1000)
        ok = "yes" if ws_back and ws_back[1] == prev_pid else ("no" if ws_back else "n/a")
        print(f"{i:>2}  {prev_name[:22]:<22} {d(t_act, ws_front[0] if ws_front else None):>15} "
              f"{d(ws_front[0] if ws_front else None, g):>15} "
              f"{(f'{blink:6.0f}ms' if blink is not None else '     -'):>13}  {ok}")
    if blinks:
        print(f"\nvisible blink (WindowServer): min {min(blinks):.0f}ms  median {statistics.median(blinks):.0f}ms  max {max(blinks):.0f}ms  over {len(blinks)} flashes")
    if reacts:
        print(f"guard reaction (WS front -> restore issued): min {min(reacts):.0f}ms  median {statistics.median(reacts):.0f}ms  max {max(reacts):.0f}ms")
    if skips:
        print("\nguard skips:"); [print("  " + s.split("] ", 1)[-1]) for s in skips[:10]]
    if verified:
        print("\nguard verification lines:"); [print("  " + v.split("] ", 1)[-1]) for v in verified[:5]]
    if not ws:
        print("\n(no WindowServer rows: /usr/bin/log needs to be run outside a sandbox)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
