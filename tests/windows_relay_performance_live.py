"""Bounded idle/load checks using an owned relay and private headless Chrome.

Records process counters and latency; it does not impose machine-speed limits.
The default 90-second idle window is a short resumption check, not an overnight soak.
"""
import argparse
import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cdp_relay
from relay_windows import RelayManager
from windows_runtime import Native
from windows_relay_live import Cdp, PrivateChrome
from windows_relay_adversarial_live import free_port


def counters(process):
    native = Native()
    kernel = native.kernel
    times = [wintypes.FILETIME() for _ in range(4)]
    handle = wintypes.HANDLE(int(process._handle))
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(x) for x in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    def seconds(value):
        return ((value.dwHighDateTime << 32) | value.dwLowDateTime) / 10_000_000
    count = wintypes.DWORD()
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    if not kernel.GetProcessHandleCount(handle, ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
    class Memory(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('peak_ws', 'ws', 'peak_paged', 'paged',
                                               'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile', 'private')]
    memory = Memory()
    memory.cb = ctypes.sizeof(memory)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {'cpu_seconds': seconds(times[2]) + seconds(times[3]), 'handles': count.value,
            'private_bytes': memory.private, 'working_set_bytes': memory.ws}


def latency(values):
    ordered = sorted(values)
    return {'count': len(values), 'median_ms': statistics.median(values),
            'p95_ms': ordered[min(len(ordered)-1, int(len(ordered)*.95))]}


async def run(args, result):
    chrome = PrivateChrome(args.chrome)
    helper = None
    with tempfile.TemporaryDirectory(prefix='yesdev-relay-performance-') as folder:
        manager = RelayManager(ROOT, Path(folder))
        port = free_port()
        url = f'ws://127.0.0.1:{port}/devtools/browser/yesdev'
        async def wait(predicate, seconds=20):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                manager.sync(True, str(chrome.profile), port)
                if predicate():
                    return
                await asyncio.sleep(.05)
            raise TimeoutError(manager.status_text())
        async def settled():
            await wait(lambda: manager._status().get('connections') == 0)
        async def query_latency(endpoint):
            samples = []
            async with Cdp(endpoint) as client:
                for n in range(220):
                    start = time.perf_counter()
                    await client.call('Browser.getVersion')
                    if n >= 20:
                        samples.append((time.perf_counter()-start)*1000)
            return latency(samples)
        try:
            # No Chrome endpoint yet: listening itself must not open a socket.
            await wait(lambda: manager.status_text() == 'Relay: listening')
            helper = manager._process
            await asyncio.sleep(.3)
            assert helper.poll() is None
            assert manager._status().get('held') is False
            result['starts_before_chrome'] = True
            await chrome.start()
            async with Cdp(url) as client:
                result['browser'] = await client.call('Browser.getVersion')
            await settled()
            idle_before = counters(helper)
            idle_start = time.monotonic()
            for _ in range(args.idle_seconds):
                manager.sync(True, str(chrome.profile), port)
                await asyncio.sleep(1)
            idle_after = counters(helper)
            elapsed = time.monotonic()-idle_start
            async with Cdp(url) as client:
                await client.call('Browser.getVersion')
            await settled()
            assert helper.poll() is None and manager._process is helper
            result['idle'] = {'seconds': elapsed, 'before': idle_before, 'after': idle_after,
                              'one_core_cpu_percent': 100*(idle_after['cpu_seconds']-idle_before['cpu_seconds'])/elapsed,
                              'resumed_same_helper': True}
            # Alternate order to reduce systematic warm-up bias.
            result['latency'] = []
            direct = cdp_relay.read_endpoint(chrome.profile).url
            for label, endpoint in [('direct', direct), ('relay', url), ('relay', url), ('direct', direct)]:
                result['latency'].append({'path': label, **await query_latency(endpoint)})
                await settled()
            # Large replies followed by repeated clean detach/reattach.
            async with Cdp(url) as client:
                target = (await client.call('Target.createTarget', {'url':'about:blank'}))['targetId']
                session = (await client.call('Target.attachToTarget', {'targetId':target,'flatten':True}))['sessionId']
                for size in (1024*1024, 4*1024*1024):
                    reply = await client.call('Runtime.evaluate', {'expression':f'"x".repeat({size})','returnByValue':True}, session)
                    assert len(reply['result']['value']) == size
                await client.call('Target.closeTarget', {'targetId':target})
            await settled()
            result['large_replies_bytes'] = [1024*1024,4*1024*1024]
            checkpoints = [counters(helper)]
            reconnect_times = []
            for n in range(300):
                start=time.perf_counter()
                async with Cdp(url) as client:
                    await client.call('Browser.getVersion')
                await settled()
                reconnect_times.append((time.perf_counter()-start)*1000)
                if (n+1)%100 == 0:
                    checkpoints.append(counters(helper))
            result['reconnections'] = {'latency':latency(reconnect_times), 'resource_checkpoints':checkpoints}
            log=(Path(folder)/'relay.log').read_text(encoding='utf-8')
            result['held_upstream_opens'] = log.count('Holding a Chrome connection open for later clients')
            assert result['held_upstream_opens'] == 1, result['held_upstream_opens']
            # Handles and private memory should not grow per-client without bound.
            assert checkpoints[-1]['handles'] <= checkpoints[0]['handles']+8, checkpoints
            assert checkpoints[-1]['private_bytes'] <= checkpoints[0]['private_bytes']+16*1024*1024, checkpoints
            result['passed']=True
        except Exception:
            result['manager_status'] = manager.status_text()
            result['host_status'] = manager._status()
            log_path = Path(folder)/'relay.log'
            result['host_log_tail'] = log_path.read_text(encoding='utf-8')[-12000:] if log_path.exists() else ''
            raise
        finally:
            manager.stop('performance test end')
            deadline=time.monotonic()+12
            while manager._process is not None or manager._worker is not None or manager._sampler is not None:
                if time.monotonic()>deadline:
                    if helper is not None and helper.poll() is None:
                        helper.kill()
                        await asyncio.to_thread(helper.wait,5)
                    raise TimeoutError('Owned test helper did not stop')
                await asyncio.sleep(.05)
            result['owned_helper_stopped']=helper is None or helper.poll() is not None
            await chrome.cleanup()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--idle-seconds',type=int,default=90)
    parser.add_argument('--chrome',type=Path,default=Path(os.environ['PROGRAMFILES'])/'Google/Chrome/Application/chrome.exe')
    args=parser.parse_args()
    result={'passed':False,'scope':'owned Windows helper and fresh headless Chrome; short load/idle check',
            'source_hashes':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in ('cdp_relay.py','relay_windows.py')}}
    try:
        asyncio.run(run(args,result))
    except Exception:
        result['error']=traceback.format_exc()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
