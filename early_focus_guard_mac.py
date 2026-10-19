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
from CoreFoundation import (
    CFFileDescriptorCreate, CFFileDescriptorCreateRunLoopSource, CFFileDescriptorEnableCallBacks,
    CFFileDescriptorInvalidate, CFRunLoopAddSource, CFRunLoopGetCurrent, kCFFileDescriptorReadCallBack,
    kCFRunLoopDefaultMode,
)
from Foundation import NSDate, NSDefaultRunLoopMode, NSObject, NSRunLoop, NSTimer

MAX_MESSAGE = 4096
HOLD_LIMIT_S = 6        # an unauthenticated client may not hold the only slot longer
PENDING_TICK_S = 0.005  # expiry and input checks, only while a request is armed
IDLE_SLICE_S = 1.0      # the loop also wakes for sockets, signals and notifications


def identity(app):
    if app is None or app.isTerminated() or app.launchDate() is None:
        return None
    return AppIdentity(int(app.processIdentifier()), float(app.launchDate().timeIntervalSince1970()))


def live_identity(pid):
    return identity(NSRunningApplication.runningApplicationWithProcessIdentifier_(pid))


def idle_age():
    return normal.CGEventSourceSecondsSinceLastEventType(
        normal.kCGEventSourceStateCombinedSessionState, normal.kCGAnyInputEventType)


class FdWatch:
    """Run callback on the run loop each time fd becomes readable.

    The caller keeps ownership of fd; closing the watch never closes it, and it
    must be closed before fd is, so a reused descriptor number is never watched
    on behalf of a socket that has gone.
    """

    def __init__(self, fd, callback):
        def fired(ref, _types, _info):
            try:
                callback()
            finally:
                # One callback per enable: re-arm unless the callback closed us.
                if self.ref is not None:
                    CFFileDescriptorEnableCallBacks(self.ref, kCFFileDescriptorReadCallBack)
        self._fired = fired   # CF holds no Python reference to the callable
        self.ref = CFFileDescriptorCreate(None, fd, False, fired, None)
        CFRunLoopAddSource(CFRunLoopGetCurrent(),
                           CFFileDescriptorCreateRunLoopSource(None, self.ref, 0),
                           kCFRunLoopDefaultMode)
        CFFileDescriptorEnableCallBacks(self.ref, kCFFileDescriptorReadCallBack)

    def close(self):
        ref, self.ref = self.ref, None
        if ref is not None:
            CFFileDescriptorInvalidate(ref)   # also removes its run-loop source


class ControlServer:
    """Small bounded protocol, serviced on the AppKit thread before ACK is sent.

    In the helper each socket is read only by its own run-loop watch: the
    listener's watch calls accept_pending(), which never reads, and the
    client's watch calls read_pending(), which never accepts. That rule is
    load-bearing. A CFFileDescriptor that is armed while data is waiting
    delivers its callback later; if the data has been read in the meantime by
    someone else, CoreFoundation drops the callback, and since a watch is only
    re-armed from its own callback, it never fires again. Accepting and reading
    in one pass did exactly that in 1.4.0: the first message on a connection
    was handled and every later one was not, until the next connection arrived
    and was refused because the old one was still open.

    poll() does one step of both, for the tests that drive the protocol
    without a run loop.
    """

    def __init__(self, path, token, handler, disconnected, watch=None):
        self.token = token
        self.handler = handler
        self.disconnected = disconnected
        # Optional run-loop hooks: client_opened(sock) and client_closing(sock).
        self.watch = watch
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
            if self.watch is not None:
                self.watch.client_closing(self.client)
            self.client.close()
            self.client = None
            self.buffer = b""
            self.disconnected()

    def expire_hold(self):
        """Unauthenticated clients cannot hold the sole connection indefinitely."""
        if self.client is not None and time.monotonic() - self.accepted_at > HOLD_LIMIT_S:
            self.drop()

    def accept_pending(self):
        """Take a waiting connection. Never reads it: the client's own watch does."""
        self.expire_hold()
        try:
            client, _ = self.listener.accept()
        except BlockingIOError:
            return
        if self.client is not None:
            client.close()
            return
        self.client = client
        client.setblocking(False)
        self.accepted_at = time.monotonic()
        if self.watch is not None:
            self.watch.client_opened(client)

    def read_pending(self):
        """Read the client once, then handle every whole line that has arrived."""
        if self.client is None:
            return
        try:
            data = self.client.recv(MAX_MESSAGE + 1)
        except BlockingIOError:
            data = None
        except OSError:
            self.drop()
            return
        if data == b"":
            self.drop()
            return
        if data:
            self.buffer += data
        while self.client is not None:
            if len(self.buffer) > MAX_MESSAGE:
                self.drop()
                return
            if b"\n" not in self.buffer:
                return
            self._handle_line()

    def poll(self):
        """One step of everything, for tests without a run loop."""
        self.accept_pending()
        self.read_pending()

    def _handle_line(self):
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
        self.listen_watch = None
        self.client_watch = None
        self.hold_timer = None
        self.pending_timer = None

    # -------- run-loop wiring: the helper sleeps until something happens --------

    def attach(self, control):
        self.control = control
        self.listen_watch = FdWatch(control.listener.fileno(), self.control_accept)

    def control_accept(self):
        self.control.accept_pending()
        self.sync_pending_timer()

    def control_read(self):
        self.control.read_pending()
        self.sync_pending_timer()

    def client_opened(self, sock):
        self.client_watch = FdWatch(sock.fileno(), self.control_read)
        self.hold_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            HOLD_LIMIT_S + 0.05, self.bridge, "holdLimit:", None, False)

    def client_closing(self, _sock):
        if self.client_watch is not None:
            self.client_watch.close()
            self.client_watch = None
        if self.hold_timer is not None:
            self.hold_timer.invalidate()
            self.hold_timer = None

    def sync_pending_timer(self):
        """Tick fast only while a request is armed: at most two seconds per connection."""
        if self.gate.pending is not None and self.pending_timer is None:
            self.pending_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                PENDING_TICK_S, self.bridge, "pendingTick:", None, True)
        elif self.gate.pending is None and self.pending_timer is not None:
            self.pending_timer.invalidate()
            self.pending_timer = None

    def detach(self):
        for timer in (self.hold_timer, self.pending_timer):
            if timer is not None:
                timer.invalidate()
        self.hold_timer = self.pending_timer = None
        if self.client_watch is not None:
            self.client_watch.close()
            self.client_watch = None
        if self.listen_watch is not None:
            self.listen_watch.close()
            self.listen_watch = None

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
        self.sync_pending_timer()
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

    def pending_tick(self):
        now = time.monotonic()
        if self.gate.expire(now):
            self.log("request expired", "AUDIT")
        elif self.gate.input_changed(now, idle_age()):
            self.log("request cancelled by input after ARM", "AUDIT")
        self.sync_pending_timer()


class _EarlyFocusBridge(NSObject):
    guard = None

    def activated_(self, note):
        self.guard.on_activated(note)

    def pendingTick_(self, _timer):
        self.guard.pending_tick()

    def holdLimit_(self, _timer):
        self.guard.hold_timer = None
        self.guard.control.expire_hold()
        self.guard.sync_pending_timer()

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
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="the relay's pid, passed by the relay; see watcher_mac.py")
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
    guard.attach(ControlServer(socket_path, token, guard.command, guard.disconnected, watch=guard))
    session_path = opts.runtime_dir / "session.json"
    staged_session = session_path.with_suffix(".tmp")
    with open(staged_session, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
        json.dump({"socket": str(socket_path), "token": token, "browser_pid": browser.pid,
                   "browser_started": browser.started, "profile": str(opts.profile.resolve()),
                   "endpoint": endpoint}, f)
    staged_session.replace(session_path)
    NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
        guard.bridge, "activated:", normal.NSWorkspaceDidActivateApplicationNotification, None)
    guard.log(f"early-focus experiment ready browser_pid={browser.pid} session={session_path}")
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    # A signal writes its number to this pipe, which wakes the run loop at once;
    # the Python handler above then runs as soon as the wake-up callback does.
    # Without it the loop would have to wake on a timer just to notice a signal.
    wake_r, wake_w = os.pipe()
    for fd in (wake_r, wake_w):
        os.set_blocking(fd, False)
    signal.set_wakeup_fd(wake_w)
    def drain_wake():
        try:
            os.read(wake_r, 512)
        except BlockingIOError:
            pass
    wake_watch = FdWatch(wake_r, drain_wake)
    try:
        # runMode:beforeDate: returns after any input source - a socket, the
        # signal pipe, an activation notification - or after IDLE_SLICE_S, so an
        # idle helper wakes once a second instead of two hundred times.
        while not stopping:
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(IDLE_SLICE_S))
            if opts.exit_with_parent and os.getppid() != guard.parent_pid:
                stopping = True
    finally:
        signal.set_wakeup_fd(-1)
        wake_watch.close()
        os.close(wake_r)
        os.close(wake_w)
        guard.detach()
        NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(guard.bridge)
        guard.control.close()
        session_path.unlink(missing_ok=True)
        socket_path.unlink(missing_ok=True)
        opts.runtime_dir.rmdir()
        guard.log("early-focus experiment stopped; runtime files removed")


if __name__ == "__main__":
    main()
