"""Fake game for testing the server without Windows.

    python -m client.mock_client --token ... --seconds 30 [--seed 1] [--playbook v002]

A lawn with falling suns. It is deliberately harder than "find yellow circles", so
that playbook changes have something to fix:

- sunflower heads: static yellow-green discs that look sun-like; clicking one is wasted
- small suns: a third of the suns are smaller than the seed playbook's minimum area
- suns expire after LIFETIME steps if nobody collects them

A click within 30 px of a sun collects it. The game's own counters are reported as
ground-truth state (judge and coach only), and each click's outcome is returned.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import threading

import cv2
import numpy as np

from client.fake_lawn import FakeLawn
from client.link import ServerLink
from common.protocol import Command

W, H = 800, 600
LAWN_BGR = (40, 140, 40)
# OpenCV hue ~27-29, as measured on real suns (2026-10-29: 27.5-28.8). The earlier 22-27 overlapped the
# real game's wall-nuts and potato mines (hue ~22-23), which a sun range wide enough for it clicked all game.
SUN_BGR = [(0, 230, 255), (0, 235, 255), (0, 240, 255), (0, 245, 255)]
DECOY_BGR = (0, 255, 230)  # OpenCV hue ~33: inside the seed range, outside a tight sun range
SUN_RADIUS, SMALL_SUN_RADIUS, DECOY_RADIUS = 20, 13, 18
SPAWN_P, SMALL_P, LIFETIME, FALL = 0.06, 0.33, 140, 2.0
HIT_PX = 30


class FakeGame:
    def __init__(self, seed: int = 0) -> None:
        self.rng = random.Random(seed)
        self.decoys = [(self.rng.uniform(120, 680), self.rng.uniform(200, 520)) for _ in range(3)]
        self.suns: list[dict] = []
        self.paused = False
        self.counters = {"spawned": 0, "spawned_small": 0, "collected": 0, "collected_small": 0,
                         "expired": 0, "expired_small": 0, "clicks": 0, "wasted_clicks": 0,
                         "decoy_clicks": 0}
        self._lock = threading.Lock()

    def step(self) -> None:
        with self._lock:
            if self.paused:
                return
            if self.rng.random() < SPAWN_P:
                small = self.rng.random() < SMALL_P
                self.suns.append({"x": self.rng.uniform(80, 720), "y": 90.0, "age": 0, "small": small,
                                  "bgr": self.rng.choice(SUN_BGR)})
                self.counters["spawned"] += 1
                self.counters["spawned_small"] += small
            alive = []
            for s in self.suns:
                s["y"] = min(s["y"] + FALL, 540.0)
                s["age"] += 1
                if s["age"] > LIFETIME:
                    self.counters["expired"] += 1
                    self.counters["expired_small"] += s["small"]
                else:
                    alive.append(s)
            self.suns = alive

    def grab(self) -> tuple[bytes, int, int]:
        self.step()
        img = np.zeros((H, W, 3), np.uint8)
        img[:] = LAWN_BGR
        with self._lock:
            for x, y in self.decoys:
                cv2.circle(img, (int(x), int(y)), DECOY_RADIUS, DECOY_BGR, -1)
            for s in self.suns:
                r = SMALL_SUN_RADIUS if s["small"] else SUN_RADIUS
                cv2.circle(img, (int(s["x"]), int(s["y"])), r, s["bgr"], -1)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        assert ok
        return buf.tobytes(), W, H

    def state(self) -> dict:
        with self._lock:
            return {**self.counters, "suns_on_screen": len(self.suns)}

    async def execute(self, cmd: Command) -> dict | None:
        with self._lock:
            if cmd.action == "pause":
                self.paused = True
            elif cmd.action == "resume":
                self.paused = False
            elif cmd.action == "click" and cmd.x is not None and cmd.y is not None:
                hit = [s for s in self.suns if abs(s["x"] - cmd.x) + abs(s["y"] - cmd.y) <= HIT_PX]
                decoy = any(abs(x - cmd.x) + abs(y - cmd.y) <= HIT_PX for x, y in self.decoys)
                self.suns = [s for s in self.suns if s not in hit]
                self.counters["clicks"] += 1
                self.counters["collected"] += len(hit)
                self.counters["collected_small"] += sum(s["small"] for s in hit)
                if not hit:
                    self.counters["wasted_clicks"] += 1
                    self.counters["decoy_clicks"] += decoy
                return {"collected": len(hit), "decoy": decoy and not hit}
        return None


async def play(url: str, token: str, seconds: float, seed: int, fps: float = 20,
               playbook: str | None = None, client_id: str = "mock", game: str = "suns") -> dict:
    """game="suns": the sun-collecting game above; game="lawn": client/fake_lawn.py, with zombies.
    A lawn episode ends when it is won or lost, or after `seconds`."""
    if game == "lawn":
        world = FakeLawn(seed)
        done = lambda: world.outcome is not None
    else:
        world, done = FakeGame(seed), None
    link = ServerLink(url, token, client_id, world.grab, world.execute, read_state=world.state,
                      fps=fps, playbook=playbook)
    await link.run(duration=seconds, done=done)
    state = world.state()
    if game == "lawn":
        state = {k: v for k, v in state.items() if k not in ("zombies", "plants")}
    return {"session": link.session_id, "playbook": link.playbook, "seed": seed,
            "frames_sent": link.frame_id, **state, **link.latency_summary()}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    parser.add_argument("--token", required=True)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--fps", type=float, default=20)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--playbook", default=None)
    parser.add_argument("--game", choices=["suns", "lawn"], default="suns")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(await play(args.url, args.token, args.seconds, args.seed, args.fps, args.playbook, game=args.game))


if __name__ == "__main__":
    asyncio.run(main())
