"""Trusted native episode host for screenshot-only code evaluation.

This is a synthetic daytime combat/economy benchmark, NOT original-game visual
transfer. No model, network, file, or server operations occur during episodes.
The candidate must never receive this object, native objects, or truth(). Its
interface is a copied frame plus its own memory, returning one action dictionary.
"""
from __future__ import annotations

import random

from . import render

UPSTREAM = "bb6347233fbbf9964fa6dd4a15f721262eda15ce"
ENVIRONMENT = "pvzemu-procedural-day-v1"
PLANTS = render.PLANTS
COSTS = dict(zip(PLANTS, (50, 100, 50, 25, 150, 175)))
NATIVE_NAMES = dict(zip(PLANTS, ("sunflower", "pea_shooter", "wallnut", "potato_mine", "cherry_bomb", "snow_pea")))
ALIASES = {"pea_shooter": "peashooter", "wall-nut": "wallnut", "wall_nut": "wallnut",
           "potato-mine": "potato_mine", "cherry-bomb": "cherry_bomb", "snowpea": "snow_pea"}
LIMITATIONS = (
    "Procedural RGB symbols; no claim of original-game screenshot transfer.",
    "Daytime 5x9 only; six plants; normal, conehead and buckethead zombies.",
    "Fixed seeded external spawn schedule replaces native Survival Endless waves.",
    "Native economy/cooldowns with 50 starting sun; sun auto-credits, no sun clicking.",
    "No mowers; any native house breach loses. No shovel or cob-cannon actions.",
    "Deterministic on the same pinned build/platform; cross-toolchain parity is unverified.",
    "Local training patch seeds construction and prevents sunflower generation resetting sky-sun timer.",
)


class EmulatorEpisode:
    """One fresh native World; 100 ticks represent one game second.

    step applies at most one action before advancing ticks (1..18000). Invalid
    actions still advance time and are counted only in privileged truth. Public
    step returns timing/termination only; native action success/error reasons
    remain in privileged truth, not the return value.
    """

    def __init__(self, seed: int, ticks: int = 18000):
        if type(seed) is not int or not 0 <= seed < 2**32:
            raise ValueError("seed must be a uint32 integer")
        if type(ticks) is not int or not 1 <= ticks <= 180000:
            raise ValueError("ticks must be in 1..180000")
        try:
            import pvzemu
        except ImportError as exc:
            raise RuntimeError("Build pvzemu first: python scripts/install_emulator.py") from exc
        if getattr(pvzemu, "training_patch", None) != "pvz-agent-v1":
            raise RuntimeError("pvzemu requires pinned training patch; run scripts/install_emulator.py")
        self._native = pvzemu
        self._world = pvzemu.World(pvzemu.SceneType.day, seed)
        pvzemu._configure_training(self._world, 50)
        selected = [getattr(pvzemu.PlantType, NATIVE_NAMES[name]) for name in PLANTS]
        # The upstream binding does not expose the C++ default second argument.
        if not self._world.select_plants(selected, pvzemu.PlantType.none):
            raise RuntimeError("Native card selection failed")
        self._seed, self._limit, self._tick = seed, ticks, 0
        rng = random.Random(seed)
        self._schedule = []
        # Last 30 seconds allow cleanup; short test episodes can have no spawns.
        for t in range(1800, max(1800, ticks-3000), 900):
            choices = ("zombie",) if t < 6000 else ("zombie", "zombie", "conehead", "buckethead")
            self._schedule.append((t, rng.choice(choices), rng.randrange(5), rng.getrandbits(32)))
        self._spawned = self._placed = self._eaten = self._invalid = 0
        self._outcome = "running"

    @property
    def done(self) -> bool:
        return self._outcome != "running"

    def _plants(self):
        reverse = {value: key for key, value in NATIVE_NAMES.items()}
        return [{"plant": reverse.get(p.type.name, p.type.name), "row": int(p.row),
                 "col": int(p.col), "hp": int(p.hp)}
                for p in self._world.scene.plants if not p.is_dead and not p.is_smashed]

    def _zombies(self):
        return [{"type": z.type.name, "row": int(z.row), "x": float(z.x),
                 "hp": int(z.hp), "armor_hp": int(z.accessory_1.hp)}
                for z in self._world.scene.zombies if not z.is_dead and z.is_not_dying]

    def _cards(self):
        sun = int(self._world.scene.sun.sun)
        return [{"plant": name, "cost": COSTS[name], "cooldown": int(card.cold_down),
                 "ready": card.cold_down == 0 and sun >= COSTS[name]}
                for name, card in zip(PLANTS, self._world.scene.cards)]

    def _apply(self, action):
        if action is None:
            return
        if not isinstance(action, dict) or set(action) != {"plant", "row", "col"}:
            self._invalid += 1
            return
        name, row, col = action["plant"], action["row"], action["col"]
        if not isinstance(name, str):
            self._invalid += 1
            return
        name = ALIASES.get(name, name)
        if name not in PLANTS or type(row) is not int or type(col) is not int or not (0 <= row < 5 and 0 <= col < 9):
            self._invalid += 1
            return
        if any(p["row"] == row and p["col"] == col for p in self._plants()):
            self._invalid += 1
            return
        if self._world.plant(PLANTS.index(name), row, col):
            self._placed += 1
        else:
            self._invalid += 1

    def step(self, action: dict | None, ticks: int = 100) -> dict:
        if type(ticks) is not int or not 1 <= ticks <= 18000:
            raise ValueError("step ticks must be in 1..18000")
        if self.done:
            return {"advanced_ticks": 0, "done": True}
        before_tick = self._tick
        self._apply(action)
        for _ in range(min(ticks, self._limit-self._tick)):
            self._tick += 1
            if self._spawned < len(self._schedule) and self._schedule[self._spawned][0] == self._tick:
                _, kind, row, event_seed = self._schedule[self._spawned]
                self._native._spawn_training(self._world, getattr(self._native.ZombieType, kind), row, event_seed)
                self._spawned += 1
            # Keep live native references for this single update only. Eaten plants
            # have hp<=0; bombs/mines that activate retain HP, so aren't miscounted.
            before_plants = [p for p in self._world.scene.plants if not p.is_dead]
            self._world.update()
            self._eaten += sum(p.is_dead and p.hp <= 0 for p in before_plants)
            if self._world.scene.is_game_over:
                self._outcome = "lose"
                break
            if self._schedule and self._spawned == len(self._schedule) and not self._zombies():
                self._outcome = "win"
                break
        if not self.done and self._tick == self._limit:
            self._outcome = "timeout"
        return {"advanced_ticks": self._tick-before_tick, "done": self.done}

    def frame(self):
        """Return a fresh RGB ndarray; never a view into state or a metadata bundle."""
        return render.render(sun=int(self._world.scene.sun.sun), plants=self._plants(),
                             zombies=self._zombies(), cards=self._cards(), tick=self._tick,
                             limit=self._limit, outcome=self._outcome)

    def truth(self) -> dict:
        """Privileged judge snapshot. Do not give this or this episode to candidate code."""
        zombies = self._zombies()
        return {"game": "emulator", "environment": ENVIRONMENT, "seed": self._seed, "tick": self._tick, "limit": self._limit,
                "outcome": self._outcome, "killed": self._spawned-len(zombies),
                "scheduled": len(self._schedule), "spawned": self._spawned, "alive": len(zombies),
                "plants_placed": self._placed, "plants_eaten": self._eaten,
                "invalid_actions": self._invalid, "sun": int(self._world.scene.sun.sun),
                "plants": self._plants(), "zombies": zombies, "cards": self._cards(),
                "engine": {"name": "dnartz/PvZ-Emulator", "revision": UPSTREAM, "patch": "pvz-agent-v1"},
                "limitations": list(LIMITATIONS)}
