"""Is the shovel in its box, and where: code, not a model.

The box sits right of the seed bank at one of geometry.SHOVEL_XS (depends on the number of seed slots).
Its crop is compared with knowledge/shovel_box.png (cut from a live 1.2.0.1096 frame, 2026-11-03) using
the seed-packet feature. On 15 live frames: 1.0 in the box, 0.68 with a zombie walking over it,
at most 0.11 at the other positions; an emptied box (shovel picked up) scores about 0.48.
"""
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from server import geometry
from server.seed_packets import feature

PATH = Path(__file__).resolve().parent.parent / "knowledge" / "shovel_box.png"
MIN_SCORE = 0.6


@lru_cache(maxsize=1)
def _template() -> np.ndarray:
    image = cv2.imread(str(PATH))
    if image is None:
        raise FileNotFoundError(PATH)
    return feature(image)


def find_shovel(image: np.ndarray) -> int | None:
    """Left edge of the box holding the shovel, or None when no shovel is in view."""
    best, where = MIN_SCORE, None
    for x0 in geometry.SHOVEL_XS:
        crop = image[0:geometry.SHOVEL_H, x0:x0 + geometry.SHOVEL_W]
        if crop.shape[:2] != (geometry.SHOVEL_H, geometry.SHOVEL_W):
            continue
        score = float(_template() @ feature(crop))
        if score >= best:
            best, where = score, x0
    return where
