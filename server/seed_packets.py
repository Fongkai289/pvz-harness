"""Which plant each seed-bank card shows, read from the screenshot.

Each slot's crop is compared with labeled crops of the game's own card art (knowledge/seed_packets.npz,
built by scripts/build_seed_templates.py from Portable renders). Colour normalized correlation keeps
ready, dimmed and recharging cards apart from each other and from empty slots / the shovel box. A
slot is named only when the best match is clearly ahead; otherwise it is unknown and never playable.
CardMemory keeps each slot's last confident answer for the rest of the level.
"""
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from server import geometry

PATH = Path(__file__).resolve().parent.parent / "knowledge" / "seed_packets.npz"
EMPTY = "empty"
UNKNOWN = "unknown"
MIN_SCORE, MIN_MARGIN = 0.75, 0.05  # held-out Portable games: 0 wrong of 2048 crops, 0.8% unknown
MAX_SLOTS = 10
FEATURE_SIZE = (24, 35)  # width, height


def feature(crop: np.ndarray) -> np.ndarray:
    small = cv2.resize(crop, FEATURE_SIZE, interpolation=cv2.INTER_AREA).astype(np.float32)
    small -= small.reshape(-1, 3).mean(0)  # per-channel offset: brightness changes, hue does not
    vector = small.ravel()
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


@lru_cache(maxsize=1)
def _templates():
    data = np.load(PATH)
    features = np.stack([feature(c) for c in data["crops"]])
    return features, [str(label) for label in data["labels"]]


def known_plants() -> list[str]:
    """Every plant whose card art is in the template set."""
    return sorted(set(_templates()[1]) - {EMPTY})


def identify(crop: np.ndarray) -> tuple[str, float]:
    """(plant, empty or unknown; best correlation) for one slot crop."""
    features, labels = _templates()
    scores = features @ feature(crop)
    best: dict[str, float] = {}
    for label, score in zip(labels, scores):
        best[label] = max(best.get(label, -1.0), float(score))
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    top, score = ranked[0]
    if score < MIN_SCORE or (len(ranked) > 1 and score - ranked[1][1] < MIN_MARGIN):
        return UNKNOWN, round(score, 3)
    return top, round(score, 3)


def read_bank(image: np.ndarray) -> list[tuple[str, float]]:
    """Slots left to right until the first confidently empty one (or the image edge)."""
    slots = []
    for slot in range(MAX_SLOTS):
        x0, y0, x1, y1 = geometry.seed_card_rect(slot)
        if x1 > image.shape[1]:
            break
        name, score = identify(image[y0:y1, x0:x1])
        if name == EMPTY:
            break
        slots.append((name, score))
    return slots


class CardMemory:
    """Per level: a slot keeps its last confident plant while later frames are unsure (e.g. recharging)."""

    def __init__(self):
        self.names: list[str] = []

    def reset(self):
        self.names = []

    def observe(self, image: np.ndarray) -> list[str]:
        seen = read_bank(image)
        if not any(name != UNKNOWN for name, _ in seen):
            return list(self.names)  # no seed bank in view (menu, overlay): nothing learned
        names = []
        for slot, (name, _) in enumerate(seen):
            if name == UNKNOWN and slot < len(self.names):
                name = self.names[slot]
            names.append(name)
        if self.names:
            # Unsure slots past the known bank are background, not new cards.
            while len(names) > len(self.names) and names[-1] == UNKNOWN:
                names.pop()
            # A slot read as empty while an earlier frame named it: keep the earlier, longer bank.
            if len(names) < len(self.names) and all(n == UNKNOWN or n == o for n, o in zip(names, self.names)):
                names += self.names[len(names):]
        self.names = names
        return list(names)
