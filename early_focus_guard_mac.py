"""ARM/ACK focus guard for the opt-in local relay.

The runtime directory must not already exist. A cooperating test client reads
session.json from this private directory and waits for ACK before connecting.
Only the supplied browser PID/launch identity is eligible, once per request.
The regular watcher still independently recognizes and approves the sheet.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from focus_protocol import AppIdentity, ArmGate
import focus_guard_mac as normal
from AppKit import NSRunningApplication, NSWorkspace
from Foundation import NSDate, NSObject, NSRunLoop, NSTimer

MAX_MESSAGE = 4096


def identity(app):
    if app is None or app.isTerminated() or app.launchDate() is None:
        return None
    return AppIdentity(int(app.processIdentifier()), float(app.launchDate().timeIntervalSince1970()))


def live_identity(pid):
    return identity(NSRunningApplication.runningApplicationWithProcessIdentifier_(pid))


def idle_age():
    return normal.CGEventSourceSecondsSinceLastEventType(
        normal.kCGEventSourceStateCombinedSessionState, normal.kCGAnyInputEventType)


class ControlServer:
    """Small bounded protocol, polled on the AppKit thread before ACK is sent."""

    def __init__(self, path, token, handler, disconnected):
        self.token = token
        self.handler = handler
        self.disconnected = disconnected
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        os.chmod(path, 0o600)
        self.listener.listen(1)
        self.listener.setblocking(False)
        self.client = None
        self.buffer = b""
        self.accepted_at = 0.0

    def drop(self):
        if self.client is not None:
            self.client.close()
            self.client = None
            self.buffer = b""
            self.disconnected()

    def poll(self):
        # Unauthenticated clients cannot hold the sole connection indefinitely.
        if self.client is not None and time.monotonic() - self.accepted_at > 6:
            self.drop()
        try:
            client, _ = self.listener.accept()
        except BlockingIOError:
            client = None
        if client is not None:
            if self.client is not None:
                client.close()
            else:
                self.client = client
                client.setblocking(False)
                self.accepted_at = time.monotonic()
        if self.client is None:
            return
        if b"\n" not in self.buffer:
            try:
                data = self.client.recv(MAX_MESSAGE + 1)
            except BlockingIOError:
                return
            except OSError:
                self.drop()
                return
            if not data:
                self.drop()
                return
            self.buffer += data
        if len(self.buffer) > MAX_MESSAGE:
            self.drop()
            return
        if b"\n" not in self.buffer:
            return
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            message = json.loads(line)
            if not isinstance(message, dict) or not isinstance(message.get("token"), str):
                raise ValueError("invalid message")
            if not hmac.compare_digest(message["token"], self.token):
                raise ValueError("unauthorized")
            request_id = message.get("request_id")
            if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                raise ValueError("invalid request id")
            response = self.handler(message)
            # Replies are tiny and loopback-only; a blocked send cancels ARM.
            self.client.sendall((json.dumps(response) + "\n").encode())
            if message.get("op") in ("DONE", "CANCEL"):
                self.drop()
        except (ValueError, TypeError, OSError):
            self.drop()

    def close(self):
        self.drop()
        self.listener.close()


class EarlyGuard(normal.Guard):
    def __init__(self, opts, browser):
        super().__init__(opts)
        self.gate = ArmGate(browser, ttl=opts.ttl)
        self.previous = None
        self.current_identity = identity(NSWorkspace.sharedWorkspace().frontmostApplication())
        self.bridge = _EarlyFocusBridge.alloc().init()
        self.bridge.guard = self
        self.control = None

    def command(self, message):
        op, request_id = message.get("op"), message["request_id"]
        if op in ("DONE", "CANCEL"):
            result = self.gate.finish(request_id)
        elif op == "ARM":
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            previous = identity(front)
            if front is None or int(front.activationPolicy()) != normal.NSApplicationActivationPolicyRegular:
                result = "no-previous-app"
            elif front.bundleIdentifier() in self.bundles:
                result = "previous-app-is-browser"
            else:
                # Validate identity before the final input read.
                browser = live_identity(self.gate.browser.pid)
                idle = idle_age()
                result = self.gate.arm(request_id, previous, browser, time.monotonic(), idle)
                if result == "armed":
                    self.previous = self._describe(front)
                    self.current_identity = previous
        else:
            result = "unknown-operation"
        self.log(f"CONTROL {op} request={request_id} result={result}")
        return {"request_id": request_id, "status": result}

    def disconnected(self):
        if self.gate.pending is not None:
            self.log(f"CANCEL request={self.gate.pending.request_id} control disconnected")
        self.gate.clear()

    def on_activated(self, note):
        notified = time.monotonic()
        app = note.userInfo().get(normal.NSWorkspaceApplicationKey)
        activated = identity(app)
        if activated is None:
            return
        previous, self.current_identity = self.current_identity, activated
        pending = self.gate.pending
        request_id = pending.request_id if pending else None
        # Keep the final input read after the preceding identity lookups.
        idle = idle_age()
        decision = self.gate.activation(activated, previous, time.monotonic(), idle)
        self.log(f"ACTIVATED name={app.localizedName()} pid={activated.pid} "
                 f"request={request_id} decision={decision}", "AUDIT")
        if decision != "restore":
            return
        # Revalidate the destination and input immediately before the AX call.
        if live_identity(pending.previous.pid) != pending.previous:
            self.log("CANCEL previous app exited or restarted", "AUDIT")
            return
        idle = idle_age()
        restore_at = time.monotonic()
        if idle <= restore_at - pending.armed_at:
            self.log("CANCEL input before restore", "AUDIT")
            return
        how = self._restore(self.previous)
        self.log(f"EARLY_RESTORE request={request_id} previous_pid={pending.previous.pid} "
                 f"browser_pid={activated.pid} decision_ms={(restore_at-notified)*1000:.2f} "
                 f"restore_ms={(time.monotonic()-restore_at)*1000:.2f} via={how}", "ACTION")
        for delay in (0.15, 0.6, 1.5):
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                delay, self.bridge, "verify:", {"want": pending.previous.pid, "since": restore_at}, False)

    def poll(self):
        self.control.poll()
        now = time.monotonic()
        if self.gate.expire(now):
            self.log("request expired", "AUDIT")
        elif self.gate.input_changed(now, idle_age()):
            self.log("request cancelled by input after ARM", "AUDIT")


class _EarlyFocusBridge(NSObject):
    guard = None

    def activated_(self, note):
        self.guard.on_activated(note)

    def poll_(self, _timer):
        self.guard.poll()

    def verify_(self, timer):
        value = timer.userInfo()
        self.guard.verify(int(value["want"]), float(value["since"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-pid", type=int, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--ttl", type=float, default=2.0)
    parser.add_argument("--exit-with-parent", action="store_true")
    opts = parser.parse_args()
    opts.watch_pid, opts.include_edge = [opts.browser_pid], False
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(opts.browser_pid)
    browser = identity(app)
    if browser is None or str(app.bundleIdentifier()) not in normal.wm.CHROME_BUNDLES:
        parser.error("browser PID must identify a running Chrome variant")
    if not normal.is_trusted():
        parser.error("Accessibility permission is required")
    endpoint = (opts.profile / "DevToolsActivePort").read_text().splitlines()[:2]
    if len(endpoint) != 2:
        parser.error("profile has no live DevToolsActivePort")
    opts.runtime_dir.mkdir(mode=0o700)
    socket_path = opts.runtime_dir / "control.sock"
    token = secrets.token_hex(32)
    normal.NSApplication.sharedApplication().setActivationPolicy_(normal.NSApplicationActivationPolicyAccessory)
    guard = EarlyGuard(opts, browser)
    guard.control = ControlServer(socket_path, token, guard.command, guard.disconnected)
    session_path = opts.runtime_dir / "session.json"
    staged_session = session_path.with_suffix(".tmp")
    with open(staged_session, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
        json.dump({"socket": str(socket_path), "token": token, "browser_pid": browser.pid,
                   "browser_started": browser.started, "profile": str(opts.profile.resolve()),
                   "endpoint": endpoint}, f)
    staged_session.replace(session_path)
    NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
        guard.bridge, "activated:", normal.NSWorkspaceDidActivateApplicationNotification, None)
    poll_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.005, guard.bridge, "poll:", None, True)
    guard.log(f"early-focus experiment ready browser_pid={browser.pid} session={session_path}")
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    try:
        # Return to Python regularly so SIGTERM can end an idle accessory app.
        # NSApplication.stop_ alone can wait indefinitely for an AppKit event.
        while not stopping:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
            if opts.exit_with_parent and os.getppid() != guard.parent_pid:
                stopping = True
    finally:
        poll_timer.invalidate()
        NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(guard.bridge)
        guard.control.close()
        session_path.unlink(missing_ok=True)
        socket_path.unlink(missing_ok=True)
        opts.runtime_dir.rmdir()
        guard.log("early-focus experiment stopped; runtime files removed")


if __name__ == "__main__":
    main()
