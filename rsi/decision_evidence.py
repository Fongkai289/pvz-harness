"""Join player observations with decisions for review/replay; never use judge state."""
from copy import deepcopy

from rules.hard_rules import PLANT_COSTS


def decision_snapshots(events):
    resources, last_plants, last_zombies = {}, {}, None
    choices, issued, result = {}, [], []
    for index, event in enumerate(events):
        kind = event.get("kind")
        if kind == "scene" and event.get("scene"):
            resources[event["scene"]["frame_id"]] = event["scene"]
        elif kind == "plants" and event.get("plants") is not None:
            last_plants = event
        elif kind == "zombies" and event.get("scene"):
            last_zombies = event["scene"]
        elif kind == "cmd" and event.get("reason", "").startswith("plant at"):
            choice = choices.get((event.get("meta") or {}).get("decision_id"))
            if choice:
                issued.append((event.get("frame_id", 0), choice))
        elif kind == "jev":
            if event.get("placement"):
                choices[event.get("decision_id")] = event["placement"]
            context = deepcopy(event.get("decision_context"))
            exact = context is not None
            if context is None:
                scene = deepcopy(resources.get(event.get("scene_frame")))
                if scene is None:
                    continue
                if last_zombies and last_zombies.get("rows") == scene.get("rows"):
                    scene.update({k: last_zombies[k] for k in ("zombies", "nearest_col")})
                plants = {tuple(map(int, cell.split(","))): list(ps) for cell, ps in last_plants.get("plants", {}).items()}
                spent, used = 0, set()
                for frame, placement in issued:
                    slot = placement["slot"]
                    if slot >= len(scene["cards"]):
                        continue
                    plant = scene["cards"][slot]["plant"]
                    if frame >= scene["frame_id"]:
                        used.add(slot)
                        spent += PLANT_COSTS.get(plant, 0)
                    if frame >= last_plants.get("frame_id", -1):
                        cell = plants.setdefault((placement["row"], placement["col"]), [])
                        if plant not in cell:
                            cell.append(plant)
                context = {"scene": scene, "plants": [{"row": r, "col": c, "plants": ps} for (r,c),ps in sorted(plants.items())],
                           "sun_spent_since_scene": spent, "used_slots": sorted(used), "layout_fresh": bool(last_plants)}
            result.append({"ref": f"E{index}", "decision_id": event.get("decision_id"),
                           "provenance": "recorded_request_context" if exact else "reconstructed_from_prior_player_logs; timing may differ",
                           "guidance": event.get("guidance"), "committed_spend": event.get("budget_spent", 0),
                           "decision_context": context, "placement": event.get("placement"),
                           "action": event.get("action"), "rejected": event.get("rejected")})
    return result
