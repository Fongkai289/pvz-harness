"""Fake lawn game: a small, simplified Plants vs. Zombies, drawn so the player can only see pixels.

Not a copy of the game; just enough of its structure to test the whole agent (look, plan, Jev,
hard rules, RSI) against wins and losses:

- a seed bar with the sun count and one card per loadout plant; a card is bright when it can be
  bought (affordable and recharged) and dark otherwise, as in the game
- planting = click a usable card, then a free lawn cell
- sun falls from the sky and comes from sunflowers; clicking it adds 25
- zombies walk in from the right on scheduled waves, stop to eat plants (nothing signals that a
  plant was eaten), and are shot by peashooters; potato mines and cherry bombs kill outright
- one lawnmower per row clears that row once; a zombie reaching the house a second time loses
- winning = every scheduled zombie spawned and killed

All randomness comes from the seed, so the same seed is the same level. Time runs in steps: one
step per frame grabbed, so at 20 frames/s, 20 steps are one game second. Ground truth (state())
is for the judge and coach only.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field

import cv2
import numpy as np

from common.protocol import Command
from rules.hard_rules import PLANT_COSTS
from server import geometry

W, H = 800, 600
ROWS, COLS = geometry.ROWS, geometry.COLS
STEPS_PER_SECOND = 20

LOADOUT = ["peashooter", "sunflower", "cherry bomb", "wall-nut", "potato mine", "snow pea"]
RECHARGE = {"peashooter": 150, "sunflower": 150, "cherry bomb": 1000, "wall-nut": 600, "potato mine": 600,
            "snow pea": 150}  # steps
PLANT_HP = {"wall-nut": 4000}
DEFAULT_PLANT_HP = 300
SHOOT_EVERY, PEA_DAMAGE = 28, 20
SUNFLOWER_EVERY, SKY_SUN_EVERY, SUN_LIFETIME, SUN_VALUE = 480, 200, 300, 25
POTATO_ARM, CHERRY_FUSE = 300, 20
EAT_PER_STEP = 5  # 100 damage per second

ZOMBIES = {  # hp, speed in px per step (80 px cell in ~4.7 s for a normal zombie)
    "normal": (270, 0.85), "flag": (270, 1.0), "conehead": (640, 0.85), "buckethead": (1370, 0.85),
    "football": (1670, 1.7), "pole vaulting": (500, 1.7),
}


def cell_center(row: int, col: int) -> tuple[int, int]:
    return geometry.cell_center(row, col)


def col_of(x: float) -> int:
    return int((x - geometry.LAWN_X0) // geometry.CELL_W)


@dataclass
class Plant:
    kind: str
    row: int
    col: int
    hp: int
    age: int = 0


@dataclass
class Zombie:
    kind: str
    row: int
    x: float
    hp: int
    speed: float
    slowed: int = 0
    vaulted: bool = False


@dataclass
class Sun:
    x: float
    y: float
    age: int = 0


@dataclass
class Level:
    """Zombie schedule from a seed: (step, kind, row). The flag wave comes near the end."""

    schedule: list[tuple[int, str, int]] = field(default_factory=list)

    @classmethod
    def from_seed(cls, seed: int, steps: int, difficulty: float = 1.0) -> Level:
        rng = random.Random(seed)
        easy = ["normal"] * 6 + ["conehead"] * 3
        kinds = easy + ["buckethead", "pole vaulting", "football"] if difficulty >= 1 else easy
        sched, t = [], 400
        flag_at = int(steps * 0.7)
        while t < flag_at:
            sched.append((t, rng.choice(kinds), rng.randrange(ROWS)))
            t += int(rng.uniform(140, 260) / difficulty)
        sched.append((flag_at, "flag", rng.randrange(ROWS)))
        for _ in range(int(4 * difficulty)):
            sched.append((flag_at + rng.randrange(20, 120), rng.choice(kinds), rng.randrange(ROWS)))
        return cls(sorted(sched))


class FakeLawn:
    def __init__(self, seed: int = 0, steps: int = 1800, difficulty: float = 1.0, start_sun: int = 150,
                 loadout: list[str] | None = None) -> None:
        self.rng = random.Random(seed)
        self.loadout = loadout or list(LOADOUT)
        self.level = Level.from_seed(seed, steps, difficulty)
        self.steps_total = steps
        self.step_n = 0
        self.sun = start_sun
        self.plants: dict[tuple[int, int], Plant] = {}
        self.zombies: list[Zombie] = []
        self.suns: list[Sun] = []
        self.recharge = {p: 0 for p in self.loadout}
        self.selected: int | None = None
        self.mowers = [True] * ROWS
        self.outcome: str | None = None
        self.paused = False
        self.counters = {"spawned": 0, "killed": 0, "mowers_used": 0, "plants_placed": 0, "plants_eaten": 0,
                         "sun_collected": 0, "clicks": 0, "wasted_clicks": 0}
        self._next_spawn = 0
        self._lock = threading.Lock()

    # ---- rules ----

    def card_usable(self, slot: int) -> bool:
        plant = self.loadout[slot]
        return self.recharge[plant] == 0 and self.sun >= PLANT_COSTS.get(plant, 0)

    def step(self) -> None:
        with self._lock:
            if self.paused or self.outcome:
                return
            self.step_n += 1
            for p in self.recharge:
                self.recharge[p] = max(0, self.recharge[p] - 1)
            self._spawn()
            self._suns()
            self._plants_act()
            self._zombies_act()
            if self._next_spawn >= len(self.level.schedule) and not self.zombies:
                self.outcome = "win"
            elif self.step_n >= self.steps_total + 1200 and not self.outcome:
                self.outcome = "timeout"

    def _spawn(self) -> None:
        while self._next_spawn < len(self.level.schedule) and self.level.schedule[self._next_spawn][0] <= self.step_n:
            _, kind, row = self.level.schedule[self._next_spawn]
            hp, speed = ZOMBIES[kind]
            self.zombies.append(Zombie(kind, row, W - 30.0, hp, speed))
            self.counters["spawned"] += 1
            self._next_spawn += 1

    def _suns(self) -> None:
        if self.step_n % SKY_SUN_EVERY == 0:
            x, y = cell_center(self.rng.randrange(ROWS), self.rng.randrange(COLS))
            self.suns.append(Sun(x, y))
        for p in self.plants.values():
            if p.kind == "sunflower" and p.age > 0 and p.age % SUNFLOWER_EVERY == 0:
                x, y = cell_center(p.row, p.col)
                self.suns.append(Sun(x + 18, y - 22))
        for s in self.suns:
            s.age += 1
        self.suns = [s for s in self.suns if s.age < SUN_LIFETIME]

    def _plants_act(self) -> None:
        for cell, p in list(self.plants.items()):
            p.age += 1
            x, _ = cell_center(p.row, p.col)
            if p.kind in ("peashooter", "snow pea") and p.age % SHOOT_EVERY == 0:
                ahead = [z for z in self.zombies if z.row == p.row and z.x > x - 10]
                if ahead:
                    target = min(ahead, key=lambda z: z.x)
                    target.hp -= PEA_DAMAGE
                    if p.kind == "snow pea":
                        target.slowed = 200
            elif p.kind == "potato mine" and p.age >= POTATO_ARM:
                hit = [z for z in self.zombies if z.row == p.row and abs(z.x - x) < 45]
                if hit:
                    for z in hit:
                        z.hp = 0
                    del self.plants[cell]
            elif p.kind == "cherry bomb" and p.age >= CHERRY_FUSE:
                for z in self.zombies:
                    if abs(z.row - p.row) <= 1 and abs(z.x - x) <= 120:
                        z.hp = 0
                del self.plants[cell]
        dead = [z for z in self.zombies if z.hp <= 0]
        self.counters["killed"] += len(dead)
        self.zombies = [z for z in self.zombies if z.hp > 0]

    def _zombies_act(self) -> None:
        for z in self.zombies:
            front = z.x - 15
            col = col_of(front)
            plant = self.plants.get((z.row, col)) if 0 <= col < COLS else None
            if plant and abs(cell_center(plant.row, plant.col)[0] - front) < 30:
                if z.kind == "pole vaulting" and not z.vaulted:
                    z.x -= geometry.CELL_W + 10
                    z.vaulted, z.speed = True, ZOMBIES["normal"][1]
                    continue
                plant.hp -= EAT_PER_STEP
                if plant.hp <= 0:
                    del self.plants[(plant.row, plant.col)]
                    self.counters["plants_eaten"] += 1
                continue
            z.x -= z.speed * (0.5 if z.slowed else 1.0)
            z.slowed = max(0, z.slowed - 1)
            if z.x < geometry.LAWN_X0 - 10:
                if self.mowers[z.row]:
                    self.mowers[z.row] = False
                    self.counters["mowers_used"] += 1
                    for other in self.zombies:
                        if other.row == z.row:
                            other.hp = 0
                else:
                    self.outcome = "lose"
        dead = [z for z in self.zombies if z.hp <= 0]
        self.counters["killed"] += len(dead)
        self.zombies = [z for z in self.zombies if z.hp > 0]

    # ---- input ----

    async def execute(self, cmd: Command) -> dict | None:
        with self._lock:
            if cmd.action == "pause":
                self.paused = True
            elif cmd.action == "resume":
                self.paused = False
            elif cmd.action == "click" and cmd.x is not None and cmd.y is not None:
                return self._click(cmd.x, cmd.y)
        return None

    def _click(self, x: int, y: int) -> dict:
        self.counters["clicks"] += 1
        for s in self.suns:
            if abs(s.x - x) + abs(s.y - y) <= 30:
                self.suns.remove(s)
                self.sun += SUN_VALUE
                self.counters["sun_collected"] += SUN_VALUE
                return {"collected": 1}
        for slot in range(len(self.loadout)):
            x0, y0, x1, y1 = geometry.seed_card_rect(slot)
            if x0 <= x <= x1 and y0 <= y <= y1:
                self.selected = slot if self.card_usable(slot) else None
                return {"selected": self.loadout[slot] if self.selected is not None else None}
        row = int((y - geometry.LAWNS[ROWS][0]) // geometry.LAWNS[ROWS][1])
        col = col_of(x)
        if self.selected is not None and 0 <= row < ROWS and 0 <= col < COLS:
            plant = self.loadout[self.selected]
            self.selected = None
            if (row, col) not in self.plants and self.sun >= PLANT_COSTS.get(plant, 0):
                self.plants[(row, col)] = Plant(plant, row, col, PLANT_HP.get(plant, DEFAULT_PLANT_HP))
                self.sun -= PLANT_COSTS.get(plant, 0)
                self.recharge[plant] = RECHARGE.get(plant, 300)
                self.counters["plants_placed"] += 1
                return {"planted": plant, "row": row, "col": col}
            return {"planted": None}
        self.counters["wasted_clicks"] += 1
        return {"collected": 0}

    # ---- output ----

    def state(self) -> dict:
        with self._lock:
            return {**self.counters, "game": "lawn", "outcome": self.outcome, "step": self.step_n,
                    "sun": self.sun, "mowers_left": sum(self.mowers),
                    "scheduled": len(self.level.schedule),
                    "zombies": [{"type": z.kind, "row": z.row, "col": col_of(z.x)} for z in self.zombies],
                    "plants": {f"{p.row},{p.col}": p.kind for p in self.plants.values()}}

    def grab(self) -> tuple[bytes, int, int]:
        self.step()
        with self._lock:
            img = self.render()
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        assert ok
        return buf.tobytes(), W, H

    def render(self) -> np.ndarray:
        img = np.zeros((H, W, 3), np.uint8)
        img[:] = (150, 170, 190)  # paving beyond the lawn
        y0, h = geometry.LAWNS[ROWS]
        for r in range(ROWS):
            for c in range(COLS):
                shade = (40, 150, 60) if (r + c) % 2 == 0 else (50, 165, 75)
                x = geometry.LAWN_X0 + c * geometry.CELL_W
                cv2.rectangle(img, (x, y0 + r * h), (x + geometry.CELL_W - 1, y0 + (r + 1) * h - 1), shade, -1)
            if self.mowers[r]:
                cv2.rectangle(img, (8, y0 + r * h + 40), (34, y0 + r * h + 62), (40, 40, 200), -1)
        for p in self.plants.values():
            draw_plant(img, p.kind, *cell_center(p.row, p.col), armed=p.age >= POTATO_ARM)
        for z in sorted(self.zombies, key=lambda z: z.row):
            draw_zombie(img, z.kind, int(z.x), cell_center(z.row, 0)[1], z.vaulted)
        for s in self.suns:
            cv2.circle(img, (int(s.x), int(s.y)), 20, (0, 215, 255), -1)
        self._render_bar(img)
        return img

    def _render_bar(self, img: np.ndarray) -> None:
        cv2.rectangle(img, (0, 0), (geometry.TOP_BAR[2], geometry.TOP_BAR[3]), (40, 70, 110), -1)
        cv2.circle(img, (50, 32), 20, (0, 215, 255), -1)
        x0, y0, x1, y1 = geometry.SUN_COUNTER
        cv2.rectangle(img, (x0, y0), (x1, y1), (200, 230, 240), -1)
        cv2.putText(img, str(self.sun), (x0 + 6, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
        for slot, plant in enumerate(self.loadout):
            cx0, cy0, cx1, cy1 = geometry.seed_card_rect(slot)
            card = np.full((cy1 - cy0, cx1 - cx0, 3), (190, 235, 210), np.uint8)
            draw_plant(card, plant, (cx1 - cx0) // 2, 28, scale=0.55, armed=True)
            cv2.putText(card, str(PLANT_COSTS.get(plant, 0)), (6, cy1 - cy0 - 6), cv2.FONT_HERSHEY_SIMPLEX,
                        0.4, (0, 0, 0), 1)
            if not self.card_usable(slot):
                card = (card * 0.4).astype(np.uint8)
            img[cy0:cy1, cx0:cx1] = card


def draw_plant(img: np.ndarray, kind: str, x: int, y: int, scale: float = 1.0, armed: bool = True) -> None:
    s = lambda v: max(1, int(v * scale))
    if kind == "sunflower":  # pale petals, brown face: not sun-coloured
        for a in range(0, 360, 45):
            px, py = x + int(s(20) * np.cos(np.radians(a))), y - s(8) + int(s(20) * np.sin(np.radians(a)))
            cv2.circle(img, (px, py), s(9), (140, 235, 255), -1)
        cv2.circle(img, (x, y - s(8)), s(13), (40, 80, 140), -1)
        cv2.line(img, (x, y + s(5)), (x, y + s(30)), (40, 140, 40), s(4))
    elif kind in ("peashooter", "snow pea"):
        head = (40, 200, 60) if kind == "peashooter" else (235, 200, 130)
        cv2.line(img, (x - s(4), y), (x - s(4), y + s(30)), (40, 140, 40), s(4))
        cv2.circle(img, (x - s(6), y - s(8)), s(15), head, -1)
        cv2.rectangle(img, (x + s(6), y - s(14)), (x + s(20), y - s(2)), head, -1)
        cv2.circle(img, (x - s(10), y - s(12)), s(3), (0, 0, 0), -1)
    elif kind == "wall-nut":
        cv2.ellipse(img, (x, y), (s(22), s(30)), 0, 0, 360, (50, 110, 170), -1)
        for dx in (-8, 6):
            cv2.circle(img, (x + s(dx), y - s(8)), s(4), (255, 255, 255), -1)
    elif kind == "potato mine":
        cv2.ellipse(img, (x, y + s(18)), (s(22), s(12)), 0, 180, 360, (60, 120, 170), -1)
        if armed:
            cv2.line(img, (x, y + s(8)), (x, y - s(4)), (160, 160, 160), s(2))
            cv2.circle(img, (x, y - s(6)), s(4), (0, 0, 220), -1)
    elif kind == "cherry bomb":
        for dx in (-10, 10):
            cv2.circle(img, (x + s(dx), y + s(4)), s(13), (30, 30, 210), -1)
        cv2.line(img, (x - s(8), y - s(8)), (x, y - s(24)), (40, 120, 40), s(3))
        cv2.line(img, (x + s(8), y - s(8)), (x, y - s(24)), (40, 120, 40), s(3))
    else:
        cv2.circle(img, (x, y), s(15), (120, 120, 120), -1)


def draw_zombie(img: np.ndarray, kind: str, x: int, y: int, vaulted: bool = False) -> None:
    """y is the row centre; the figure stands in its row (row 0's hat stays below the seed bar)."""
    y += 12
    suit = (40, 90, 140)  # brown suit
    shirt = (230, 230, 230)
    if kind == "pole vaulting":
        suit, shirt = (60, 60, 200), (240, 240, 240)  # red and white tank top
    if kind == "football":
        suit = (40, 40, 190)
    cv2.rectangle(img, (x - 13, y - 14), (x + 13, y + 30), suit, -1)
    cv2.rectangle(img, (x - 4, y - 14), (x + 4, y + 10), shirt, -1)
    if kind not in ("pole vaulting", "football"):
        cv2.line(img, (x, y - 12), (x, y + 6), (30, 30, 200), 3)  # red tie
    cv2.circle(img, (x, y - 26), 12, (150, 175, 160), -1)  # grey-green head
    cv2.circle(img, (x - 5, y - 28), 3, (255, 255, 255), -1)
    if kind == "conehead":
        cv2.fillPoly(img, [np.array([(x - 12, y - 34), (x + 12, y - 34), (x, y - 58)])], (0, 130, 255))
    elif kind == "buckethead":
        cv2.rectangle(img, (x - 14, y - 52), (x + 14, y - 32), (175, 175, 175), -1)
        cv2.line(img, (x - 14, y - 46), (x + 14, y - 46), (110, 110, 110), 2)
    elif kind == "football":
        cv2.ellipse(img, (x, y - 29), (15, 14), 0, 180, 360, (30, 30, 210), -1)
        cv2.rectangle(img, (x - 20, y - 14), (x + 20, y - 4), (30, 30, 210), -1)  # shoulder pads
    elif kind == "pole vaulting" and not vaulted:
        cv2.line(img, (x - 60, y - 6), (x + 50, y - 6), (200, 200, 200), 3)
    elif kind == "flag":
        cv2.line(img, (x + 14, y - 56), (x + 14, y + 10), (60, 60, 60), 2)
        cv2.rectangle(img, (x + 14, y - 56), (x + 40, y - 38), (40, 40, 200), -1)
        cv2.circle(img, (x + 27, y - 47), 6, (180, 160, 255), -1)  # brain
