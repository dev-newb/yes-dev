"""Validated macOS settings and atomic persistence, independent of AppKit."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

DEFAULTS = {
    "enabled": True, "observe_only": False, "poll_ms": 250,
    "include_edge": False, "notify_style": "puffs", "burst_limit": 60,
    "burst_action": "ask", "arm_minutes": 0, "quiet_focus": False,
    "diagnostics": False, "relay_enabled": False, "relay_port": 9333,
    "relay_profile": str(Path.home() / "Library/Application Support/Google/Chrome"),
}
NUMBERS = {
    "poll_ms": (100, 5000, "Check interval"),
    "burst_limit": (0, 10000, "Approvals per minute"),
    "arm_minutes": (0, 10080, "Stay on for"),
    "relay_port": (1024, 65535, "Relay port"),
}
ENUMS = {"notify_style": {"puffs", "toast", "none"}, "burst_action": {"ask", "stop"}}
BOOLS = {key for key, value in DEFAULTS.items() if type(value) is bool}


def validate(values):
    result = dict(values)
    for key in BOOLS:
        if type(result.get(key)) is not bool:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} must be on or off.")
    for key, (low, high, label) in NUMBERS.items():
        raw = str(result.get(key, "")).strip()
        if not raw.isdecimal() or len(raw) > 8 or not low <= int(raw) <= high:
            raise ValueError(f"{label} must be a whole number from {low} to {high}.")
        result[key] = int(raw)
    for key, allowed in ENUMS.items():
        if result.get(key) not in allowed:
            raise ValueError(f"Choose a valid {key.replace('_', ' ')}.")
    profile = str(result.get("relay_profile", "")).strip()
    if not profile:
        raise ValueError("Choose a Chrome data folder.")
    result["relay_profile"] = str(Path(profile).expanduser().resolve())
    if result["relay_enabled"]:
        if not Path(result["relay_profile"]).is_dir():
            raise ValueError("The Chrome data folder does not exist. Choose the folder containing Local State.")
        if not result["quiet_focus"]:
            raise ValueError("Fast focus requires Quiet focus.")
    return result


def normalize(saved):
    """Repair individual invalid values while retaining unknown config keys."""
    if not isinstance(saved, dict):
        saved = {}
    saved = dict(saved)
    if "notify_style" not in saved and "notify_on_approve" in saved:
        saved["notify_style"] = "puffs" if saved["notify_on_approve"] else "none"
    if "burst_limit" not in saved and "burst_guard" in saved:
        saved["burst_limit"] = DEFAULTS["burst_limit"] if saved["burst_guard"] else 0
    result = {**DEFAULTS, **saved}
    result.pop("notify_on_approve", None)
    result.pop("burst_guard", None)
    for key in BOOLS:
        if type(result[key]) is not bool:
            result[key] = DEFAULTS[key]
    for key, (low, high, _) in NUMBERS.items():
        raw = str(result[key]).strip()
        result[key] = int(raw) if raw.isdecimal() and len(raw) <= 8 and low <= int(raw) <= high else DEFAULTS[key]
    for key, allowed in ENUMS.items():
        if not isinstance(result[key], str) or result[key] not in allowed:
            result[key] = DEFAULTS[key]
    if not isinstance(result["relay_profile"], str) or not result["relay_profile"].strip():
        result["relay_profile"] = DEFAULTS["relay_profile"]
    if result["relay_enabled"]:
        result["quiet_focus"] = True
    return result


def save_atomic(path, values):
    """Never truncate a working config when serialization or replacement fails."""
    path = Path(path)
    serialized = json.dumps(values, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
