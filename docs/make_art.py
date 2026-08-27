"""Regenerate the documentation art from the live renderer.

Rather than lining clouds up in a row, this replays what actually happens above
the tray: clouds are born near the bottom right, rise at their own speed, drift
a little sideways, and fade as they age. Freezing that mid-flight gives the
scattered arrangement you see in use.

Everything comes from puffs.py - the same shapes, the same motion ranges - so
the art cannot drift from the app. Seeded, so re-running does not churn the repo.

    python docs/make_art.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import puffs

DOCS = Path(__file__).resolve().parent
SCALE = 3          # render big, so the PNG stays crisp in a README
CLOUDS = 10
SEED = 5

# The slice of desktop the clouds live in: in from the right edge, up from the
# taskbar, plus room for the oldest ones to have climbed.
VIRTUAL_W = 300
VIRTUAL_H = 300


def in_flight() -> Image.Image:
    """One frozen frame of a burst, on transparency."""
    rng = random.Random(SEED)
    canvas = Image.new("RGBA", (VIRTUAL_W * SCALE, VIRTUAL_H * SCALE), (0, 0, 0, 0))

    for i in range(CLOUDS):
        size = rng.randint(*puffs.SIZE_RANGE)
        alpha0 = rng.uniform(*puffs.ALPHA_RANGE)
        life = rng.uniform(*puffs.LIFE_RANGE)
        rise = rng.uniform(*puffs.RISE_RANGE)
        drift = rng.uniform(*puffs.DRIFT_RANGE)
        # Spread the ages so the frame catches the whole arc at once: fresh and
        # bright near the tray, higher and fainter on the way out. Ages stop
        # short of the very end, and opacity has a floor, because the last few
        # frames of a real fade are too faint to read as a picture.
        age = life * (0.05 + 0.62 * (i / max(1, CLOUDS - 1))) * rng.uniform(0.85, 1.1)
        age = min(age, life * 0.70)

        x = VIRTUAL_W - rng.randint(*puffs.SPAWN_X_BACK) + drift * age
        y = VIRTUAL_H - rng.randint(*puffs.SPAWN_Y_UP) - rise * age
        alpha = max(alpha0 * (1.0 - age / life), 0.32)

        random.seed(rng.random())        # vary the lobes per cloud, reproducibly
        art = puffs._render_cloud(size * SCALE, premultiply=False)
        faded = art.copy()
        faded.putalpha(art.split()[3].point(lambda v: int(v * alpha)))
        canvas.alpha_composite(faded, (int(x * SCALE), int(y * SCALE)))

    return canvas.crop(canvas.getbbox())


def on_panel(art: Image.Image, pad: int = 46) -> Image.Image:
    """The clouds are pale by design - they are meant to read over a dark taskbar.
    On a white page they all but disappear, so anything shown inline in the docs
    gets the dark ground they were drawn for. The transparent PNG stays available
    for anyone who wants to place them somewhere else."""
    panel = Image.new("RGBA", (art.width + pad * 2, art.height + pad * 2), (16, 18, 22, 255))
    panel.alpha_composite(art, (pad, pad))
    return panel


def main() -> int:
    random.seed(SEED)
    hero = puffs._render_cloud(320, premultiply=False)
    hero.save(DOCS / "cloud.png")
    print("cloud.png        ", hero.size)

    art = in_flight()
    art.save(DOCS / "clouds.png")
    print("clouds.png       ", art.size, "(transparent)")

    preview = on_panel(art)
    preview.convert("RGB").save(DOCS / "clouds-preview.png")
    print("clouds-preview.png", preview.size, "(dark ground, for inline docs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
