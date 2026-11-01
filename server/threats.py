"""Observed row facts and changes. Urgency is assessed in model messages."""
ATTACKERS = {"peashooter", "repeater", "snow pea", "threepeater", "split pea", "starfruit",
             "cactus", "chomper", "puff-shroom", "fume-shroom", "scaredy-shroom",
             "sea-shroom", "cabbage-pult", "kernel-pult", "melon-pult"}
WALLS = {"wall-nut", "tall-nut", "pumpkin"}


def observed_threats(scene, planted, layout_fresh):
    rows = []
    for row in range(scene.rows or 5):
        zombies = [z for z in scene.zombies if z.row == row]
        nearest = scene.nearest_col.get(row)
        # These are conservative coverage hints, not a DPS or time-to-defeat estimate.
        in_front = [p for (r, c), cell in planted.items() if r == row
                    and (nearest is None or c <= nearest) for p in cell]
        attacks = sum(p in ATTACKERS for p in in_front) if layout_fresh else None
        walls = sum(p in WALLS for p in in_front) if layout_fresh else None
        rows.append(dict(row=row, types=[z.type for z in zombies], nearest_col=nearest,
                         attackers=attacks, walls=walls, layout_fresh=layout_fresh))
    return rows


def threat_changes(previous, current):
    before = {r["row"]: r for r in previous}
    changes = {}
    for row in current:
        r = row["row"]
        old = before.get(r, {})
        reasons = []
        if sorted(row["types"]) != sorted(old.get("types", [])):
            reasons.append("观察到的僵尸类型或数量变化")
        if row["types"] and row["attackers"] == 0 and (not old.get("types") or old.get("attackers") != 0):
            reasons.append("出现无攻击植物覆盖的僵尸")
        if old.get("nearest_col") is not None and row["nearest_col"] is not None and row["nearest_col"] < old["nearest_col"]:
            reasons.append("最近僵尸位置向房屋移动")
        if row["layout_fresh"] and old.get("layout_fresh") and any(
            old.get(k, 0) > 0 and row[k] == 0 for k in ("attackers", "walls")):
            reasons.append("观察到防线消失或僵尸已越过防线")
        if reasons:
            changes[str(r)] = f"第{r + 1}行：" + "；".join(reasons)
    return changes
