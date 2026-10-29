"""Jev: the fast decider, about once a second, in three layers.

1. judgments (model): OneJev answers one fuzzy yes/no question per row with zombies: is any of
   them tough for ordinary peashooters? Its state is facts only, no rule text: given rule text,
   OneJev-0.8B took "a zombie at column 3 or lower is close" as a fact about a calm lawn.
2. tactics (playbook `jev`): the first action in priority order whose condition holds. Code
   compares the numbers; models never do (0.8B misjudged "column 7 <= 3").
3. hard rules (rules/hard_rules.py): applied by the session to whatever placement comes out.

When asked to choose the action itself, OneJev picked "emergency" for calm scenes (0.8B always;
27B 1 of 3); asked single yes/no facts, 27B was 6/6 correct (2026-10-28).

Must never import server.judge or read StateMsg (tests/test_isolation.py).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import httpx

from rules import hard_rules
from rules.playbook_rules import JevPolicy
from server import geometry
from server.planner import Placement
from server.scene import Scene

if TYPE_CHECKING:
    from server.config import Settings

log = logging.getLogger(__name__)


@dataclass
class JevContext:
    scene: Scene
    plan_next: Placement | None
    planted: dict[tuple[int, int], list[str]]  # (row, col) -> plants: last seen lawn + own placements since
    used_slots: set[int] = field(default_factory=set)  # cards used since the scene was taken

    def ready_slots(self, plants: list[str]) -> list[int]:
        """Usable cards for a role, in the order of the role's plant list (its preference)."""
        usable = {c.plant: c.slot for c in self.scene.cards if c.ready and c.slot not in self.used_slots}
        return [usable[p] for p in plants if p in usable]

    def plant(self, slot: int) -> str:
        return self.scene.cards[slot].plant

    def row_has(self, row: int, plants: list[str]) -> bool:
        return any(r == row and p in plants for (r, _), cell in self.planted.items() for p in cell)

    def count(self, plants: list[str]) -> int:
        return sum(p in plants for cell in self.planted.values() for p in cell)


# ---- layer 1: judgments ----


@dataclass
class Judgments:
    tough: dict[int, float] = field(default_factory=dict)  # row -> probability of "yes"
    source: str = "none"  # "onejev" | "none" | "failed"
    latency: float = 0.0
    error: str | None = None

    def is_tough(self, row: int) -> bool:
        return self.tough.get(row, 0.0) >= 0.5


class Judge(Protocol):
    async def judge(self, ctx: JevContext, policy: JevPolicy) -> Judgments: ...


class NoJudge:
    """No model: no row is judged tough, so only the plain thresholds apply."""

    async def judge(self, ctx: JevContext, policy: JevPolicy) -> Judgments:
        return Judgments()


def facts(scene: Scene, planted: dict[tuple[int, int], list[str]]) -> str:
    """Facts only (no rules, no thresholds): what is on the lawn and what the agent placed."""
    lines = [f"Lawn: {scene.lawn or 'unknown'}, {scene.rows or '?'} rows, 9 columns "
             "(column 0 is next to the house)."]
    for row in range(scene.rows or 5):
        types = [z.type for z in scene.zombies if z.row == row]
        mine = [f"{' on '.join(reversed(cell))} at column {c}" for (r, c), cell in sorted(planted.items()) if r == row]
        if types or mine:
            where = f", nearest at column {scene.nearest_col[row]}" if row in scene.nearest_col else ""
            lines.append(f"Row {row}: zombies {', '.join(types) or 'none'}{where}; "
                         f"plants {', '.join(mine) or 'none'}.")
    return "\n".join(lines)


class OneJevJudge:
    def __init__(self, url: str, model: str, timeout: float, client: httpx.AsyncClient | None = None) -> None:
        self.url, self.model = url, model
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def judge(self, ctx: JevContext, policy: JevPolicy) -> Judgments:
        rows = sorted({z.row for z in ctx.scene.zombies if z.type != "unknown"})
        if not rows:
            return Judgments(source="onejev")
        start = time.monotonic()
        questions = {
            f"tough_row_{r}": {"type": "noul", "instructions": policy.tough_question.format(
                types=", ".join(z.type for z in ctx.scene.zombies if z.row == r))}
            for r in rows
        }
        try:
            response = await self.client.post(self.url, json={
                "model": self.model, "state": facts(ctx.scene, ctx.planted), "questions": questions})
            response.raise_for_status()
            answers = response.json()["answers"]
            tough = {r: float(answers[f"tough_row_{r}"]["noul"]) for r in rows}
        except httpx.HTTPStatusError as e:
            return self._failed(start, f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except httpx.HTTPError as e:
            return self._failed(start, f"{type(e).__name__}: {e}")
        except (KeyError, TypeError, ValueError) as e:
            return self._failed(start, f"bad response: {e!r}")
        return Judgments(tough=tough, source="onejev", latency=time.monotonic() - start)

    @staticmethod
    def _failed(start: float, error: str) -> Judgments:
        log.warning("Jev judgment failed, no row counts as tough: %s", error)
        return Judgments(source="failed", latency=time.monotonic() - start, error=error)


# ---- layer 2: tactics ----
#
# Each action returns (why it applies, the placement it would make). An action wins only with both, so
# "applies but cannot be placed" can no longer starve the actions after it (2026-10-28: reinforce was
# chosen 32 times and placed once in a fake-lawn game). All numbers and orderings come from the playbook.

Proposal = tuple[str | None, Placement | None]


def _free(ctx: JevContext, row: int, cols, plant: str) -> int | None:
    return next((c for c in cols if 0 <= c < geometry.COLS
                 and hard_rules.cell_free_for(plant, ctx.planted.get((row, c), []))), None)


def _closest(ctx: JevContext) -> list[tuple[int, int]]:
    return sorted(ctx.scene.nearest_col.items(), key=lambda kv: kv[1])


def _shooters(ctx: JevContext, row: int, policy: JevPolicy) -> int:
    return sum(p in policy.attack_plants for (r, _), cell in ctx.planted.items() if r == row for p in cell)


def emergency(ctx: JevContext, policy: JevPolicy, j: Judgments) -> Proposal:
    slots = ctx.ready_slots(policy.emergency_plants)
    if not slots:
        return None, None
    for row, col in _closest(ctx):
        if col <= policy.danger_col:
            why = f"zombie at column {col} in row {row} (danger_col {policy.danger_col})"
        elif j.is_tough(row) and col <= policy.tough_danger_col:
            why = f"tough zombie at column {col} in row {row} (tough_danger_col {policy.tough_danger_col})"
        else:
            continue
        target = _free(ctx, row, [min(col, 8) + o for o in policy.emergency_offsets], ctx.plant(slots[0]))
        return why, Placement(slot=slots[0], row=row, col=target) if target is not None else None
    return None, None


def attack(ctx: JevContext, policy: JevPolicy, j: Judgments) -> Proposal:
    slots = ctx.ready_slots(policy.attack_plants)
    if not slots:
        return None, None
    threatened = [r for r, _ in _closest(ctx)]
    rows = threatened + ([r for r in range(ctx.scene.rows or geometry.ROWS) if r not in threatened]
                         if policy.attack_rows == "all" else [])
    why = None
    for row in rows:
        want = policy.shooters_per_tough_row if j.is_tough(row) else policy.shooters_per_row
        have = _shooters(ctx, row, policy)
        if have >= want:
            continue
        why = why or f"row {row} has {have} shooter(s), wants {want}"
        target = _free(ctx, row, policy.attack_cols, ctx.plant(slots[0]))
        if target is not None:
            return f"row {row} has {have} shooter(s), wants {want}", Placement(slot=slots[0], row=row, col=target)
    return why, None


def reinforce(ctx: JevContext, policy: JevPolicy, j: Judgments) -> Proposal:
    slots = ctx.ready_slots(policy.wall_plants)
    if not slots:
        return None, None
    why = None
    for row, col in _closest(ctx):
        if col > policy.wall_trigger_col or ctx.row_has(row, policy.wall_plants):
            continue
        why = why or f"row {row} has a zombie at column {col} and no wall"
        cols = range(min(col - policy.wall_gap, policy.wall_max_col), policy.wall_min_col - 1, -1)
        target = _free(ctx, row, cols, ctx.plant(slots[0]))
        if target is not None:
            return f"row {row} has a zombie at column {col} and no wall", Placement(slot=slots[0], row=row, col=target)
    return why, None


def follow_plan(ctx: JevContext, policy: JevPolicy, j: Judgments) -> Proposal:
    return ("the planner has a next step", ctx.plan_next) if ctx.plan_next is not None else (None, None)


def economy(ctx: JevContext, policy: JevPolicy, j: Judgments) -> Proposal:
    slots = ctx.ready_slots(policy.economy_plants)
    if not slots or any(col <= policy.economy_safe_col for col in ctx.scene.nearest_col.values()):
        return None, None
    have = ctx.count(policy.economy_plants)
    if have >= policy.economy_target:
        return None, None
    why = f"no zombie within column {policy.economy_safe_col}, {have} economy plants"
    per_row = lambda r: sum(p in policy.economy_plants for (pr, _), cell in ctx.planted.items() if pr == r for p in cell)
    for row in sorted(range(ctx.scene.rows or geometry.ROWS), key=per_row):
        col = _free(ctx, row, policy.economy_cols, ctx.plant(slots[0]))
        if col is not None:
            return why, Placement(slot=slots[0], row=row, col=col)
    return why, None


def wait(ctx: JevContext, policy: JevPolicy, j: Judgments) -> Proposal:
    return "nothing else applies", None


ACTIONS = {"emergency": emergency, "attack": attack, "reinforce": reinforce, "follow_plan": follow_plan,
           "economy": economy, "wait": wait}


def choose(ctx: JevContext, policy: JevPolicy, j: Judgments) -> tuple[str, str, Placement | None]:
    """The first action in priority order that applies and can be placed now (or wait)."""
    skipped = []
    for action in policy.priorities:
        why, place = ACTIONS[action](ctx, policy, j)
        if why is None:
            continue
        if action == "wait" or place is not None:
            return action, why + (f" (skipped, no placement: {', '.join(skipped)})" if skipped else ""), place
        skipped.append(action)
    return "wait", "nothing else applies", None


def make_judge(s: Settings) -> Judge:
    if s.decider == "onejev":
        return OneJevJudge(s.onejev_url, s.onejev_model, timeout=s.decider_timeout)
    if s.decider == "rules":
        return NoJudge()
    raise ValueError(f"unknown decider: {s.decider}")
