"""Run the same focus guard used by the relay from the comparison harness."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from early_focus_guard_mac import ControlServer, EarlyGuard, main  # noqa: F401

if __name__ == "__main__":
    main()
