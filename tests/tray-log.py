"""Exercise the real tray log reader without starting the tray or a watcher."""
import ast
from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

repo = Path(__file__).resolve().parents[1]
tree = ast.parse((repo / 'yes_dev.pyw').read_text(encoding='utf-8-sig'))
method = next(node for cls in tree.body if isinstance(cls, ast.ClassDef)
              for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_read_log')
event = b'2026-09-30 12:00:00.000 [ACTION] APPROVED; dialog dismissed\r\n'
attempt = b'2026-09-30 12:00:00.000 [INFO] approval attempted; waiting\r\n'
results = []

def run_case(name, body):
    with tempfile.TemporaryDirectory(prefix='yes-dev-tail-') as temp:
        path = Path(temp) / 'watcher.log'
        path.write_bytes(b'')
        notices, bursts = [], []
        obj = SimpleNamespace(_log_pos=0, _log_pending=b'', _log_identity=None,
            approvals=0, recent=deque(), paused_reason=None, allow_until=None,
            cfg={'burst_limit': 2}, announce=notices.append, _on_burst=bursts.append)
        env = {'LOG_PATH': path, 'os': os, 'time': SimpleNamespace(time=lambda: 100.0),
               'datetime': datetime, 'BURST_WINDOW': 60.0}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<tray-log-reader>', 'exec'), env)
        read = lambda: env['_read_log'](obj)
        def append(data):
            with path.open('ab') as stream:
                stream.write(data)
        try:
            body(path, obj, read, append, notices, bursts)
            results.append({'name': name, 'passed': True})
        except Exception as error:
            results.append({'name': name, 'passed': False, 'error': str(error)})

def require(value, expected):
    if value != expected:
        raise AssertionError(f'expected {expected!r}, got {value!r}')

def normal(path, obj, read, append, notices, bursts):
    append(attempt * 3); read(); require(obj.approvals, 0)
    append(event); read(); read(); require(obj.approvals, 1); require(notices, [1]); require(bursts, [])
    append(event); read(); require(obj.approvals, 2); require(bursts, [2])
run_case('attempts do not count; each event counts once', normal)

for cut in range(1, len(event)):
    def split(path, obj, read, append, notices, bursts, cut=cut):
        append(event[:cut]); read(); require(obj.approvals, 0)
        append(event[cut:]); read(); read(); require(obj.approvals, 1)
    run_case(f'complete event split at byte {cut}', split)

def label_marker(path, obj, read, append, notices, bursts):
    append(b'2026-09-30 12:00:00.000 [INFO] button label: [ACTION]\r\n')
    read(); require(obj.approvals, 0)
run_case('ACTION in an INFO payload does not count', label_marker)

def rotation(path, obj, read, append, notices, bursts):
    append(event + b'2026-09-30 12:00:01.000 [ACT'); read(); require(obj.approvals, 1)
    path.rename(path.with_suffix('.old'))
    path.write_bytes(event * 3)
    read(); require(obj.approvals, 4)
run_case('rotation to a larger file resets identity and partial bytes', rotation)

def truncate(path, obj, read, append, notices, bursts):
    append(attempt * 4 + b'2026-09-30 12:00:01.000 [ACT'); read()
    path.write_bytes(event); read(); require(obj.approvals, 1)
run_case('truncation drops a previous partial event', truncate)

def long_line(path, obj, read, append, notices, bursts):
    append(event[:-2] + b'x' * 100000); read()
    require(obj.approvals, 0)
    if len(obj._log_pending) > 256:
        raise AssertionError('partial log buffer exceeds 256 bytes')
    append(b'\r\n'); read(); require(obj.approvals, 1)
run_case('unfinished long line keeps bounded prefix and counts once', long_line)

report = {'tests': results, 'passed': sum(t['passed'] for t in results),
          'failed': sum(not t['passed'] for t in results)}
Path(sys.argv[1]).write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps({k: report[k] for k in ('passed', 'failed')}))
raise SystemExit(bool(report['failed']))
