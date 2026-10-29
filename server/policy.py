"""Executor: collect observed suns and turn a validated Jev placement into clicks.

Must never import server.judge or read StateMsg (tests/test_isolation.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from rules.playbook_rules import FastParams
from server import geometry
from server.perception import Observation
from server.planner import Placement


@dataclass
class Click:
    x: int
    y: int
    reason: str
    meta: dict[str, Any] = field(default_factory=dict)  # logged with the command, never sent


class FastPolicy:
    def __init__(self, params: FastParams) -> None:
        self.cooldown = params.sun_click_cooldown
        self.same_spot_px = params.sun_same_spot_px
        self._recent_sun_clicks: list[tuple[float, int, int]] = []
        self._rows = geometry.ROWS

    def set_rows(self, rows: int) -> None:
        self._rows = rows  # from the latest scene: pool and fog lawns have six rows

    def tick(self, obs: Observation, now: float | None = None, place: Placement | None = None) -> list[Click]:
        """Sun clicks and one explicit, validated Jev placement; no planner action queue."""
        now = time.monotonic() if now is None else now
        self._recent_sun_clicks = [c for c in self._recent_sun_clicks if now - c[0] < self.cooldown]
        clicks: list[Click] = []
        for sun in obs.suns:
            if any(abs(sun.x - cx) + abs(sun.y - cy) < self.same_spot_px for _, cx, cy in self._recent_sun_clicks):
                continue
            self._recent_sun_clicks.append((now, sun.x, sun.y))
            clicks.append(Click(sun.x, sun.y, "collect sun", {"area": sun.area, "hue": sun.hue}))
        # At most one placement per tick: packet first, then the cell.
        if place is not None:
            p = place
            try:
                sx, sy = geometry.seed_slot_center(p.slot)
                cx, cy = geometry.cell_center(p.row, p.col, self._rows)
            except ValueError:
                return clicks  # never turn invalid coordinates into input events
            clicks.append(Click(sx, sy, f"select slot {p.slot}"))
            clicks.append(Click(cx, cy, f"plant at row {p.row} col {p.col}"))
        return clicks
