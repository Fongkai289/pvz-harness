"""Fixed authority for System0 (player_code) outputs. Not coach-editable.

player_code may name Jev's alternatives (options), turn the chosen one into concrete actions
(act) and run reflexes every 0.25 s (reflex). Whatever it returns passes this gate first:
only three action types exist, each call has a fixed action limit, a sun pickup can only refer to
a target the host already confirmed (never coordinates), every plant still has to pass
DecisionContext.reject() (freshness, hard rules, native mask) and every shovel
DecisionContext.reject_shovel() where it is executed. A shovel runs before the plant of the same call,
so "dig up this cell and replant it" is one decision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MENU_LIMIT = 24
MENU_TAGS = ("defense", "economy", "reinforce", "save_sun")
DECISION_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")
RESERVED_IDS = frozenset({"wait"})
# entry -> maximum actions of each type in one call
LIMITS = {"act": {"plant": 1, "collect_sun": 0, "shovel": 1}, "reflex": {"plant": 1, "collect_sun": 2, "shovel": 0},
          "fallback": {"plant": 1, "collect_sun": 0, "shovel": 1}}


@dataclass(frozen=True)
class MenuItem:
    id: str
    text: str
    tags: tuple[str, ...] = ()
    rows: tuple[int, ...] = ()
    params: dict = field(default_factory=dict, compare=False)

    def public(self) -> dict:
        """What Planner, Jev and the logs see; params stay between options() and act()."""
        return {"id": self.id, "text": self.text, "tags": list(self.tags), "rows": list(self.rows)}

    def call_argument(self) -> dict:
        return {**self.public(), "params": self.params}


@dataclass(frozen=True)
class PlantAction:
    slot: int
    row: int
    col: int


@dataclass(frozen=True)
class SunAction:
    index: int


@dataclass(frozen=True)
class ShovelAction:
    row: int
    col: int


def split_actions(actions) -> tuple[ShovelAction | None, PlantAction | None]:
    """act()/fallback() results: the shovel (executed first) and the plant, each at most one."""
    return (next((a for a in actions if isinstance(a, ShovelAction)), None),
            next((a for a in actions if isinstance(a, PlantAction)), None))


def _int(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer in {low}..{high}")
    return value


def parse_menu(raw, rows: int) -> list[MenuItem]:
    if type(raw) is not list or len(raw) > MENU_LIMIT:
        raise ValueError(f"options() must return a list of at most {MENU_LIMIT} decisions")
    menu, seen = [], set()
    for entry in raw:
        if type(entry) is not dict or not {"id", "text"} <= set(entry) <= {"id", "text", "tags", "rows", "params"}:
            raise ValueError("a decision has keys id, text and optional tags, rows, params")
        ident, text = entry["id"], entry["text"]
        if type(ident) is not str or not DECISION_ID.fullmatch(ident) or ident in RESERVED_IDS or ident in seen:
            raise ValueError(f"invalid or repeated decision id: {ident!r}")
        if type(text) is not str or not 0 < len(text) <= 160:
            raise ValueError("decision text must be 1-160 characters")
        tags = entry.get("tags", [])
        if type(tags) is not list or any(t not in MENU_TAGS for t in tags) or len(set(tags)) != len(tags):
            raise ValueError(f"decision tags must be distinct values of {MENU_TAGS}")
        rows_used = entry.get("rows", [])
        if type(rows_used) is not list or len(set(rows_used)) != len(rows_used):
            raise ValueError("decision rows must be a list of distinct rows")
        for row in rows_used:
            _int(row, 0, rows - 1, "decision row")
        params = entry.get("params", {})
        if type(params) is not dict:
            raise ValueError("decision params must be an object")
        seen.add(ident)
        menu.append(MenuItem(ident, text, tuple(tags), tuple(rows_used), params))
    return menu


def parse_actions(raw, entry: str, *, suns: int,
                  allow_planting: bool = False) -> list[PlantAction | SunAction | ShovelAction]:
    """Structural authority only; plants and shovels are re-checked against the hard rules on execution."""
    limits = LIMITS[entry]
    if type(raw) is not list or len(raw) > sum(limits.values()):
        raise ValueError(f"{entry}() returned too many actions")
    actions, counts, picked = [], {"plant": 0, "collect_sun": 0, "shovel": 0}, set()
    for action in raw:
        if type(action) is not dict or action.get("type") not in counts:
            raise ValueError("actions are {type: plant, slot, row, col}, {type: shovel, row, col} "
                             "or {type: collect_sun, sun}")
        kind = action["type"]
        counts[kind] += 1
        if counts[kind] > limits[kind]:
            raise ValueError(f"{entry}() may return at most {limits[kind]} {kind} action(s)")
        if kind == "plant":
            if set(action) != {"type", "slot", "row", "col"}:
                raise ValueError("plant action keys are type, slot, row, col")
            if entry == "reflex" and not allow_planting:
                raise ValueError("reflex planting is disabled by playbook.reflex.allow_planting")
            actions.append(PlantAction(_int(action["slot"], 0, 9, "slot"), _int(action["row"], 0, 5, "row"),
                                       _int(action["col"], 0, 8, "col")))
        elif kind == "shovel":
            if set(action) != {"type", "row", "col"}:
                raise ValueError("shovel action keys are type, row, col")
            actions.append(ShovelAction(_int(action["row"], 0, 5, "row"), _int(action["col"], 0, 8, "col")))
        else:
            if set(action) != {"type", "sun"}:
                raise ValueError("collect_sun action keys are type, sun")
            index = _int(action["sun"], 0, max(suns - 1, 0), "sun index") if suns else None
            if index is None or index in picked:
                raise ValueError("collect_sun must name a distinct confirmed sun")
            picked.add(index)
            actions.append(SunAction(index))
    return actions
