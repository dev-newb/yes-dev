"""Test the real launch method without importing or starting the tray app."""
import ast
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1]
output = Path(sys.argv[1])
tree = ast.parse((root / 'yes_dev.pyw').read_text(encoding='utf-8-sig'))
method = next(node for cls in tree.body if isinstance(cls, ast.ClassDef)
              for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'start_engine')
calls = []
env = {
    'WATCHER': root / 'watcher.ps1',
    'LOG_PATH': output.parent / 'unused-test.log',
    'subprocess': SimpleNamespace(Popen=lambda args, **kwargs:
        calls.append({'args': args, **kwargs}) or SimpleNamespace(pid=98765)),
    'CREATE_NO_WINDOW': subprocess.CREATE_NO_WINDOW,
    'os': SimpleNamespace(getpid=lambda: 12345),
    'log': lambda message: None,
    'datetime': datetime,
    'timedelta': timedelta,
}
exec(compile(ast.Module(body=[method], type_ignores=[]), '<tray-start-engine>', 'exec'), env)
results = []
for edge, observe in [(False, False), (True, False), (True, True)]:
    instance = SimpleNamespace(engine_running=lambda: False,
        cfg={'include_edge': edge, 'poll_ms': 250, 'observe_only': observe, 'arm_minutes': 0})
    env['start_engine'](instance)
    args = calls[-1]['args']
    passed = (
        args[args.index('-BrowserProcess') + 1] == ('chrome,msedge' if edge else 'chrome')
        and ('-Observe' in args) == observe
        and args[args.index('-ParentPid') + 1] == '12345'
        and calls[-1]['creationflags'] == subprocess.CREATE_NO_WINDOW
    )
    results.append({'name': f'include_edge={edge}, observe={observe}', 'passed': passed})
before = len(calls)
env['start_engine'](SimpleNamespace(engine_running=lambda: True))
results.append({'name': 'already running prevents launch', 'passed': len(calls) == before})
report = {'tests': results, 'passed': sum(t['passed'] for t in results),
          'failed': sum(not t['passed'] for t in results)}
output.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report))
raise SystemExit(bool(report['failed']))
