"""Kill the real isolated tray and observe all of its helpers from outside.

The 0.25-second trials meet the launch-time request. Because imports and tray
startup can take longer, additional trials kill it just after actual helper
creation to exercise that race rather than pass vacuously.
All evidence is written outside the repository. Never kills a preexisting PID.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from settings_model import DEFAULTS,save_atomic
from verify_hold_live import disposable_pid,write_json
from websockets.asyncio.client import connect

ROLES={'watcher_mac.py':'watcher','relay_mac.py':'relay',
       'early_focus_guard_mac.py':'early_guard','focus_guard_mac.py':'quiet_guard'}


def helper_inventory():
    rows={}
    output=subprocess.check_output(['ps','-axo','pid=,ppid=,stat=,command='],text=True)
    for line in output.splitlines():
        parts=line.strip().split(None,3)
        if len(parts)!=4:continue
        pid,parent,state,command=parts
        role=next((role for script,role in ROLES.items()
                   if command.startswith(sys.executable+' '+str(ROOT/script)+' ')),None)
        if role:
            declared=re.search(r'--parent-pid (\d+)',command)
            rows[int(pid)]={'pid':int(pid),'parent_pid':int(parent),'state':state,
                           'role':role,'declared_parent':int(declared[1]) if declared else None,
                           'command':command}
    return rows


class Trial:
    def __init__(self,args,label):
        self.args=args
        self.out=args.out/label
        self.out.mkdir()
        self.data=self.out/'app-data'
        save_atomic(self.data/'config.json',{**DEFAULTS,'enabled':True,'observe_only':False,
            'quiet_focus':True,'relay_enabled':True,'relay_hold':True,'relay_port':args.port,
            'relay_profile':str(args.profile.resolve()),'notify_style':'none'})
        self.baseline=helper_inventory()
        assert not self.baseline,'Other real helpers are running; do not overlap tests'
        self.seen={}
        self.samples=[]
        self.finished=threading.Event()
        self.started=time.monotonic()
        self.log=open(self.out/'tray-stdout.log','w')
        self.tray=subprocess.Popen([sys.executable,str(ROOT/'yes_dev_mac.py')],cwd=ROOT,
            env={**os.environ,'YESDEV_DATA_DIR':str(self.data)},stdout=self.log,stderr=subprocess.STDOUT)
        self.thread=threading.Thread(target=self.monitor,daemon=True)
        self.thread.start()

    def monitor(self):
        while not self.finished.is_set():
            all_rows=helper_inventory()
            now=time.monotonic()
            owned={self.tray.pid,*self.seen.keys()}
            for _ in range(3):
                for pid,row in all_rows.items():
                    if pid not in self.baseline and (row['parent_pid'] in owned or row['declared_parent'] in owned):
                        owned.add(pid)
                        if pid not in self.seen:self.seen[pid]={**row,'first_seen':now}
                        self.seen[pid]['last_seen']=now
                        self.seen[pid]['last_parent']=row['parent_pid']
            self.samples.append({'t':now,'alive':[pid for pid in self.seen if pid in all_rows]})
            self.finished.wait(.015)

    async def ready(self):
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            assert self.tray.poll() is None,'Tray exited before the test kill'
            try:
                status=json.loads((self.data/'relay-status.json').read_text())
                if status.get('state')=='listening' and {'watcher','relay'}<={r['role'] for r in self.seen.values()}:return
            except (OSError,ValueError):pass
            await asyncio.sleep(.025)
        raise RuntimeError('Tray helpers did not become ready')

    async def grant(self):
        async with connect(f'ws://127.0.0.1:{self.args.port}/devtools/browser/yesdev',proxy=None,
                           open_timeout=20,ping_interval=None) as ws:
            await ws.send(json.dumps({'id':1,'method':'Browser.getVersion'}))
            while True:
                reply=json.loads(await asyncio.wait_for(ws.recv(),20))
                if reply.get('id')==1:
                    assert 'product' in reply.get('result',{}),reply
                    return reply

    async def run(self,mode):
        result={'mode':mode,'tray_pid':self.tray.pid,'passed':False}
        try:
            if mode=='ready':
                await self.ready()
                result['grant_reply']=await self.grant()
                deadline=time.monotonic()+3
                while time.monotonic()<deadline and 'early_guard' not in {r['role'] for r in self.seen.values()}:
                    await asyncio.sleep(.02)
                assert {'watcher','relay','early_guard'}<={r['role'] for r in self.seen.values()},self.seen
            elif mode=='early':
                await asyncio.sleep(max(0,.25-(time.monotonic()-self.started)))
            elif mode=='helper-launch':
                deadline=time.monotonic()+12
                while time.monotonic()<deadline and not self.seen:
                    assert self.tray.poll() is None
                    await asyncio.sleep(.003)
                assert self.seen,'No helper launch observed'
            engine_log=self.data/'yes-dev.log'
            result['engine_started_before_kill']=engine_log.exists() and 'engine started' in engine_log.read_text()
            result['helper_roles_seen_before_kill']=sorted({r['role'] for r in self.seen.values()})
            result['kill_t']=time.monotonic()
            result['kill_after_launch_seconds']=result['kill_t']-self.started
            self.tray.kill()
            await asyncio.to_thread(self.tray.wait,3)
            # Observe past the full two-second deadline even if no child has
            # appeared yet: late imports must not hide a newly orphaned helper.
            await asyncio.sleep(max(0,result['kill_t']+2.3-time.monotonic()))
            result['helpers']=list(self.seen.values())
            result['last_observed_alive_seconds_after_kill']=max(
                [max(0,r['last_seen']-result['kill_t']) for r in self.seen.values()],default=0)
            late=[s for s in self.samples if s['t']>=result['kill_t']+2 and s['alive']]
            result['alive_samples_after_two_seconds']=late
            result['remaining_helpers']=[r for pid,r in helper_inventory().items() if pid in self.seen]
            assert not late and not result['remaining_helpers'],result
            if mode=='early':assert result['kill_after_launch_seconds']<=.3,result
            result['passed']=True
        except Exception as exc:
            result['error']=repr(exc)
        finally:
            if self.tray.poll() is None:self.tray.kill();await asyncio.to_thread(self.tray.wait)
            self.finished.set()
            await asyncio.to_thread(self.thread.join,3)
            # Cleanup only recorded test-owned helpers, after recording failure.
            for pid,row in helper_inventory().items():
                if pid in self.seen and row['command']==self.seen[pid]['command']:
                    with suppress(ProcessLookupError):os.kill(pid,signal.SIGTERM)
            self.log.close()
            write_json(self.out/'samples.json',self.samples)
            write_json(self.out/'result.json',result)
        return result


async def main(args):
    args.out=args.out.resolve()
    args.out.mkdir(exist_ok=False)
    assert disposable_pid(args.profile)==args.browser_pid
    results=[]
    for label,mode in [('ready','ready'),*[(f'early-{i}','early') for i in range(1,6)],
                       *[(f'helper-launch-{i}','helper-launch') for i in range(1,6)]]:
        print('START '+label,flush=True)
        value=await Trial(args,label).run(mode)
        results.append(value)
        write_json(args.out/'results.json',results)
        print(json.dumps({k:value.get(k) for k in ['mode','passed','kill_after_launch_seconds',
            'helper_roles_seen_before_kill','last_observed_alive_seconds_after_kill','error']}),flush=True)
        if not value['passed']:break
    return 0 if len(results)==11 and all(x['passed'] for x in results) else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile',type=Path,required=True)
    p.add_argument('--browser-pid',type=int,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--port',type=int,default=19334)
    raise SystemExit(asyncio.run(main(p.parse_args())))
