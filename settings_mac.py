"""Native, reusable settings window owned by the menu-bar app's main thread."""
from __future__ import annotations

from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSBezelStyleRounded, NSButton,
    NSButtonTypeSwitch, NSColor, NSFont, NSMakeRect, NSOpenPanel, NSPasteboard,
    NSPasteboardTypeString, NSPopUpButton, NSTabView, NSTabViewItem, NSTextField,
    NSView, NSWindow, NSWindowStyleMaskClosable, NSWindowStyleMaskTitled,
)
from Foundation import NSObject, NSURL
from pathlib import Path

import platform_mac
from settings_model import validate


class _YesDevSettingsActions(NSObject):
    controller = None

    def save_(self, sender):
        self.controller.save()

    def cancel_(self, sender):
        self.controller.window.orderOut_(None)

    def browse_(self, sender):
        self.controller.browse()

    def copyAddress_(self, sender):
        self.controller.copy_address()

    def focusChanged_(self, sender):
        self.controller.focus_changed()

    def accessibility_(self, sender):
        self.controller.owner.on_grant_accessibility(None)
        self.controller.refresh_status()


class SettingsWindow:
    def __init__(self, owner):
        self.owner = owner
        self.actions = _YesDevSettingsActions.alloc().init()
        self.actions.controller = self
        self.fields = {}
        self.choices = {}
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 680, 620), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False)
        self.window.setTitle_("Yes, Dev Settings")
        self.window.setReleasedWhenClosed_(False)
        self.window.center()
        content = self.window.contentView()
        self.label(content, "Yes, Dev", 28, 566, 620, 30, size=24, bold=True)
        self.label(content, "Approvals, focus, and connections", 29, 540, 620, 22, secondary=True)
        self.button(content, "Accessibility…", "accessibility:", 499, 571, 152, 30)
        self.permission = self.label(content, "", 353, 540, 298, 22, secondary=True)
        tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(20, 101, 640, 420))
        content.addSubview_(tabs)
        general = self.tab(tabs, "General")
        focus = self.tab(tabs, "Focus & connection")
        self.checkbox(general, "enabled", "Automatically approve debugging requests", 22, 345)
        self.checkbox(general, "observe_only", "Observe only — record requests without approving", 22, 312)
        self.checkbox(general, "include_edge", "Also approve Microsoft Edge requests", 22, 279)
        self.checkbox(general, "start_at_login", "Start Yes, Dev when I log in", 22, 246)
        self.popup(general, "notify_style", "Approval notice", [
            ("Floating puffs", "puffs"), ("Toast card", "toast"), ("Silent", "none")], 208)
        self.number(general, "poll_ms", "Check interval", "milliseconds", 169)
        self.number(general, "arm_minutes", "Stay on for", "minutes · 0 means until turned off", 131)
        self.number(general, "burst_limit", "Pause after", "approvals / minute · 0 disables", 93)
        self.popup(general, "burst_action", "When a burst occurs", [
            ("Ask me first (5 seconds)", "ask"), ("Pause automatically", "stop")], 55)
        self.checkbox(general, "diagnostics", "Detailed diagnostic logging", 22, 16)

        self.popup(focus, "focus_mode", "Focus behavior", [
            ("Off", "off"), ("Quiet focus", "standard"),
            ("Fast focus via local relay (experimental)", "relay")], 340,
            action="focusChanged:")
        self.label(focus, "Quiet focus returns you to your previous app after Chrome opens a prompt.\n"
                   "Fast focus shortens the interruption for tools using the address below.",
                   22, 277, 574, 52, secondary=True)
        self.label(focus, "Chrome data folder", 22, 244, 570, 22, bold=True)
        path = self.textfield(focus, "relay_profile", 22, 209, 467, 26)
        path.setToolTip_("Chrome's data folder contains Local State and DevToolsActivePort, not the Default subfolder.")
        self.browse_button = self.button(focus, "Choose…", "browse:", 498, 207, 100, 30)
        self.label(focus, "Choose the folder containing Chrome’s Local State file.",
                   22, 180, 576, 22, secondary=True)
        self.number(focus, "relay_port", "Local port", "default: 9333", 144)
        self.checkbox(focus, "relay_hold", "Keep one Chrome connection open, so Chrome asks once per launch", 22, 110)
        self.fields["relay_hold"].setToolTip_(
            "Chrome asks when you open it, while you are already in it, instead of when an agent "
            "first connects. Clients take turns on that connection; a second client at the same "
            "time gets its own connection and its own prompt. Chrome shows its automation banner "
            "for as long as the connection is held.")
        self.label(focus, "Saved connection address", 22, 80, 550, 22, bold=True)
        self.address = self.label(focus, "", 22, 49, 430, 26)
        self.address.setSelectable_(True)
        self.copy_button = self.button(focus, "Copy address", "copyAddress:", 454, 46, 144, 30)
        self.relay_status = self.label(focus, "", 22, 4, 577, 40, secondary=True)

        self.error = self.label(content, "", 29, 53, 440, 44)
        self.error.setTextColor_(NSColor.systemRedColor())
        cancel = self.button(content, "Cancel", "cancel:", 465, 26, 90, 32)
        cancel.setKeyEquivalent_("\x1b")
        save = self.button(content, "Save", "save:", 561, 26, 90, 32)
        save.setKeyEquivalent_("\r")

    @staticmethod
    def tab(tabs, title):
        item = NSTabViewItem.alloc().initWithIdentifier_(title)
        item.setLabel_(title)
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 615, 382))
        item.setView_(view)
        tabs.addTabViewItem_(item)
        return view

    @staticmethod
    def label(parent, text, x, y, w, h, size=13, bold=False, secondary=False):
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        field.setStringValue_(text)
        field.setEditable_(False)
        field.setSelectable_(False)
        field.setBordered_(False)
        field.setDrawsBackground_(False)
        field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        if secondary:
            field.setTextColor_(NSColor.secondaryLabelColor())
        parent.addSubview_(field)
        return field

    def button(self, parent, title, action, x, y, w, h):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        button.setTitle_(title)
        button.setBezelStyle_(NSBezelStyleRounded)
        button.setTarget_(self.actions)
        button.setAction_(action)
        parent.addSubview_(button)
        return button

    def checkbox(self, parent, key, title, x, y):
        button = self.button(parent, title, None, x, y, 578, 26)
        button.setButtonType_(NSButtonTypeSwitch)
        self.fields[key] = button

    def textfield(self, parent, key, x, y, w, h):
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        field.setAccessibilityLabel_(key.replace("_", " ").capitalize())
        parent.addSubview_(field)
        self.fields[key] = field
        return field

    def number(self, parent, key, title, suffix, y):
        self.label(parent, title, 22, y+2, 156, 22)
        self.textfield(parent, key, 186, y, 86, 26)
        self.label(parent, suffix, 285, y+2, 320, 22, secondary=True)

    def popup(self, parent, key, title, choices, y, action=None):
        self.label(parent, title, 22, y+3, 164, 22)
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(184, y, 414, 28), False)
        popup.addItemsWithTitles_([label for label, _ in choices])
        popup.setAccessibilityLabel_(title)
        if action:
            popup.setTarget_(self.actions)
            popup.setAction_(action)
        parent.addSubview_(popup)
        self.fields[key] = popup
        self.choices[key] = [value for _, value in choices]

    def show(self):
        if not self.window.isVisible():
            values = dict(self.owner.cfg)
            values["start_at_login"] = platform_mac.autostart_enabled()
            values["focus_mode"] = "relay" if values["relay_enabled"] else "standard" if values["quiet_focus"] else "off"
            for key, field in self.fields.items():
                if key in self.choices:
                    field.selectItemAtIndex_(self.choices[key].index(values[key]))
                elif isinstance(field, NSButton):
                    field.setState_(int(values[key]))
                else:
                    field.setStringValue_(str(values[key]))
            self.error.setStringValue_("")
        self.focus_changed()
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def focus_changed(self):
        active = self.choices["focus_mode"][self.fields["focus_mode"].indexOfSelectedItem()] == "relay"
        for field in (self.fields["relay_profile"], self.fields["relay_port"], self.fields["relay_hold"],
                      self.browse_button, self.copy_button):
            field.setEnabled_(active)
        self.refresh_status()

    def refresh_status(self):
        # Use the saved port: Copy address must refer to a running/saved service,
        # not a field whose changes the user has not applied yet.
        self.address.setStringValue_(f"http://127.0.0.1:{self.owner.cfg['relay_port']}")
        self.relay_status.setStringValue_(self.owner.relay_status_text())
        self.permission.setStringValue_("Accessibility: granted" if platform_mac.is_trusted()
                                       else "Accessibility permission required")

    def save(self):
        values = dict(self.owner.cfg)
        for key, field in self.fields.items():
            if key in self.choices:
                values[key] = self.choices[key][field.indexOfSelectedItem()]
            elif isinstance(field, NSButton):
                values[key] = bool(field.state())
            else:
                values[key] = str(field.stringValue())
        mode = values.pop("focus_mode")
        login = values.pop("start_at_login")
        values["quiet_focus"] = mode != "off"
        values["relay_enabled"] = mode == "relay"
        try:
            values = validate(values)
            self.owner.apply_settings(values, login)
        except Exception as exc:
            self.error.setStringValue_(str(exc))
            return
        self.window.orderOut_(None)

    def browse(self):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setCanCreateDirectories_(False)
        panel.setPrompt_("Choose folder")
        current = Path(str(self.fields["relay_profile"].stringValue())).expanduser()
        if current.is_dir():
            panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(current)))
        panel.setMessage_("Choose Chrome’s data folder (the folder containing Local State).")
        if panel.runModal() == 1:
            self.fields["relay_profile"].setStringValue_(str(panel.URL().path()))

    def copy_address(self):
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(str(self.address.stringValue()), NSPasteboardTypeString)
