"""Yes, Dev engine for macOS: auto-approves Chrome's "Allow remote debugging?"
consent dialog through the Accessibility API.

This is the macOS counterpart of watcher.ps1. The contract with the tray is the
only thing that must stay identical: append a line containing `[ACTION]` to the
log for each approval, in the existing format

    2026-08-27 16:11:28.644 [ACTION]   APPROVED via AXPress

and the counter, the clouds and the burst guard in the tray all work unchanged.
`[ACTION]` is only written after the sheet is gone - and "gone" is judged by the
identity of the AX refs that were pressed (a dismissed sheet's refs answer every
read with AXError -25202), never by whether something still sits at its
coordinates: Chrome queues these prompts when several CDP clients connect at
once and draws the next one exactly where the last one was. Under multi-client
load AXPress can report success while the sheet outlives the verify wait. On
Chrome 153 AXPress is acknowledged and Allow never runs, and a second AXPress
removes the sheet while leaving the debug socket unapproved - success in the
log, nothing granted. So the fallback is a keystroke posted to Chrome's pid
instead: no pointer movement, no app activation, and it cannot land anywhere
but Chrome. A sheet that survives both is logged FAILED and retried next sweep.

The dialog's shape in the accessibility tree is an AXSheet on the browser
window wrapping an alert. Chrome 152 titled the sheet itself. Chrome 153 leaves
AXTitle empty and puts the same string on an AXHeading inside it:

    AXWindow  "<tab title> - Google Chrome"
      AXSheet  title=""
        AXGroup / AXSubrole=AXApplicationAlertDialog
          AXHeading  "Allow remote debugging?"
          AXButton  "Turn off in settings" | "Cancel" | "Allow"

Two things that title alone will not tell you: the same title is also carried
by the Window menu's AXMenuItem and by an AXHeading inside the dialog, so the
role has to match too - though find_dialog_hosts() only ever reads a Chrome
process's AXWindows and their AXSheets, so neither is actually reachable; the
role check is defense in depth, not the only thing standing between it and a
false positive. That scope is also the perf fix: earlier builds walked every
AXChild up to twelve levels deep, which on a loaded page means the page's own
AX-exposed DOM, on every single poll (confirmed at ~150-160ms/pid via
--diagnostics). The dialog only ever lives two AX reads from the app root, so
that's all this looks at now. And Chrome sets no AXIdentifier here, so dedupe
keys on position and size. `docs/mac/ax_probe.py` re-dumps the tree if a
future Chrome moves it.

Runs standalone:

    python3 watcher_mac.py --observe        # log dialogs and buttons, never click
    python3 watcher_mac.py --once           # one sweep, then exit (prints findings)
    python3 watcher_mac.py                   # the real loop, clicking Allow
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from platform_mac import DATA_DIR, LOG_PATH, ensure_data_dir, is_trusted
except ImportError:  # allow running from another cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from platform_mac import DATA_DIR, LOG_PATH, ensure_data_dir, is_trusted

try:
    import objc
    from ApplicationServices import (
        AXUIElementCreateApplication,
        AXUIElementCopyMultipleAttributeValues,
        kAXCopyMultipleAttributeOptionStopOnError,
        AXUIElementPerformAction,
        AXUIElementSetAttributeValue,
        AXValueGetValue,
        kAXErrorInvalidUIElement,
        kAXErrorSuccess,
        kAXValueCGPointType,
        kAXValueCGSizeType,
    )
    from AppKit import NSRunningApplication, NSUserDefaults, NSWorkspace
    from Quartz import CGEventCreateKeyboardEvent, CGEventPostToPid
except ImportError:
    sys.exit(
        "Yes, Dev macOS engine needs pyobjc:\n"
        "    pip3 install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices pyobjc-framework-Quartz"
    )

# Chrome variants. Edge is Chromium too and shows the same dialog; the tray adds
# it to the bundle set when "Include Microsoft Edge" is on.
CHROME_BUNDLES = ("com.google.Chrome", "com.google.Chrome.beta",
                  "com.google.Chrome.canary", "com.google.Chrome.dev")
EDGE_BUNDLES = ("com.microsoft.edgemac", "com.microsoft.edgemac.beta")

# Match the dialog by title, the approve button by an anchored label. The anchor
# is not fussiness: web pages carry their own "Allow" buttons (permission chips,
# ad blockers) and a loose match clicks them; it also keeps us off "Turn off in
# settings", which disables the whole feature.
DIALOG_PATTERN = re.compile(r"^allow remote debugging\??$", re.I)
APPROVE_PATTERN = re.compile(r"^(allow|approve)$", re.I)


def _is_consent_host(title: str, has_heading: bool, is_dialog: bool) -> bool:
    """Whether this element is the remote-debugging consent container.

    Chrome 152 puts the prompt on the sheet's own title. Chrome 153 leaves
    that empty and puts the same string on a heading inside the sheet. A
    dialog with some other title, or an untitled element that is not a
    dialog, is not it.

    @param title - AXTitle or AXDescription, already stripped.
    @param has_heading - True when a descendant heading matches the prompt.
    @param is_dialog - True when the element is a sheet or alert dialog.
    @returns True when the element should be searched for the Allow button.
    """
    if not is_dialog:
        return False
    if DIALOG_PATTERN.match(title):
        return True
    return title == "" and has_heading


# ponytail: title-vs-heading decision only. A live sheet still needs Chrome.
assert _is_consent_host("Allow remote debugging?", False, True)
assert _is_consent_host("", True, True)
assert not _is_consent_host("", False, True)
assert not _is_consent_host("GitHub", True, True)
assert not _is_consent_host("", True, False)

POLL_MS_DEFAULT = 250
DEDUPE_SECONDS = 2.0     # don't re-press one dialog mid-teardown ...
DEDUPE_MAX = 400         # ... but cap the memory so a long run can't grow forever
AUDIT_MAX = 200          # distinct sheet-candidate decisions remembered before the set resets
# ponytail: fixed sleep, not AX notification. Bump if Chrome teardown gets slower.
VERIFY_WAIT_S = 0.5
# pyobjc does not re-export Carbon's kVK_* virtual keycodes.
VK_TAB = 0x30
VK_SPACE = 0x31
# Chromium's InputEventActivationProtector drops input within 500ms of a
# security-sensitive dialog appearing. On Chrome 154 that covers AXPress too:
# a press inside the window returns success and does nothing, and only a later
# press clears the sheet. So both the first press and any keystroke wait it
# out, measured from the sweep that first saw the sheet.
ACTIVATION_GUARD_S = 0.6
# Tab stops to walk looking for Allow: three buttons plus slack.
MAX_TAB_STOPS = 6




def _copy_attribute(element, name):
    """Read one attribute through an owned array, preserving AX error codes.

    PyObjC 12.2.2 leaks copied strings returned directly through CFTypeRef out
    parameters. The array path lets the bridge balance ownership for every
    value type, without manual CFRelease calls or version-specific patches.
    """
    err, values = AXUIElementCopyMultipleAttributeValues(
        element, [name], kAXCopyMultipleAttributeOptionStopOnError, None)
    return err, values[0] if err == kAXErrorSuccess and values else None


def _attr(element, name):
    """One AX attribute, or None. Every read can fail (permission, torn-down
    element); callers treat None as 'not present' rather than crashing."""
    try:
        err, value = _copy_attribute(element, name)
    except Exception:
        return None
    return value if err == kAXErrorSuccess else None


def _ref_alive(element) -> bool | None:
    """Whether an AX ref still points at a live node - the one thing _attr's
    None cannot say, since it also covers a merely empty attribute.

    True: the node answered. False: kAXErrorInvalidUIElement (-25202), which is
    what a dismissed sheet and every button under it return on any read once
    Chrome tears the dialog down, and which a live node never returns. None:
    no answer either way (Chrome busy, AX unreachable) - proof of nothing."""
    try:
        err, _ = _copy_attribute(element, "AXRole")
    except Exception:
        return None
    if err == kAXErrorSuccess:
        return True
    if err == kAXErrorInvalidUIElement:
        return False
    return None


def _children(element):
    """A window's dialog can hang off AXSheets as readily as AXChildren, and the
    probe may reveal AXWindows nesting too - walk all three the way ax_probe does."""
    out = []
    for bucket in ("AXChildren", "AXSheets", "AXWindows"):
        kids = _attr(element, bucket)
        if kids:
            out.extend(kids)
    return out


def _ax_point(element, name="AXPosition"):
    """Unpack a CGPoint-typed AX attribute to (x, y). A pyobjc AXValue does not
    expose .pointValue(); it must go through AXValueGetValue, which is why the
    first cut of the dedupe key silently fell back to object identity."""
    val = _attr(element, name)
    if val is None:
        return None
    ok, pt = AXValueGetValue(val, kAXValueCGPointType, None)
    return (pt.x, pt.y) if ok else None


def _ax_size(element, name="AXSize"):
    val = _attr(element, name)
    if val is None:
        return None
    ok, sz = AXValueGetValue(val, kAXValueCGSizeType, None)
    return (sz.width, sz.height) if ok else None


# The consent prompt shows up in the tree three ways: the AXSheet on the browser
# window, the AXGroup/AXApplicationAlertDialog it wraps, and - a false positive -
# the Window menu's AXMenuItem bearing the same title (plus the AXHeading inside).
# Only the first two are real dialog containers with the Allow button beneath them.
_DIALOG_ROLES = {"AXSheet", "AXDialog"}
_DIALOG_SUBROLES = {"AXApplicationAlertDialog", "AXDialog", "AXSystemDialog"}


def _is_dialog_role(element) -> bool:
    if (_attr(element, "AXRole") or "") in _DIALOG_ROLES:
        return True
    return (_attr(element, "AXSubrole") or "") in _DIALOG_SUBROLES


def _element_label(element) -> str:
    """AXTitle, or AXDescription when the title is empty."""
    return str(_attr(element, "AXTitle") or _attr(element, "AXDescription") or "").strip()


def _has_dialog_heading(element, depth: int = 0) -> bool:
    """Whether an AXHeading under element carries the consent prompt.

    Only called for an untitled sheet. The heading sits a few groups down
    from the sheet, above the buttons, so this never enters the page DOM.

    @param element - Dialog-role element whose own title was empty.
    @param depth - How many levels below that element this call is.
    @returns True when a heading matches DIALOG_PATTERN.
    """
    if depth > 5:
        return False
    if (_attr(element, "AXRole") or "") == "AXHeading":
        return bool(DIALOG_PATTERN.match(_element_label(element)))
    for child in _children(element)[:8]:
        if _has_dialog_heading(child, depth + 1):
            return True
    return False


def _keyboard_navigation() -> bool:
    """Whether Tab moves focus between controls on this Mac.

    System Settings > Keyboard > Keyboard navigation, stored as bit 2 of
    AppleKeyboardUIMode, off by default. (Not the Accessibility pane's Full
    Keyboard Access, which is a separate feature.) Chrome's dialogs follow it.
    Measured on Chrome 154: with it off, the AXFocused write on Allow reads back
    false and Tab never reaches the button, so neither keyboard route can work;
    with it on, the focus write lands and Space grants.

    @returns True when Tab can reach buttons.
    """
    try:
        mode = NSUserDefaults.standardUserDefaults().integerForKey_("AppleKeyboardUIMode")
    except Exception:
        return False
    return bool(int(mode) & 2)


def _is_visible(element) -> bool:
    """Skip hidden/offscreen elements: a dismissed dialog lingers briefly in the
    tree and would otherwise be approved a second time."""
    if _attr(element, "AXHidden"):
        return False
    # A size of zero is the other tell-tale of a torn-down element.
    size = _ax_size(element)
    if size is not None and (size[0] <= 0 or size[1] <= 0):
        return False
    return True


class Engine:
    def __init__(self, observe: bool = False, poll_ms: int = POLL_MS_DEFAULT,
                 include_edge: bool = False, log_path: Path = LOG_PATH,
                 exit_with_parent: bool = False, diagnostics: bool = False) -> None:
        self.observe = observe
        self.poll_s = max(0.05, poll_ms / 1000.0)
        self.bundles = CHROME_BUNDLES + (EDGE_BUNDLES if include_edge else ())
        self.log_path = Path(log_path)
        self.approved = 0
        self.exit_with_parent = exit_with_parent
        self.diagnostics = diagnostics
        self._parent_pid = os.getppid()
        self._seen: dict[str, float] = {}    # dedupe key -> last-press wall clock
        self._audited: set[tuple] = set()    # sheet-candidate decisions already logged
        self._first_seen: dict[str, float] = {}  # dedupe key -> sweep clock when first seen
        self._next_diagnostic_at = 0.0

    # -------- logging: byte-for-byte the format the tray parses --------

    def log(self, message: str, level: str = "INFO") -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"
        line = f"{stamp} [{level}] {message}"
        print(line, flush=True)
        try:
            ensure_data_dir()
            # One generation, rolled at 1MB - an engine that runs for months
            # must not fill the disk. Matches watcher.ps1.
            if self.log_path.exists() and self.log_path.stat().st_size > 1_048_576:
                self.log_path.replace(self.log_path.with_name(self.log_path.name + ".1"))
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    # -------- finding Chrome, the dialog, and the button --------

    def workspace_chrome_pids(self) -> list[int]:
        """Read NSWorkspace's application list for diagnostic comparison."""
        out = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if (app.bundleIdentifier() or "") in self.bundles:
                out.append(app.processIdentifier())
        return out

    def chrome_pids(self) -> list[int]:
        """Query each bundle directly, bypassing NSWorkspace's stale app list."""
        out = []
        for bundle in self.bundles:
            apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
            out.extend(app.processIdentifier() for app in apps)
        return out

    def find_dialog_hosts(self, app_element, diag: list[str] | None = None) -> list:
        """Collect the consent sheet under each Chrome window.

        Chrome 152 titles the AXSheet. Chrome 153 leaves that title empty and
        puts the prompt on a heading inside the sheet. Either shape counts.
        One level of window children only, but *which*
        attribute exposes it there is not stable. A live probe against a real
        prompt found window.AXSheets returning fourteen entries, every one a
        stale/invalid ref (AXError -25202), while the actual sheet showed up
        as a plain entry in window.AXChildren instead. So both are checked,
        one level deep, no further - which is still nothing like the old
        depth-12 recursive walk (~150-160ms/pid, see --diagnostics): a
        window's AXChildren here is a handful of top-level regions (toolbar,
        tab strip, the sheet if any, the web area), not the page's own
        AX-exposed DOM, which only appears if you recurse *into* the web-area
        child - and this doesn't."""
        hits: list = []
        seen: set = set()
        windows = _attr(app_element, "AXWindows") or []
        if diag is not None:
            diag.append(f"windows={len(windows)}")
        for window in windows:
            if _is_consent_host(_element_label(window), False, _is_dialog_role(window)):
                self._add_host(window, hits, seen)
            candidates = (_attr(window, "AXSheets") or []) + (_attr(window, "AXChildren") or [])
            if diag is not None:
                diag.append(f"candidates={len(candidates)}")
            for candidate in candidates:
                label = _element_label(candidate)
                is_dialog = _is_dialog_role(candidate)
                has_heading = False
                # The heading walk is for the untitled shape only. A titled
                # dialog that is not the prompt (a JavaScript alert, say) is
                # rejected on its title; walking it would cost the same eight
                # children by five levels and change nothing.
                if is_dialog and label == "":
                    has_heading = _has_dialog_heading(candidate)
                accepted = _is_consent_host(label, has_heading, is_dialog)
                if is_dialog:
                    # Left in on purpose. Chrome 153's sheet has an empty title,
                    # so a miss here is otherwise invisible (hosts=0, no buttons).
                    # Once per distinct decision, not once per sweep: an alert
                    # left standing would otherwise write four lines a second
                    # for as long as it stood, and bury the lines that matter.
                    decision = (label, has_heading, accepted,
                                str(_attr(candidate, "AXRole")),
                                str(_attr(candidate, "AXSubrole")))
                    if decision not in self._audited:
                        if len(self._audited) >= AUDIT_MAX:
                            self._audited.clear()
                        self._audited.add(decision)
                        self.log(
                            f"sheet candidate label={label!r} heading={has_heading} "
                            f"accept={accepted} role={decision[3]} subrole={decision[4]}",
                            "AUDIT",
                        )
                if accepted:
                    self._add_host(candidate, hits, seen)
        return hits

    def _add_host(self, host, hits: list, seen: set) -> None:
        """Append host if its geometry signature hasn't already surfaced this
        sweep - a window and its sheet can't collide, but stay defensive."""
        sig = self._host_signature(host)
        if sig not in seen:
            seen.add(sig)
            hits.append(host)

    def find_approve_button(self, host, depth: int = 0):
        """The Allow button within one dialog subtree. Scoped to the dialog, never
        the whole app tree, so page-level Allow buttons are out of reach."""
        if depth > 6:
            return None
        if _attr(host, "AXRole") == "AXButton":
            label = _attr(host, "AXTitle") or _attr(host, "AXDescription") or ""
            if APPROVE_PATTERN.match(str(label).strip()):
                return host
        for child in _children(host):
            found = self.find_approve_button(child, depth + 1)
            if found is not None:
                return found
        return None

    def _button_labels(self, host, depth: int = 0, out=None) -> list[str]:
        if out is None:
            out = []
        if depth > 6:
            return out
        if _attr(host, "AXRole") == "AXButton":
            out.append(str(_attr(host, "AXTitle") or _attr(host, "AXDescription") or ""))
        for child in _children(host):
            self._button_labels(child, depth + 1, out)
        return out

    def _host_signature(self, host) -> str:
        """A stable identity for one on-screen dialog, used both to dedupe the
        several tree paths that surface it and to dedupe across sweeps. macOS AX
        has no GetRuntimeId(); Chrome sets no AXIdentifier on this alert (the probe
        confirmed None), so key on geometry, which the port doc anticipated.
        Object id() is useless here - every sweep rebuilds the app element and its
        refs, so id() changes each pass and defeats the dedupe entirely."""
        ident = _attr(host, "AXIdentifier")
        if ident:
            return f"id:{ident}"
        pos = _ax_point(host)
        size = _ax_size(host)
        if pos is not None:
            base = f"pos:{int(pos[0])},{int(pos[1])}"
            if size is not None:
                base += f";size:{int(size[0])},{int(size[1])}"
            return base
        return f"obj:{id(host)}"

    # Back-compat alias for the per-sweep dedupe call site.
    _dedupe_key = _host_signature

    def _press(self, button) -> str | None:
        """AXPress the button. Returns the action name on AX success, not Chrome accept.

        The error code stays in the log. Success here means AX took the action,
        not that Chrome dismissed the sheet.
        """
        try:
            err = AXUIElementPerformAction(button, "AXPress")
        except Exception as exc:
            self.log(f"  AXPress raised {exc!r}", "AUDIT")
            return None
        self.log(f"  AXPress err={err}", "AUDIT")
        if err == kAXErrorSuccess:
            return "AXPress"
        return None

    def _log_press_state(self, host, button, phase: str) -> None:
        """Record whether the pressed sheet still looks alive.

        A dismissed sheet's refs answer with -25202. Chrome 153 has also
        invalidated a ref while the dialog was still on screen, which is
        what a bare APPROVED line cannot tell apart. This line stays.

        @param host - The sheet that was pressed.
        @param button - The Allow button ref from that same sheet.
        @param phase - Which wait this snapshot follows.
        """
        host_alive = _ref_alive(host)
        button_alive = _ref_alive(button)
        visible = _is_visible(host) if host_alive is True else False
        self.log(
            f"  {phase} host_alive={host_alive} button_alive={button_alive} "
            f"visible={visible}",
            "AUDIT",
        )

    def _raise_host(self, host) -> None:
        """Best-effort AXRaise on the sheet and its parent window so the click lands."""
        try:
            AXUIElementPerformAction(host, "AXRaise")
        except Exception:
            pass
        parent = _attr(host, "AXParent")
        if parent is None:
            return
        try:
            AXUIElementPerformAction(parent, "AXRaise")
        except Exception:
            pass

    def _sheet_still_up(self, host, button) -> bool:
        """True while the sheet that was pressed - that AX node, not whatever
        now sits at its coordinates - is still on screen with its Allow button.

        Identity, not geometry. Chrome queues the consent prompt when several
        CDP clients connect at once and draws the next one at exactly the
        position and size of the one just dismissed, so re-scanning and
        matching a signature reported "still up" for a sheet that was gone,
        and a real approval became a FAILED line the tray's burst guard never
        counted. A dismissed sheet's refs go invalid instead (-25202 on any
        read, verified on Chrome 152), and only a node that is really gone
        does that. AXPress that Chrome ignored, or a teardown slower than
        VERIFY_WAIT_S, leaves the same refs valid and visible: the CGEvent case."""
        host_alive = _ref_alive(host)
        if host_alive is False or _ref_alive(button) is False:
            return False
        if host_alive is None:
            # No answer is not a dismissal: no [ACTION] on this pass. If the
            # sheet is really gone the next sweep simply finds nothing.
            return True
        return _is_visible(host)

    def _focus_button(self, button) -> bool:
        """Ask AX to move keyboard focus onto the Allow button.

        Setting AXFocused is a write, not an action, so Chrome's command
        dispatch is not involved - the thing that swallows AXPress. Whether
        Chrome honours it is logged either way.

        @param button - The Allow button.
        @returns True when the button reports itself focused afterwards.
        """
        try:
            err = AXUIElementSetAttributeValue(button, "AXFocused", True)
        except Exception as exc:
            self.log(f"  AXFocused set raised {exc!r}", "AUDIT")
            return False
        focused = _attr(button, "AXFocused")
        self.log(f"  AXFocused set err={err} readback={focused!r}", "AUDIT")
        return bool(focused)

    def _key(self, pid: int, keycode: int, name: str) -> None:
        """Post one keystroke to a process without moving the pointer.

        CGEventPostToPid delivers keyboard events but silently drops mouse
        events, because AppKit hit-tests mouse position against the window
        server's pointer state and this transport never updates it. Keyboard
        events carry no position, so they arrive - and they arrive at Chrome
        only, so they cannot land in whatever the user is typing in.

        @param pid - Target process.
        @param keycode - Carbon virtual keycode.
        @param name - Label for the log line.
        """
        for pressed in (True, False):
            event = CGEventCreateKeyboardEvent(None, keycode, pressed)
            if event is None:
                self.log(f"  {name} event creation failed", "AUDIT")
                return
            CGEventPostToPid(pid, event)
        self.log(f"  sent {name} to pid {pid}", "AUDIT")

    def _sheet_state(self, host, button) -> str:
        """The pressed sheet as the keyboard path needs to see it.

        'live': both refs answer and the sheet is on screen. 'gone': a ref
        answers -25202, which only a torn-down node does, or the sheet has
        left the screen. 'unknown': no answer (Chrome busy, AX unreachable),
        which proves nothing either way.

        _sheet_still_up is the negative form and says True for 'unknown',
        which is right for judging an approval and wrong for sending a key:
        a keystroke needs the positive form. 'Not proven gone' is not enough.

        @param host - The sheet that was pressed.
        @param button - Its Allow button.
        @returns 'live', 'gone' or 'unknown'.
        """
        host_alive = _ref_alive(host)
        button_alive = _ref_alive(button)
        if host_alive is False or button_alive is False:
            return "gone"
        if host_alive is None or button_alive is None:
            return "unknown"
        return "live" if _is_visible(host) else "gone"

    def _no_key(self, state: str, when: str, pressed: str) -> str | None:
        """What to report when the gate closed and no key was sent.

        A sheet proven gone was dismissed by the AXPress already sent, the
        slow-teardown case seen under multi-client load: that is the approval,
        reported as such so the tray's burst guard counts it, not as a FAILED
        line for a sheet nobody can find. One that stopped answering proves
        nothing and is retried next sweep.

        @param state - 'gone' or 'unknown' from _sheet_state.
        @param when - Where in the keyboard path the gate closed, for the log.
        @param pressed - How the sheet was pressed before the keyboard path.
        @returns pressed for a sheet proven gone, None otherwise.
        """
        if state == "gone":
            self.log(f"  sheet gone {when} - late {pressed} teardown, no key sent", "AUDIT")
            return pressed
        self.log(f"  sheet not answering {when} - no key sent, retrying next sweep", "AUDIT")
        return None

    def _key_approve(self, pid: int, host, button, pressed: str = "AXPress") -> str | None:
        """Activate Allow with the keyboard. No pointer, no app activation.

        Two ways in. If Chrome honours an AXFocused write, Space on the
        already-focused button is one keystroke. If it does not, Tab is walked
        until the button reports focus, which also tells the log how many stops
        away it is in case Chrome adds a control to the sheet.

        Every keystroke is gated on the sheet that was pressed still being the
        live, visible node it was: before the focus write, before each Tab, and
        again immediately before Space, which must also find Allow still
        focused. A key posted after the sheet has gone lands on whatever Chrome
        focuses next - the page, or the default button of a queued successor -
        so the gate closing means no more keys, whatever else it means.

        @param pid - Chrome process showing the sheet.
        @param host - The sheet, used to gate each key and to verify dismissal.
        @param button - The Allow button.
        @param pressed - How the sheet was pressed before this, for the log and
            for the report when that press turns out to have worked late.
        @returns 'Space' once the sheet is verified gone after the keystroke,
            pressed when it went before any key was needed, None while it
            still stands or stopped answering.
        """
        state = self._sheet_state(host, button)
        if state != "live":
            return self._no_key(state, "before the focus write", pressed)
        if not self._focus_button(button):
            if not _keyboard_navigation():
                self.log("  Tab walk skipped: Keyboard navigation (System Settings > Keyboard) "
                         "is off, so Tab cannot reach a button in a Chrome dialog", "AUDIT")
                return None
            for stop in range(MAX_TAB_STOPS):
                state = self._sheet_state(host, button)
                if state != "live":
                    return self._no_key(state, f"after {stop} Tab(s)", pressed)
                self._key(pid, VK_TAB, "Tab")
                time.sleep(0.1)
                if _attr(button, "AXFocused"):
                    self.log(f"  Allow focused after {stop + 1} Tab(s)", "AUDIT")
                    break
            else:
                self.log(f"  Allow never took focus in {MAX_TAB_STOPS} Tabs", "AUDIT")
                return None
        state = self._sheet_state(host, button)
        if state != "live":
            return self._no_key(state, "before Space", pressed)
        if not _attr(button, "AXFocused"):
            self.log("  Allow not focused at the moment of Space - not sending", "AUDIT")
            return None
        self._key(pid, VK_SPACE, "Space")
        time.sleep(VERIFY_WAIT_S)
        self._log_press_state(host, button, "after Space")
        if self._sheet_still_up(host, button):
            return None
        return "Space"

    def _approve(self, pid: int, host, button) -> str | None:
        """Dismiss the consent sheet. Returns how it fell, only once the sheet is
        verified gone. Does not move the pointer or activate Chrome.

        One AXPress, which is enough on Chrome 151. On Chrome 153 that returns
        success without running Allow, so the keyboard follows - through a gate
        that re-reads the pressed refs before every key, because the AXPress
        can also take effect late (slow teardown under load) and a keystroke
        after that has nowhere safe to land. A second AXPress is never sent:
        it removes the sheet and leaves the socket unapproved, which reads as
        success and is not. A sheet that survives both is retried next sweep.
        """
        seen_at = time.monotonic()
        self._raise_host(host)
        ax_ok = self._press(button) is not None
        time.sleep(VERIFY_WAIT_S)
        self._log_press_state(host, button, "after first AXPress")
        if not self._sheet_still_up(host, button):
            return "AXPress" if ax_ok else "AXRaise"

        guard_left = ACTIVATION_GUARD_S - (time.monotonic() - seen_at)
        if guard_left > 0:
            time.sleep(guard_left)
        return self._key_approve(pid, host, button, "AXPress" if ax_ok else "AXRaise")

    def _element_summary(self, element) -> str:
        """Return the AX identity and geometry used to audit a pending click."""
        attributes = {
            "title": _attr(element, "AXTitle"),
            "description": _attr(element, "AXDescription"),
            "role": _attr(element, "AXRole"),
            "subrole": _attr(element, "AXSubrole"),
            "identifier": _attr(element, "AXIdentifier"),
            "enabled": _attr(element, "AXEnabled"),
            "position": _ax_point(element),
            "size": _ax_size(element),
        }
        return " ".join(f"{key}={value!r}" for key, value in attributes.items())

    # -------- one sweep, and the loop --------

    def sweep(self) -> None:
        now = time.time()
        is_diagnostic_sweep = self.diagnostics and now >= self._next_diagnostic_at
        if is_diagnostic_sweep:
            self._next_diagnostic_at = now + 5
            self.log(f"diagnostic sweep start trusted={is_trusted(prompt=False)} "
                     f"parent={os.getppid()} expected_parent={self._parent_pid}", "DIAG")

        direct_started = time.monotonic()
        chrome_pids = self.chrome_pids()
        if is_diagnostic_sweep:
            direct_ms = int((time.monotonic() - direct_started) * 1000)
            workspace_started = time.monotonic()
            workspace_pids = self.workspace_chrome_pids()
            workspace_ms = int((time.monotonic() - workspace_started) * 1000)
            self.log(f"diagnostic pids direct={chrome_pids} ({direct_ms}ms) "
                     f"workspace={workspace_pids} ({workspace_ms}ms)", "DIAG")

        for pid in chrome_pids:
            app = AXUIElementCreateApplication(pid)
            scan_started = time.monotonic()
            diag: list[str] = [] if is_diagnostic_sweep else None
            hosts = self.find_dialog_hosts(app, diag=diag)
            if is_diagnostic_sweep:
                scan_ms = int((time.monotonic() - scan_started) * 1000)
                self.log(f"diagnostic AX pid={pid} hosts={len(hosts)} "
                         f"({scan_ms}ms) {' '.join(diag)}", "DIAG")
            for host in hosts:
                try:
                    if not _is_visible(host):
                        self.log("  sheet matched but not visible - not pressing", "AUDIT")
                        continue
                    labels = [b for b in self._button_labels(host) if b]
                    # A dismissed dialog lingers in the tree for a beat with its
                    # buttons already gone. Matching its title but finding no
                    # buttons is that teardown state, not a real prompt - skip it
                    # quietly so it neither logs noise nor burns a dedupe slot.
                    if not labels:
                        self.log("  sheet matched but no button labels - not pressing", "AUDIT")
                        continue

                    key = self._dedupe_key(host)
                    # A sheet is pressed only once it has stood for the
                    # activation guard. Chrome 154, three clients queued: every
                    # press made within a poll of the sheet appearing returned
                    # success and did nothing, and every press made a second
                    # later granted. A queued successor is drawn the instant
                    # its predecessor goes and shares this key, so the clock
                    # is reset on each verified approval below.
                    if not self.observe:
                        # Stamped here, not from the sweep's `now`: the AX walk
                        # above can take 100ms or more, and the guard is
                        # measured from when the sheet was actually seen.
                        first = self._first_seen.get(key)
                        if first is None:
                            self._first_seen[key] = time.time()
                            self.log(f"  sheet first seen key={key} - pressing once the "
                                     f"activation guard has passed", "AUDIT")
                            continue
                        if time.time() - first < ACTIVATION_GUARD_S:
                            continue
                    last = self._seen.get(key)
                    if last is not None and now - last < DEDUPE_SECONDS:
                        self.log(f"  sheet deduped key={key} - not pressing", "AUDIT")
                        continue
                    self._seen[key] = now

                    self.log(f"dialog found (pid={pid}) buttons: "
                             + ", ".join(f"'{b}'" for b in labels))

                    if self.observe:
                        self.log("  observe mode - not clicking", "OBSERVE")
                        continue

                    button = self.find_approve_button(host)
                    if button is None:
                        # Real buttons, but none is Allow/Approve (e.g. only "Turn
                        # off in settings"/"Cancel"). Leave it be and say why.
                        self.log(f"  no button matched /{APPROVE_PATTERN.pattern}/ - left alone", "WARN")
                        continue

                    self.log(f"approval pending host={key} siblings={labels!r} "
                             f"target={self._element_summary(button)}", "AUDIT")
                    how = self._approve(pid, host, button)
                    # The dedupe slot has done its job either way. On success
                    # the pressed sheet is verified gone, and a queued prompt
                    # Chrome draws at the same coordinates carries the same
                    # key: it must be served next sweep, not after
                    # DEDUPE_SECONDS. On failure a leftover sheet is retried
                    # next sweep instead of sitting out the window.
                    self._seen.pop(key, None)
                    if how:
                        self._first_seen.pop(key, None)
                        self.approved += 1
                        self.log(f"  APPROVED via {how}", "ACTION")
                        self.log(f"  total approved this session: {self.approved}")
                    else:
                        self.log("  FAILED: sheet not verified gone after AXPress and the keyboard path - retrying next sweep", "ERROR")
                except Exception as exc:
                    self.log(f"  host error: {exc!r}", "ERROR")

        # Bound the dedupe memory.
        if len(self._seen) > DEDUPE_MAX:
            cutoff = now - 300
            self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        if len(self._first_seen) > DEDUPE_MAX:
            cutoff = now - 300
            self._first_seen = {k: v for k, v in self._first_seen.items() if v >= cutoff}
        if is_diagnostic_sweep:
            self.log("diagnostic sweep complete", "DIAG")

    def run(self) -> int:
        if not is_trusted(prompt=False):
            self.log("NOT trusted for Accessibility - every AX read will be empty. "
                     "Grant it in System Settings > Privacy & Security > Accessibility, "
                     "then restart.", "ERROR")
            # Keep running: the grant can be given while we are up, and the next
            # sweep will start seeing elements. Better than exiting and looking dead.
        self.log(f"engine started (observe={self.observe}, interval={int(self.poll_s * 1000)}ms, "
                 f"bundles={len(self.bundles)}, pid={os.getpid()})")
        while True:
            if self.exit_with_parent and os.getppid() != self._parent_pid:
                # The tray is gone (quit, crashed, killed, logged out) and we have
                # been reparented. Exiting matters more here than it looks: every
                # safety limit - the burst guard, the arm timer, the pause - lives
                # in the tray. An engine that outlives it keeps approving prompts
                # with nothing watching the rate and no way to turn it off short
                # of finding the pid.
                self.log("parent process is gone - exiting rather than approving "
                         "unsupervised", "WARN")
                return 0
            try:
                # The polling loop does not enter AppKit's event loop.
                with objc.autorelease_pool():
                    self.sweep()
            except Exception as exc:
                self.log(f"loop error: {exc!r}", "ERROR")
            time.sleep(self.poll_s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Yes, Dev macOS engine")
    ap.add_argument("--observe", action="store_true", help="log dialogs, never click")
    ap.add_argument("--once", action="store_true", help="one sweep then exit")
    ap.add_argument("--interval-ms", type=int, default=POLL_MS_DEFAULT)
    ap.add_argument("--include-edge", action="store_true")
    ap.add_argument("--log-path", default=str(LOG_PATH))
    ap.add_argument("--exit-with-parent", action="store_true",
                    help="stop as soon as the launching process goes away; the "
                         "tray passes this so a dead tray cannot leave an engine "
                         "approving prompts unsupervised")
    ap.add_argument("--diagnostics", action="store_true",
                    help="log trust, process discovery, and AX sweep timing every 5s")
    args = ap.parse_args(argv)

    engine = Engine(observe=args.observe, poll_ms=args.interval_ms,
                    include_edge=args.include_edge, log_path=Path(args.log_path),
                    exit_with_parent=args.exit_with_parent,
                    diagnostics=args.diagnostics)
    if args.once:
        if not is_trusted():
            engine.log("NOT trusted for Accessibility - results will be empty.", "ERROR")
        engine.sweep()
        return 0
    return engine.run()


if __name__ == "__main__":
    raise SystemExit(main())
