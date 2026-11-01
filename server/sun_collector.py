"""Pixel-only sun confirmation; no models, budgets, game memory or planting."""
import time

from server.perception import Observation

COLLECT_INTERVAL = 0.25
MAX_FRAME_AGE = 0.8
MAX_CONFIRM_GAP = 0.8


class SunCollector:
    def __init__(self):
        self.current = None
        self.received = 0.0
        self.confirmed = []

    @staticmethod
    def in_play_area(sun):
        # Exclude the entire card/shovel/menu strip, and bottom level/progress UI.
        return 0 <= sun.x < 780 and 90 <= sun.y < 570

    def observe(self, obs: Observation, received=None):
        now = time.monotonic() if received is None else received
        previous = self.current
        self.confirmed = []
        if (previous is not None and obs.frame_id > previous.frame_id
                and 0 <= now - self.received <= MAX_CONFIRM_GAP):
            self.confirmed = [sun for sun in obs.suns if self.in_play_area(sun) and any(
                self.in_play_area(old) and abs(sun.x-old.x) + abs(sun.y-old.y) <= 40
                and 0.5 <= sun.area / max(old.area, 1) <= 2
                for old in previous.suns)]
        self.current, self.received = obs, now

    def targets(self, obs, now=None):
        now = time.monotonic() if now is None else now
        if obs is not self.current or not 0 <= now-self.received <= MAX_FRAME_AGE:
            return []
        return self.confirmed
