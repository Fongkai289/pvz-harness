"""Pixels -> observation. The player's only view of the game.

Must never import server.judge or read StateMsg: that would leak ground truth
to the player (tests/test_isolation.py enforces this). Thresholds come from the
session's playbook, so the coach can tune them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from common.protocol import FrameHeader
from rules.playbook_rules import PerceptionParams
from server import geometry


# What perception can currently recognise. Strategy knowledge that depends on anything else
# (zombie types, plants, sun count, cooldowns) cannot be acted on yet; the coach is told so.
CAPABILITIES = ("sun blobs: position, area, mean hue",)


@dataclass
class Blob:
    x: int
    y: int
    area: int
    hue: float  # mean OpenCV hue (0-179) of the blob's pixels


@dataclass
class Observation:
    frame_id: int
    t_capture: float
    suns: list[Blob] = field(default_factory=list)


def decode_jpeg(jpeg: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("could not decode frame")
    return image


def describe(obs: Observation) -> str:
    """Text summary of what perception saw. This, not ground truth, is what text-only models read."""
    if not obs.suns:
        return "Perception summary: no sun visible."
    spots = ", ".join(f"({b.x}, {b.y})" for b in obs.suns)
    return f"Perception summary: {len(obs.suns)} sun visible at pixel {spots}."


class Perception:
    def __init__(self, params: PerceptionParams) -> None:
        self.lo = np.array(params.sun_hsv_lo)
        self.hi = np.array(params.sun_hsv_hi)
        self.min_area, self.max_area = params.sun_min_area, params.sun_max_area

    def process(self, header: FrameHeader, image: np.ndarray) -> Observation:
        return Observation(
            frame_id=header.frame_id,
            t_capture=header.t_capture,
            suns=self._find_suns(image),
        )

    def _find_suns(self, image: np.ndarray) -> list[Blob]:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, self.lo, self.hi)
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
        suns = []
        for i in range(1, count):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if self.min_area <= area <= self.max_area:
                cx, cy = centroids[i]
                if cx < geometry.TOP_BAR[2] and cy < geometry.TOP_BAR[3]:
                    continue  # the sun icon in the seed bar is not a sun to collect
                hue = float(hsv[:, :, 0][labels == i].mean())
                suns.append(Blob(int(cx), int(cy), area, round(hue, 1)))
        return suns
