"""Compatibility import for the early-focus experiment."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from focus_protocol import AppIdentity, ArmGate, Pending  # noqa: F401
