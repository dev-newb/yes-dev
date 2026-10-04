"""Build line-addressable inventories from record_cdp_traffic live JSONL files.

Read-only with respect to traces. Use --out outside Git, beside the raw evidence.
Every browser-level command retains its exact params and matched response.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
from pathlib import Path


def analyze(root, out):
    out.mkdir(parents=True, exist_ok=True)
    browser_calls, autoattach, clients, target_events = [], [], [], []
    for path in sorted(root.glob('**/trace-*.jsonl')):
        frames = []
        for line_number, text in enumerate(path.read_text().splitlines(), 1):
            frames.append((line_number, json.loads(text)))
        refs = {'trace': str(path), 'run': path.parent.parent.name, 'case': path.parent.name}
        responses = {(f['socket'], f['message']['id']): (line, f)
            for line, f in frames if f['direction'] == 'received'
            and isinstance(f['message'], dict) and 'id' in f['message']}
        client = path.stem.removeprefix('trace-')
        sockets = sorted({f['socket'] for _, f in frames})
        sent = received = 0
        method_counts = collections.Counter()
        for line, frame in frames:
            message = frame['message']
            if not isinstance(message, dict):
                continue
            if frame['direction'] == 'sent':
                sent += 1
            elif frame['direction'] == 'received':
                received += 1
            if frame['direction'] == 'sent' and 'method' in message:
                method_counts[message['method']] += 1
                record = {**refs, 'client': client, 'line': line, 't': frame['t'],
                          'socket': frame['socket'], 'request': message}
                reply = responses.get((frame['socket'], message.get('id')))
                if reply:
                    record.update(reply_line=reply[0], reply_time=reply[1]['t'], reply=reply[1]['message'])
                if 'sessionId' not in message:
                    browser_calls.append(record)
                if message['method'] == 'Target.setAutoAttach':
                    autoattach.append({**record, 'scope': 'session' if 'sessionId' in message else 'browser'})
            if message.get('method') in {'Target.attachedToTarget', 'Target.detachedFromTarget',
                    'Target.targetCreated', 'Target.targetDestroyed', 'Runtime.runIfWaitingForDebugger',
                    'Target.createTarget', 'Page.frameStartedLoading', 'Page.frameNavigated',
                    'Page.domContentEventFired', 'Page.loadEventFired', 'Network.requestWillBeSent'}:
                target_events.append({**refs, 'client': client, 'line': line, **frame})
        clients.append({**refs, 'client': client, 'sockets': sockets, 'socket_count': len(sockets),
                        'sent_frames': sent, 'received_frames': received, 'method_counts': dict(method_counts),
                        'first_t': frames[0][1]['t'] if frames else None,
                        'last_t': frames[-1][1]['t'] if frames else None})
    for name, value in [('browser-calls.json', browser_calls), ('autoattach.json', autoattach),
                         ('clients.json', clients), ('target-events.json', target_events)]:
        (out / name).write_text(json.dumps(value, indent=2) + '\n')
    with (out / 'browser-calls.tsv').open('w') as stream:
        writer = csv.writer(stream, delimiter='\t')
        writer.writerow(['run', 'case', 'client', 'method', 'params', 'trace', 'line', 'reply_line', 'error'])
        for item in browser_calls:
            writer.writerow([item['run'], item['case'], item['client'], item['request']['method'],
                json.dumps(item['request'].get('params', '<omitted>'), separators=(',', ':')),
                item['trace'], item['line'], item.get('reply_line', ''),
                json.dumps(item.get('reply', {}).get('error', ''), separators=(',', ':'))])
    print(json.dumps({'clients': len(clients), 'browser_calls': len(browser_calls),
                      'autoattach_calls': len(autoattach), 'target_events': len(target_events)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    analyze(args.root.resolve(), args.out.resolve())


if __name__ == '__main__':
    main()
