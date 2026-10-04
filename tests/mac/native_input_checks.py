"""Explicitly authorized native input for the verified disposable Chrome only.

Cancel and new-tab trials use native UI actions, not human-hand observations.
PID-directed keys do not reset the idle timer on the tested Mac; run A5 with
record_cdp_traffic.py and actual typing instead. Evidence
stays outside Git. Never use a bundle id or foreground app as the input target.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import time

from record_cdp_traffic import LaunchRun, disposable_pid, write_json
from recon_cdp_live import Recon
import watcher_mac as w
from relay_mac import front_pid_with_window
from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
from Quartz import (CGEventCreateKeyboardEvent, CGEventPostToPid, CGEventSetFlags,
                    CGEventKeyboardSetUnicodeString, kCGEventFlagMaskCommand)


class Native:
    def verify(self):
        assert disposable_pid(self.profile) == self.pid

    def key(self, code, flags=0, text=None):
        self.verify()
        for down in (True, False):
            event = CGEventCreateKeyboardEvent(None, code, down)
            CGEventSetFlags(event, flags)
            if text is not None:
                CGEventKeyboardSetUnicodeString(event, len(text), text)
            CGEventPostToPid(self.pid, event)

    async def activate_native(self):
        self.verify()
        NSRunningApplication.runningApplicationWithProcessIdentifier_(self.pid).activateWithOptions_(
            NSApplicationActivateIgnoringOtherApps)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if await front_pid_with_window() == self.pid:
                return
            await asyncio.sleep(.05)
        raise RuntimeError('Disposable Chrome is not foreground')

    def nodes(self, root=None):
        self.verify()
        root = root if root is not None else w.AXUIElementCreateApplication(self.pid)
        stack = [(root, 0)]
        visited = 0
        while stack and visited < 2000:
            element, depth = stack.pop()
            visited += 1
            yield element
            if depth < 16 and w._attr(element, 'AXRole') not in ('AXMenuBar', 'AXWebArea'):
                stack.extend((child, depth + 1) for child in w._attr(element, 'AXChildren') or [])

    def omnibox(self):
        return next((e for e in self.nodes() if w._attr(e, 'AXRole') == 'AXTextField'
            and w._attr(e, 'AXDescription') == 'Address and search bar'), None)

    def sheets(self):
        engine = w.Engine(observe=True, log_path=self.case_out / 'native-observer.log')
        engine.log = lambda *_: None
        self.verify()
        return engine.find_dialog_hosts(w.AXUIElementCreateApplication(self.pid))

    async def ui(self, action, **details):
        request = {'action': action, 'operator': 'authorized_pid_bound_native_driver',
                   'result_path': str(self.case_out / 'ui-result.json'), **details}
        write_json(self.case_out / 'ui-request.json', request)
        self.event('native_ui_request', request=request)
        if action == 'click_cancel_on_disposable_launch_prompt':
            hosts = await self.wait_for(self.sheets, 60)
            await asyncio.sleep(.75)
            hosts = self.sheets()
            assert hosts, 'Consent sheet disappeared before Cancel'
            button = next((e for host in hosts for e in self.nodes(host)
                if w._attr(e, 'AXRole') == 'AXButton' and w._element_label(e).lower() == 'cancel'), None)
            assert button is not None, 'No Cancel button in verified remote-debugging sheet'
            self.verify()
            error = w.AXUIElementPerformAction(button, 'AXPress')
            self.event('native_cancel_pressed', pid=self.pid, error=error)
            assert error == 0
            await self.wait_for(lambda: not self.sheets(), 10)
            result = {'completed': True, 'operator': 'AXPress_on_verified_disposable_consent_Cancel'}
        elif action == 'open_native_tab_with_two_real_mcp_clients':
            gate_path = self.case_out / 'resume-gate.json'
            gate = json.loads(gate_path.read_text())
            gate['active_tab_only'] = True
            write_json(gate_path, gate)
            await self.activate_native()
            self.key(17, kCGEventFlagMaskCommand)  # Cmd-T: native tab, no CDP createTarget.
            await asyncio.sleep(.3)
            self.key(37, kCGEventFlagMaskCommand)
            await asyncio.sleep(.1)
            field = self.omnibox()
            assert field is not None and w._attr(field, 'AXFocused')
            self.key(0, text=details['url'])
            await asyncio.sleep(.1)
            actual = str(w._attr(field, 'AXValue'))
            assert actual.strip() == details['url'], actual
            self.key(36)  # Return submits native omnibox navigation.
            self.event('native_tab_navigation_submitted', pid=self.pid, url=actual)
            result = {'completed': True, 'operator': 'CGEventPostToPid_CmdT_and_omnibox', 'url': actual}
        else:
            raise ValueError(action)
        write_json(self.case_out / 'ui-result.json', result)
        self.event('native_ui_complete', action=action, result=result)
        return result


class NativeLaunch(Native, LaunchRun):
    pass


class NativeRecon(Native, Recon):
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--mode', choices=['launch', 'native-tab'], required=True)
    parser.add_argument('--tooling', type=Path)
    parser.add_argument('--only', action='append', choices=['A6'])
    args = parser.parse_args()
    args.port = 19339
    args.ui_timeout = 120
    args.start_delay = 0
    args.wait_idle = 3 if args.mode == 'launch' else 0
    args.hidden_background = False
    args.restore_background_app = False
    args.work_pause = 0
    if args.mode == 'launch':
        args.only = args.only or ['A6']
        runner = NativeLaunch(args)
    else:
        assert args.tooling is not None
        args.only = ['autoattach-native']
        runner = NativeRecon(args)
    raise SystemExit(asyncio.run(runner.run()))


if __name__ == '__main__':
    main()
