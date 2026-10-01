"""Conservative focus restoration for a newly appeared debugging prompt.

This cannot prevent Chrome's initial activation. The history model is independent
of macOS so timing and user-interaction cases can be checked without moving focus.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class FocusChange:
    previous_pid: int
    browser_pid: int
    observed_at: float


class FocusHistory:
    MAX_DELAY = 2.0
    INPUT_MARGIN = 0.25

    def __init__(self, browser_bundles):
        self.browser_bundles = set(browser_bundles)
        self.current_pid = None
        self.current_regular = False
        self.pending = None

    def observe(self, pid, bundle, regular, now, last_intent):
        if pid != self.current_pid:
            self.pending = None
            if (pid is not None and self.current_pid is not None
                    and self.current_regular and bundle in self.browser_bundles
                    and now - last_intent > self.INPUT_MARGIN):
                self.pending = FocusChange(self.current_pid, pid, now)
            self.current_pid = pid
            self.current_regular = regular
        if self.pending and (
                now - self.pending.observed_at > self.MAX_DELAY
                or last_intent >= self.pending.observed_at - self.INPUT_MARGIN):
            self.pending = None

    def take_target(self, browser_pid):
        change = self.pending
        if (change is None or self.current_pid != browser_pid
                or change.browser_pid != browser_pid):
            return None
        self.pending = None  # One attempt; never fight the user in a retry loop.
        return change.previous_pid


class MacQuietFocus:
    def __init__(self, browser_bundles, log):
        from AppKit import NSWorkspace
        self.workspace = NSWorkspace.sharedWorkspace()
        self.history = FocusHistory(browser_bundles)
        self.log = log
        self.last_intent = float('-inf')
        self.sample()

    def sample(self):
        from AppKit import NSApplicationActivationPolicyRegular
        from Quartz import (
            CGEventSourceSecondsSinceLastEventType, CGEventSourceFlagsState,
            kCGEventSourceStateHIDSystemState, kCGEventLeftMouseDown,
            kCGEventRightMouseDown, kCGEventOtherMouseDown, kCGEventFlagsChanged,
            kCGEventKeyDown, kCGEventFlagMaskCommand, kCGEventFlagMaskControl,
            kCGEventFlagMaskAlternate,
        )
        now = time.monotonic()
        events = [kCGEventLeftMouseDown, kCGEventRightMouseDown,
                  kCGEventOtherMouseDown, kCGEventFlagsChanged]
        flags = CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState)
        if flags & (kCGEventFlagMaskCommand | kCGEventFlagMaskControl
                    | kCGEventFlagMaskAlternate):
            events.append(kCGEventKeyDown)
        for event in events:
            age = CGEventSourceSecondsSinceLastEventType(
                kCGEventSourceStateHIDSystemState, event)
            self.last_intent = max(self.last_intent, now - age)
        app = self.workspace.frontmostApplication()
        self.history.observe(
            app.processIdentifier() if app else None,
            app.bundleIdentifier() if app else None,
            bool(app and app.activationPolicy() == NSApplicationActivationPolicyRegular),
            now, self.last_intent)

    def wait(self, seconds):
        # AppKit caches changing properties until its main run loop advances.
        from Foundation import NSDate, NSRunLoop
        end = time.monotonic() + seconds
        while True:
            remaining = end - time.monotonic()
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(max(0, min(0.05, remaining))))
            self.sample()
            if time.monotonic() >= end:
                return

    def restore_for(self, browser_pid):
        from AppKit import NSRunningApplication
        from ApplicationServices import AXUIElementCreateApplication, AXUIElementSetAttributeValue
        self.wait(0)
        target_pid = self.history.take_target(browser_pid)
        if target_pid is None:
            return
        target = NSRunningApplication.runningApplicationWithProcessIdentifier_(target_pid)
        if target is None or target.isTerminated() or target.isHidden():
            return
        # Check again immediately before the write; a newer app selection wins.
        current = self.workspace.frontmostApplication()
        if current is None or current.processIdentifier() != browser_pid:
            return
        err = AXUIElementSetAttributeValue(
            AXUIElementCreateApplication(target_pid), 'AXFrontmost', True)
        self.wait(0.05)
        restored = self.history.current_pid == target_pid
        self.log(f'quiet focus target_pid={target_pid} AXFrontmost err={err} '
                 f'restored={restored}', 'FOCUS')
