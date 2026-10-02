"""Single-request policy for the opt-in early-focus experiment.

This is correlation with a cooperating client, not proof of a consent sheet.
It never approves a debugging connection. No sockets or UI calls live here.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AppIdentity:
    pid: int
    started: float


@dataclass
class Pending:
    request_id: str
    browser: AppIdentity
    previous: AppIdentity
    armed_at: float
    expires_at: float
    used: bool = False


class ArmGate:
    def __init__(self, browser: AppIdentity, ttl: float = 2.0, quiet_s: float = 0.5):
        if not 0 < ttl <= 5:
            raise ValueError("expiry must be between zero and five seconds")
        self.browser = browser
        self.ttl = ttl
        self.quiet_s = quiet_s
        self.pending: Optional[Pending] = None

    def clear(self):
        self.pending = None

    def expire(self, now):
        if self.pending is not None and now >= self.pending.expires_at:
            self.clear()
            return True
        return False

    def arm(self, request_id, previous, live_browser, now, idle_age):
        self.expire(now)
        if self.pending is not None:
            return "busy"
        if live_browser != self.browser:
            return "browser-restarted-or-gone"
        if previous is None or previous == self.browser:
            return "no-previous-app"
        if idle_age < self.quiet_s:
            return "recent-input"
        self.pending = Pending(request_id, self.browser, previous, now, now + self.ttl)
        return "armed"

    def finish(self, request_id):
        if self.pending is None or self.pending.request_id != request_id:
            return "unknown-request"
        self.clear()
        return "finished"

    def input_changed(self, now, idle_age):
        p = self.pending
        if p is not None and not p.used and idle_age <= now - p.armed_at:
            self.clear()
            return True
        return False

    def activation(self, activated, previous, now, idle_age):
        """Consume before the restore call; no activation can restore twice."""
        if self.expire(now):
            return "expired"
        p = self.pending
        if p is None:
            return "unarmed"
        if p.used:
            return "already-used"
        if self.input_changed(now, idle_age):
            return "input-after-arm"
        if activated != p.browser:
            if activated != p.previous:
                self.clear()
                return "different-app"
            return "previous-app"
        if previous != p.previous:
            self.clear()
            return "previous-app-changed"
        p.used = True
        return "restore"
