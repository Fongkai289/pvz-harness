"""Hard rules: every placement must pass these, whoever proposed it (planner, tactics, a person).

Fixed: the coach cannot edit this file. A rejected placement is not clicked; the reason is logged.
They encode facts of the game and of the agent's own knowledge, not strategy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.planner import Placement
    from server.scene import Scene

COLS = 9

# Standard PC costs (knowledge/pvz_strategy.md); confirm on the installed version.
PLANT_COSTS = {
    "peashooter": 100, "sunflower": 50, "cherry bomb": 150, "wall-nut": 50, "potato mine": 25, "snow pea": 175,
    "chomper": 150, "repeater": 200, "puff-shroom": 0, "sun-shroom": 25, "fume-shroom": 75, "grave buster": 75,
    "hypno-shroom": 75, "scaredy-shroom": 25, "ice-shroom": 75, "doom-shroom": 125, "lily pad": 25, "squash": 50,
    "threepeater": 325, "tangle kelp": 25, "jalapeno": 125, "spikeweed": 100, "torchwood": 175, "tall-nut": 125,
    "sea-shroom": 0, "plantern": 25, "cactus": 125, "blover": 100, "split pea": 125, "starfruit": 125,
    "pumpkin": 125, "magnet-shroom": 100, "cabbage-pult": 100, "flower pot": 25, "kernel-pult": 100,
    "garlic": 50, "umbrella leaf": 100, "marigold": 50, "melon-pult": 300,
}
# Single-use plants: wasted unless a zombie is within reach in their row.
INSTANT_KILLS = {"cherry bomb", "jalapeno", "squash", "doom-shroom", "tangle kelp"}
INSTANT_KILL_REACH = 1  # columns between the plant and the row's nearest zombie
# Traps arm after a delay (potato mine ~15 s; a normal zombie walks a column in ~4.7 s): wasted unless
# the row's nearest zombie is still far enough right of the trap to arrive after it is armed. One
# placed at the zombie's own column is behind it by then (real run, 2026-10-29).
TRAPS = {"potato mine"}
TRAP_REACH = (3, 6)  # columns the nearest zombie may stand right of the trap
BASES = {"lily pad", "flower pot"}  # carry one more plant on top (water rows, roof)


def cell_free_for(plant: str, occupants: list[str]) -> bool:
    return not occupants or (set(occupants) <= BASES and plant not in BASES)


def check(p: Placement, scene: Scene, planted: dict[tuple[int, int], list[str]], used_slots: set[int],
          sun_spent: int = 0) -> str | None:
    """None if the placement is allowed, else the reason it is not.

    `planted` is the lawn as last seen plus what the agent placed since; never the agent's memory alone,
    which cannot know about plants that were eaten or were there before it started.
    """
    rows = scene.rows or 5
    if not (0 <= p.row < rows and 0 <= p.col < COLS):
        return f"cell ({p.row}, {p.col}) is outside a {rows}-row lawn"
    if p.slot >= len(scene.cards):
        return f"no seed slot {p.slot}"
    card = scene.cards[p.slot]
    occupants = planted.get((p.row, p.col), [])
    if not cell_free_for(card.plant, occupants):
        return f"cell ({p.row}, {p.col}) already has {', '.join(occupants)}"
    if not card.ready:
        return f"{card.plant} card is not usable"
    if p.slot in used_slots:
        return f"{card.plant} card was already used since the last look"
    cost = PLANT_COSTS.get(card.plant)
    if scene.sun is not None and cost is not None and scene.sun - sun_spent < cost:
        return f"{card.plant} costs {cost}, sun left is {scene.sun - sun_spent}"
    nearest = scene.nearest_col.get(p.row)
    if card.plant in INSTANT_KILLS:
        if nearest is None or abs(p.col - nearest) > INSTANT_KILL_REACH:
            return f"{card.plant} at column {p.col} has no zombie within reach in row {p.row}"
    if card.plant in TRAPS:
        lo, hi = TRAP_REACH
        if nearest is None or not lo <= nearest - p.col <= hi:
            return (f"{card.plant} at column {p.col} needs the row's nearest zombie {lo}-{hi} columns to its "
                    f"right (row {p.row}: {'none' if nearest is None else f'column {nearest}'})")
    return None
