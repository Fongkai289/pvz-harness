"""Observed row facts for model context. Urgency is assessed in model messages; whether a
plan needs replacing is decided by System0 against the plan's replan_when."""
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

