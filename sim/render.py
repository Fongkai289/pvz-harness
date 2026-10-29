"""Procedural screenshot contract v1; no assets, state encoder, or model calls.

RGB uint8 (140,180,3). Lawn x=9,y=30, cell size=18; center=(18+18*c,39+18*r).
Sun: four zero-padded digits at (4,4), 3x5 glyphs at scale 2, stride 8.
Cards: slot i starts x=60+19*i; body x..x+12,y=4..16, readiness y=20..22.
Plants: 11x11 color body centered on the cell, with a white glyph at scale 1.
Zombies: 7x9 magenta body, row center y; orange/gray cap for cone/bucket.
No hidden HP, IDs, future schedule, seed, or action-mask metadata is drawn.
"""
from __future__ import annotations

import numpy as np

WIDTH, HEIGHT = 180, 140
GRID_X, GRID_Y, CELL = 9, 30, 18
PLANTS = ("sunflower", "peashooter", "wallnut", "potato_mine", "cherry_bomb", "snow_pea")
COLORS = dict(zip(PLANTS, ((245,200,40), (80,220,90), (170,115,60),
                         (180,100,200), (235,65,65), (70,200,240))))
READY, NOT_READY = (40,230,100), (90,90,100)
ZOMBIE = (210,70,145)
DIGITS = {
    "0": ("111","101","101","101","111"),
    "1": ("010","110","010","010","111"),
    "2": ("111","001","111","100","111"),
    "3": ("111","001","111","001","111"),
    "4": ("101","101","111","001","001"),
    "5": ("111","100","111","001","111"),
    "6": ("111","100","111","101","111"),
    "7": ("111","001","010","010","010"),
    "8": ("111","101","111","101","111"),
    "9": ("111","101","111","001","111"),
}

CONTRACT = (__doc__ + "\nFixed card slot order: " + repr(PLANTS)
            + "\nRGB plant colors: " + repr(COLORS)
            + "\nReadiness RGB: ready=" + repr(READY) + ", unavailable=" + repr(NOT_READY)
            + ". Ready means affordable AND cooldown zero. White glyph RGB=(255,255,255)."
            + "\nDigit templates (1=white, 0=background): " + repr(DIGITS))


def glyph(image, char, x, y, *, scale=1, color=(255,255,255)):
    for r, line in enumerate(DIGITS[char]):
        for c, bit in enumerate(line):
            if bit == "1":
                image[y+r*scale:y+(r+1)*scale, x+c*scale:x+(c+1)*scale] = color


def render(*, sun, plants, zombies, cards, tick, limit, outcome):
    """Trusted renderer input; only the returned detached image crosses to player."""
    image = np.full((HEIGHT, WIDTH, 3), (24,28,38), dtype=np.uint8)
    for row in range(5):
        for col in range(9):
            x, y = GRID_X + CELL*col, GRID_Y + CELL*row
            image[y:y+CELL-1, x:x+CELL-1] = (38,75+5*((row+col)%2),48)
    for i, char in enumerate(f"{min(9999, max(0, int(sun))):04}"):
        glyph(image, char, 4+8*i, 4, scale=2)
    for i, name in enumerate(PLANTS):
        x = 60+19*i
        image[4:17, x:x+13] = COLORS[name]
        glyph(image, str(i+1), x+5, 8)
        image[20:23, x:x+13] = READY if cards[i]["ready"] else NOT_READY
    for p in plants:
        if p["plant"] not in COLORS:
            continue
        x, y = 18+18*p["col"], 39+18*p["row"]
        image[y-5:y+6, x-5:x+6] = COLORS[p["plant"]]
        glyph(image, str(PLANTS.index(p["plant"])+1), x-1, y-2)
    for z in zombies:
        # Native plant center x=40+80*c; same affine map as lawn centers.
        x = max(3, min(WIDTH-4, round(9 + z["x"]*18/80)))
        y = 39+18*z["row"]
        image[y-4:y+5, x-3:x+4] = ZOMBIE
        if z["type"] == "conehead":
            image[y-6:y-4, x-2:x+3] = (250,130,35)
        elif z["type"] == "buckethead":
            image[y-6:y-4, x-3:x+4] = (180,185,195)
    # Visible progress bar and terminal lamp, not numeric judge counters.
    image[129:132, 9:9+int(162*min(tick/limit, 1))] = (130,170,220)
    image[135:138, 9:16] = {"running": (90,90,100), "win": READY,
                            "lose": (235,65,65), "timeout": (245,200,40)}[outcome]
    return image
