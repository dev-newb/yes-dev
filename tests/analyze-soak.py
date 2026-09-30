"""Evaluate a completed soak using fixed, pre-run acceptance limits."""
import json
from pathlib import Path
import statistics
import sys

directory = Path(sys.argv[1])
report = json.loads((directory / 'soak-results.json').read_text(encoding='utf-8-sig'))
samples = [json.loads(line) for line in (directory / 'samples.jsonl').read_text().splitlines()]
MIB = 1024 * 1024

def slope_per_minute(rows, key):
    x = [r['elapsed_seconds'] / 60 for r in rows]
    y = [r[key] / MIB for r in rows]
    xm, ym = statistics.mean(x), statistics.mean(y)
    return sum((a-xm)*(b-ym) for a, b in zip(x, y)) / sum((a-xm)**2 for a in x)

phases = {}
idle_end = report['idle_seconds']
end = report['requested_seconds']
for name, start, stop in [('idle', 300, idle_end), ('active', idle_end + 300, end)]:
    rows = [r for r in samples if start <= r['elapsed_seconds'] <= stop]
    if len(rows) < 24:
        phases[name] = {'passed': False, 'reason': 'Not enough post-warmup samples'}
        continue
    early = [r for r in rows if r['elapsed_seconds'] <= start + 120]
    late = [r for r in rows if r['elapsed_seconds'] >= stop - 120]
    growth = (statistics.median(r['private_bytes'] for r in late)
              - statistics.median(r['private_bytes'] for r in early)) / MIB
    handle_growth = statistics.median(r['handles'] for r in late) - statistics.median(r['handles'] for r in early)
    slope = slope_per_minute(rows, 'private_bytes')
    phases[name] = {
        'samples': len(rows), 'private_start_mib': statistics.median(r['private_bytes'] for r in early) / MIB,
        'private_end_mib': statistics.median(r['private_bytes'] for r in late) / MIB,
        'private_growth_mib': growth, 'private_slope_mib_per_minute': slope,
        'handle_growth': handle_growth,
        'passed': growth <= 16 and slope <= 0.5 and handle_growth <= 64,
    }
checks = {
    'completed_requested_duration': report['completed'] and report['duration_seconds'] >= end,
    'at_least_30_minutes': end >= 1800,
    'no_failed_connections': report['failed_connections'] == 0,
    'one_count_per_connection': report['counted_dialogs'] == report['successful_connections'],
    'at_least_100_connections': report['successful_connections'] >= 100,
    'same_watcher_process': len({r['watcher_pid'] for r in samples}) == 1,
    'below_400_mib_ceiling': max(r['private_bytes'] for r in samples) < 400 * MIB,
    'no_idle_approvals': all(r['counted_dialogs'] == 0 for r in samples if r['phase'] == 'idle'),
    'idle_memory_and_handles': phases['idle']['passed'],
    'active_memory_and_handles': phases['active']['passed'],
}
analysis = {
    'mode': report['mode'], 'source_sha256': report['source_sha256'], 'checks': checks,
    'passed': all(checks.values()), 'phases': phases,
    'peak_private_mib': max(r['private_bytes'] for r in samples) / MIB,
    'peak_working_set_mib': max(r['working_set_bytes'] for r in samples) / MIB,
    'successful_connections': report['successful_connections'],
    'counted_dialogs': report['counted_dialogs'],
    'limits': {'private_growth_mib': 16, 'private_slope_mib_per_minute': 0.5, 'handle_growth': 64},
}
(directory / 'analysis.json').write_text(json.dumps(analysis, indent=2))
print(json.dumps(analysis, indent=2))
raise SystemExit(not analysis['passed'])
