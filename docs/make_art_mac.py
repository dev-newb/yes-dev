"""Regenerate the macOS documentation art from the live renderer.

The macOS counterpart of make_art.py. Same idea - replay a real burst and freeze
it mid-flight rather than lining clouds up by hand - but the macOS geometry:
clouds are released from under the menu bar and fall away from the status item,
hanging rather than sitting.

Everything comes from puffs.py and puffs_mac.py: the same shapes, the same motion
ranges, the same release band, the same flip. The art cannot drift from the app.
Seeded, so re-running does not churn the repo.

Run it on a Mac (it imports puffs_mac, which needs pyobjc):

    python docs/make_art_mac.py

Outputs, sized to sit beside the Windows pair in the README:

    docs/clouds-mac.png          the clouds alone, on transparency
    docs/clouds-mac-preview.png  the same on a dark ground, with the menu bar
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import puffs
import puffs_mac

DOCS = Path(__file__).resolve().parent

# Matches docs/clouds-preview.png, so the two sit level in a side-by-side table.
OUT_W, OUT_H = 600, 599
# The slice of desktop the frame shows, in points. Sized so the burst fills it:
# the clouds spawn 60-210pt in from the right edge and fall up to ~220pt, and a
# much wider view would leave most of the picture as empty desktop.
VIEW_PT = 250
CLOUDS = 14
SEED = 11

MENU_BAR = 30            # points, measured on the machine this was built on
GROUND = (16, 18, 22)    # the same dark ground on_panel() uses
BAR = (30, 32, 39)
STATUS_GREEN = (46, 160, 67)


def _frame(with_chrome: bool):
    """One frozen frame of a burst. `with_chrome` draws the ground and menu bar."""
    rng = random.Random(SEED)
    W, H = OUT_W * 2, OUT_H * 2                     # render at 2x, downscale at the end
    pt = W / VIEW_PT                                # pixels per point
    canvas = Image.new("RGBA", (W, H), GROUND + (255,) if with_chrome else (0, 0, 0, 0))

    vw = VIEW_PT                                    # the visible region, in points

    for i in range(CLOUDS):
        size = rng.randint(*puffs.SIZE_RANGE)
        alpha0 = rng.uniform(*puffs.ALPHA_RANGE)
        life = rng.uniform(*puffs.LIFE_RANGE)
        fall = rng.uniform(*puffs.RISE_RANGE)       # carried downward on macOS
        drift = rng.uniform(*puffs.DRIFT_RANGE)
        gap = rng.randint(*puffs_mac.SPAWN_Y_DOWN)  # released this far under the bar

        # Spread the ages so one frame catches the whole arc: fresh and bright up
        # near the bar, lower and fainter on the way out. The jitter is wide on
        # purpose - stepping the age straight off the index lines the clouds up in
        # a neat diagonal, which a real burst never does.
        step = (i + rng.uniform(-0.85, 0.85)) / max(1, CLOUDS - 1)
        age = life * min(max(0.04 + 0.86 * step, 0.03), 0.88)

        # puffs_mac releases a cloud just under the bar and carries it down.
        y = MENU_BAR + gap + fall * age
        x = vw - rng.randint(*puffs.SPAWN_X_BACK) + drift * age
        # A floor on the opacity: the last frames of a real fade are too faint to
        # read as a picture, though they are correct on screen.
        alpha = max(alpha0 * (1.0 - age / life), 0.30)

        random.seed(rng.random())                   # vary the lobes, reproducibly
        art = puffs_mac._flip_hanging(
            puffs._render_cloud(int(size * pt), premultiply=False))
        faded = art.copy()
        faded.putalpha(art.split()[3].point(lambda v: int(v * alpha)))
        canvas.alpha_composite(faded, (int(x * pt), int(y * pt)))

    if with_chrome:
        _menu_bar(canvas, pt)
    return canvas


def _menu_bar(canvas, pt: float) -> None:
    """The bar, drawn last so a cloud that reaches it passes behind rather than
    over - which is what happens on screen, since the clouds never rise into it."""
    W = canvas.width
    bar = Image.new("RGBA", (W, int(MENU_BAR * pt)), BAR + (255,))
    canvas.alpha_composite(bar, (0, 0))
    d = ImageDraw.Draw(canvas)

    cy = MENU_BAR * pt / 2
    # The status item itself: the app's own icon, the same cloud flipped and
    # filled, so the picture shows what the clouds are falling out of.
    random.seed(7)                                  # the icon's fixed silhouette
    icon_h = 18 * pt
    icon = puffs._render_cloud(int(icon_h / 0.78), premultiply=False)
    mask = puffs_mac.ImageOps.flip(icon.split()[3])
    body = Image.new("RGBA", mask.size, STATUS_GREEN + (255,))
    body.putalpha(mask)
    ix = int(W - 118 * pt)
    canvas.alpha_composite(body, (ix, int(cy - mask.size[1] / 2)))
    cx = ix + mask.size[0] / 2
    s = mask.size[1] * 0.0118 * 1.34
    d.line([(cx - 13 * s, cy + 0.5 * s), (cx - 4.5 * s, cy + 9.5 * s),
            (cx + 13.5 * s, cy - 10 * s)],
           fill=(255, 255, 255, 255), width=int(5.6 * s), joint="curve")

    # A few neighbouring menu extras, for scale.
    for i, w in enumerate((13, 9, 11)):
        ex = W - 88 * pt + i * 20 * pt
        d.rounded_rectangle([ex, cy - 6 * pt, ex + w * pt, cy + 6 * pt],
                            radius=2 * pt, fill=(150, 158, 172, 255))


def main() -> int:
    art = _frame(with_chrome=False).resize((OUT_W, OUT_H), Image.LANCZOS)
    art.save(DOCS / "clouds-mac.png")
    print("clouds-mac.png        ", art.size, "(transparent)")

    preview = _frame(with_chrome=True).resize((OUT_W, OUT_H), Image.LANCZOS)
    preview.convert("RGB").save(DOCS / "clouds-mac-preview.png")
    print("clouds-mac-preview.png", preview.size, "(dark ground + menu bar)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
