"""Which coach lessons hold on the current lawn. Player observations only, never judge data."""
from rules.lessons import evaluate
from server.threats import ATTACKERS


def active_lessons(book, scene, planted, sun) -> tuple[list[dict], dict[str, str]]:
    """(shown [{id, then}] in listed order up to max_shown, errors {id: message}) for one model call."""
    if not book.items:
        return [], {}
    rows = scene.rows or 5
    facts = {"sun": sun, "zombies": len(scene.zombies), "rows": rows}

    def row_of(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < rows else None

    def nearest(row):
        if row_of(row) in scene.nearest_col:
            return scene.nearest_col[row_of(row)]
        cols = [z.col for z in scene.zombies if z.row == row_of(row) and z.col is not None]
        return min(cols) if cols else None

    functions = {
        "has_zombie": lambda kind: any(z.type == kind for z in scene.zombies),
        "zombie_count": lambda kind: sum(z.type == kind for z in scene.zombies),
        "zombies_in_row": lambda row: sum(z.row == row_of(row) for z in scene.zombies),
        "nearest_col": nearest,
        "plant_count": lambda name: sum(cell.count(name) for cell in planted.values()),
        "attackers_in_row": lambda row: sum(p in ATTACKERS for (r, _), cell in planted.items()
                                            if r == row_of(row) for p in cell),
        "has_card": lambda name: any(c.plant == name for c in scene.cards),
        "card_ready": lambda name: any(c.plant == name and c.ready for c in scene.cards),
    }
    shown, errors = [], {}
    for lesson in book.items:
        try:
            holds = evaluate(lesson.when, facts, functions)
        except (TypeError, ValueError, ArithmeticError) as exc:
            errors[lesson.id] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        if holds and len(shown) < book.max_shown:
            shown.append({"id": lesson.id, "then": lesson.then})
    return shown, errors
