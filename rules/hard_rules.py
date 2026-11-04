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
BASES = {"lily pad", "flower pot"}  # carry one more plant on top (water rows, roof)


def card_cost(card):
    return card.cost if getattr(card,'cost',None) is not None else PLANT_COSTS.get(card.plant)


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
    if p.slot < 0 or p.slot >= len(scene.cards) or scene.cards[p.slot].slot != p.slot:
        return f"no seed slot {p.slot}"
    card = scene.cards[p.slot]
    if card.plant == "unknown":
        return f"seed slot {p.slot} was not recognized on screen"
    occupants = planted.get((p.row, p.col), [])
    if not cell_free_for(card.plant, occupants):
        return f"cell ({p.row}, {p.col}) already has {', '.join(occupants)}"
    if not card.ready:
        return f"{card.plant} card is not usable"
    if p.slot in used_slots:
        return f"{card.plant} card was already used since the last look"
    cost = card_cost(card)
    if scene.sun is not None and cost is not None and scene.sun - sun_spent < cost:
        return f"{card.plant} costs {cost}, sun left is {scene.sun - sun_spent}"
    return None


def check_shovel(row: int, col: int, scene: Scene, planted: dict[tuple[int, int], list[str]]) -> str | None:
    """None if digging up the plant in this cell is allowed, else the reason it is not.

    The game's shovel removes the top plant of the cell and refunds nothing; it cannot dig graves.
    """
    rows = scene.rows or 5
    if not (0 <= row < rows and 0 <= col < COLS):
        return f"cell ({row}, {col}) is outside a {rows}-row lawn"
    if scene.shovel is not True:
        return "no shovel in its box"
    occupants = planted.get((row, col), [])
    if not occupants:
        return f"cell ({row}, {col}) has no plant to dig up"
    if "grave buster" in occupants:
        return "the shovel cannot dig up a grave buster"
    return None
