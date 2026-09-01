"""Regenerate the Windows documentation art from the live renderer.

Rather than lining clouds up in a row, this replays what actually happens above
the tray: clouds are born near the bottom right, rise at their own speed, drift
a little sideways, and fade as they age. Freezing that mid-flight gives the
scattered arrangement you see in use.

Everything comes from puffs.py - the same shapes, the same motion ranges - so
the art cannot drift from the app. Seeded, so re-running does not churn the repo.

    python docs/make_art.py

Its macOS counterpart is docs/make_art_mac.py, and the two are deliberately
built the same way and to the same size, so the pair sits level side by side in
the README and any difference you see between them is a real difference in the
app rather than an artefact of how the picture was made.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import puffs

DOCS = Path(__file__).resolve().parent

OUT_W, OUT_H = 600, 599
# The slice of desktop the frame shows, in unscaled pixels. Sized so the burst
# fills it: clouds spawn 60-210px in from the right edge and rise up to ~220px,
# and a much wider view would leave most of the picture as empty desktop. The
# macOS generator uses the same number for the same reason.
VIEW = 250
CLOUDS = 14
SEED = 5

TASKBAR = 40             # px at 100% scale
GROUND = (16, 18, 22)
BAR = (28, 30, 36)
TRAY_GREEN = (46, 160, 67)


def _frame(with_chrome: bool):
    """One frozen frame of a burst. `with_chrome` draws the ground and taskbar."""
    rng = random.Random(SEED)
    W, H = OUT_W * 2, OUT_H * 2                  # render at 2x, downscale at the end
    px = W / VIEW                                # render pixels per desktop pixel
    canvas = Image.new("RGBA", (W, H), GROUND + (255,) if with_chrome else (0, 0, 0, 0))

    # The work area is the desktop minus the taskbar - what SPI_GETWORKAREA
    # returns, and what puffs.py anchors a cloud to.
    work_bottom = VIEW - TASKBAR

    for i in range(CLOUDS):
        size = rng.randint(*puffs.SIZE_RANGE)
        alpha0 = rng.uniform(*puffs.ALPHA_RANGE)
        life = rng.uniform(*puffs.LIFE_RANGE)
        rise = rng.uniform(*puffs.RISE_RANGE)
        drift = rng.uniform(*puffs.DRIFT_RANGE)

        # Spread the ages so one frame catches the whole arc: fresh and bright
        # down near the tray, higher and fainter on the way out. The jitter is
        # wide on purpose - stepping the age straight off the index lines the
        # clouds up in a neat diagonal, which a real burst never does.
        step = (i + rng.uniform(-0.85, 0.85)) / max(1, CLOUDS - 1)
        age = life * min(max(0.04 + 0.86 * step, 0.03), 0.88)

        # Windows: the tray is at the bottom, so a cloud rises away from it and
        # y decreases as it ages.
        y = work_bottom - rng.randint(*puffs.SPAWN_Y_UP) - rise * age
        x = VIEW - rng.randint(*puffs.SPAWN_X_BACK) + drift * age
        # A floor on the opacity: the last frames of a real fade are too faint to
        # read as a picture, though they are correct on screen.
        alpha = max(alpha0 * (1.0 - age / life), 0.30)

        random.seed(rng.random())                # vary the lobes, reproducibly
        art = puffs._render_cloud(int(size * px), premultiply=False)
        faded = art.copy()
        faded.putalpha(art.split()[3].point(lambda v: int(v * alpha)))
        canvas.alpha_composite(faded, (int(x * px), int(y * px)))

    if with_chrome:
        _taskbar(canvas, px)
    return canvas


def _taskbar(canvas, px: float) -> None:
    """The taskbar, drawn last so a cloud low enough to overlap it passes behind
    rather than over - which is what happens on screen, since the clouds are
    anchored to the work area and rise away from the bar."""
    W, H = canvas.width, canvas.height
    top = int((VIEW - TASKBAR) * px)
    canvas.alpha_composite(Image.new("RGBA", (W, H - top), BAR + (255,)), (0, top))
    d = ImageDraw.Draw(canvas)

    cy = top + TASKBAR * px / 2
    # The tray icon itself, so the picture shows what the clouds are rising out
    # of. Proportions mirror make_image() in yes_dev.pyw, which draws it on a
    # 64px canvas: a filled circle inset by 2, and a check through (18,33) ->
    # (28,44) -> (46,20) at width 8.
    r = 9 * px
    cx = W - 118 * px
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=TRAY_GREEN + (255,))
    s = (2 * r) / 64.0
    d.line([(cx - r + 18 * s, cy - r + 33 * s), (cx - r + 28 * s, cy - r + 44 * s),
            (cx - r + 46 * s, cy - r + 20 * s)],
           fill=(255, 255, 255, 255), width=max(1, int(8 * s)), joint="curve")

    # A few neighbouring notification-area icons, for scale.
    for i, w in enumerate((13, 9, 11)):
        ex = W - 88 * px + i * 20 * px
        d.rounded_rectangle([ex, cy - 6 * px, ex + w * px, cy + 6 * px],
                            radius=2 * px, fill=(150, 158, 172, 255))


def main() -> int:
    random.seed(SEED)
    hero = puffs._render_cloud(320, premultiply=False)
    hero.save(DOCS / "cloud.png")
    print("cloud.png         ", hero.size)

    art = _frame(with_chrome=False).resize((OUT_W, OUT_H), Image.LANCZOS)
    art.save(DOCS / "clouds.png")
    print("clouds.png        ", art.size, "(transparent)")

    preview = _frame(with_chrome=True).resize((OUT_W, OUT_H), Image.LANCZOS)
    preview.convert("RGB").save(DOCS / "clouds-preview.png")
    print("clouds-preview.png", preview.size, "(dark ground + taskbar)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
