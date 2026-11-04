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


def _number(entry, *keys):
    for key in keys:
        if isinstance(entry.get(key), (int, float)):
            return entry[key]
    return None


def player_reference(scene, plants):
    """Per-species constants for System0 code, so it can turn positions into rough times.

    Full-health values and the fastest listed movement phase; never live HP, timers or positions.
    Unverified values stay null.
    """
    data = _catalog()
    plant_names = {c.plant for c in scene.cards} | {p for cell in plants for p in cell.get("plants", ())}
    plant_ref = {}
    for name in sorted(plant_names):
        entry = data["plants"].get(name, {})
        damage, interval = _number(entry, "damage_per_projectile"), _number(entry, "attack_interval_s")
        damage = damage * (_number(entry, "projectiles_per_attack") or 1) if damage else damage
        sun, cycle = _number(entry, "sun_per_cycle"), _number(entry, "production_interval_s_approx")
        plant_ref[name] = {"cost": _number(entry, "sun_cost"), "hp": _number(entry, "full_hp"),
                           "dps": round(damage / interval, 2) if damage and interval else None,
                           "sun_per_cycle": sun, "cycle_s": cycle if sun else None}
    zombie_ref = {}
    for name in sorted({z.type for z in scene.zombies}):
        entry = data["zombies"].get(name, {})
        phases = entry.get("seconds_per_tile")
        speeds = [v for v in phases.values() if isinstance(v, (int, float))] if isinstance(phases, dict) else []
        zombie_ref[name] = {"hp": _number(entry, "full_hp"), "chew_dps": _number(entry, "chew_dps"),
                            "s_per_tile": min(speeds) if speeds else None}
    return {"plants": plant_ref, "zombies": zombie_ref}


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
