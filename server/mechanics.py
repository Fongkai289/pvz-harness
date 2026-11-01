"""Public static game reference. No tracking, judge data or action authority."""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

from rules.hard_rules import PLANT_COSTS

PATH = Path(__file__).resolve().parent.parent / "knowledge" / "pvz_mechanics.json"
DEFAULT_PLANTS = ("peashooter", "sunflower", "cherry bomb", "wall-nut", "potato mine", "snow pea")


@lru_cache(maxsize=1)
def _catalog():
    data = json.loads(PATH.read_text(encoding="utf-8"))
    for name, cost in PLANT_COSTS.items():
        entry = data["plants"].setdefault(name, {"detail_status": "cost_only; other stats unverified"})
        entry["sun_cost"] = cost
    return data


def catalog():
    return deepcopy(_catalog())


def mechanics_context(scene=None, planted=None):
    """Include only relevant species, never infer which damage/phase applies now."""
    data = _catalog()
    plant_names = set(DEFAULT_PLANTS) if scene is None else {c.plant for c in scene.cards}
    for cell in (planted or {}).values():
        plant_names.update(cell)
    zombie_names = {z.type for z in scene.zombies} if scene is not None else {"normal"}
    unknown = {"detail_status": "unverified", "full_hp": None}
    return deepcopy({
        "version": data["version"], "scope": data["scope"],
        "kind": "static_reference_not_live_state", "cautions": data["cautions"],
        "units": data["units"],
        "plants": {name: {k: v for k, v in data["plants"].get(name, unknown).items() if k != "sources"}
                   for name in sorted(plant_names)},
        "zombies": {name: data["zombies"].get(name, unknown) for name in sorted(zombie_names)},
    })
