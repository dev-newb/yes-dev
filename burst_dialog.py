"""The burst-guard confirmation dialog.

Runs as its own process for the same reason the overlay does: Tk only paints
from the main thread, and the tray already owns the parent's. Prints the
choice - "stop" or "allow_hour" - on stdout. Deciding nothing means stop,
because that is the safe answer to a burst you were not expecting.
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import font as tkfont

LIMIT_SECONDS = 5.0
BG = "#1f2430"
FG = "#e8ecf4"
MUTED = "#a7b0c0"
ACCENT = "#d9534f"


def main() -> int:
    count = sys.argv[1] if len(sys.argv) > 1 else "?"
    app = sys.argv[2] if len(sys.argv) > 2 else "Yes, Dev"

    root = tk.Tk()
    root.title(app)
    root.configure(bg=BG)
    root.resizable(False, False)
    root.attributes("-topmost", True)

    w, h = 460, 220
    root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}"
                  f"+{(root.winfo_screenheight() - h) // 3}")

    choice = {"value": "stop"}
    bold = tkfont.nametofont("TkDefaultFont").copy()
    bold.configure(size=11, weight="bold")

    tk.Label(root, text=f"{count} approvals in the last minute",
             bg=BG, fg=FG, font=bold).pack(anchor="w", padx=20, pady=(18, 6))
    tk.Label(root,
             text=(f"{app} is approving Chrome debugging requests much faster than\n"
                   "usual. If you were not expecting this many, something other than\n"
                   "your own tools may be asking - stopping is recommended."),
             bg=BG, fg=MUTED, justify="left").pack(anchor="w", padx=20)

    countdown = tk.Label(root, text="", bg=BG, fg=ACCENT)
    countdown.pack(anchor="w", padx=20, pady=(12, 4))

    track = tk.Canvas(root, height=4, bg="#39404f", highlightthickness=0)
    track.pack(fill="x", padx=20)
    bar = track.create_rectangle(0, 0, 0, 4, fill=ACCENT, width=0)

    def pick(value: str) -> None:
        choice["value"] = value
        root.destroy()

    row = tk.Frame(root, bg=BG)
    row.pack(fill="x", padx=20, pady=16)
    tk.Button(row, text="Allow for one hour", command=lambda: pick("allow_hour"),
              relief="flat", bg="#39404f", fg=FG, activebackground="#495061",
              padx=14, pady=6).pack(side="right", padx=(8, 0))
    stop = tk.Button(row, text=f"Stop {app}", command=lambda: pick("stop"),
                     relief="flat", bg=ACCENT, fg="white", padx=14, pady=6)
    stop.pack(side="right")

    remaining = [LIMIT_SECONDS]

    def tick() -> None:
        remaining[0] -= 0.1
        if remaining[0] <= 0:
            pick("stop")
            return
        countdown.config(text=f"Stopping automatically in {remaining[0]:.0f}s")
        frac = remaining[0] / LIMIT_SECONDS
        track.coords(bar, 0, 0, track.winfo_width() * frac, 4)
        root.after(100, tick)

    root.after(100, tick)
    root.lift()
    root.focus_force()
    stop.focus_set()
    root.protocol("WM_DELETE_WINDOW", lambda: pick("stop"))
    root.mainloop()

    print(choice["value"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
