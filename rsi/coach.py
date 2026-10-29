"""Coach: reads dev-set results (including ground truth) and proposes one playbook change.

Privileged side: it sees the judge's counters and every click's true outcome. It never
sees held-out results, and its proposal only reaches the player after it passes the
fixed rules and beats the current version on the dev seeds.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from rules.playbook_rules import (
    EDITABLE_SECTIONS,
    FastParams,
    JevPolicy,
    PerceptionParams,
    PlannerKnowledge,
    Playbook,
)
from server import perception
from server.config import Settings

log = logging.getLogger(__name__)


class Proposal(BaseModel):
    """Only the sections being changed; an omitted (null) section is kept exactly as it is.

    Asked to copy unchanged sections back, the coach rewrote long text it meant to keep
    (2026-10-28), so unchanged sections are left out instead of retyped.
    """

    perception: PerceptionParams | None = None
    fast: FastParams | None = None
    jev: JevPolicy | None = None
    planner: PlannerKnowledge | None = None
    rationale: str = Field(description="Which failure this targets and why the change should fix it")

    def apply(self, current: Playbook, version: str) -> Playbook:
        changed = {s: getattr(self, s) for s in EDITABLE_SECTIONS if getattr(self, s) is not None}
        return Playbook.model_validate({**current.model_dump(), **{k: v.model_dump() for k, v in changed.items()},
                                        "version": version, "parent": current.version, "notes": self.rationale})


SECTION_READERS = {
    name: " ".join((field.annotation.__doc__ or "").split())
    for name, field in Playbook.model_fields.items() if name in EDITABLE_SECTIONS
}

COACH_PROMPT = """You are the coach of a game-playing agent. Between games you read what happened and
improve the agent's playbook. You cannot play, and you cannot change how games are scored.

The game is described under "game" in the input. The agent sees only screenshots.

The playbook has sections, each read by one part of the player during play (listed under
"playbook_sections"); the field descriptions and allowed ranges are in "playbook_schema". The
"player_profile" says which models play, what they are given, and what perception can recognise.
Write each section for its reader: numbers for code, short concrete sentences for models. Knowledge
that depends on something perception cannot recognise yet cannot be acted on.

You get ground truth the player never sees: totals over the development games and, for every sun
click, whether it hit a sun, a decoy, or nothing, with the hue and area perception measured for that blob.

Propose ONE change that targets the largest loss you can see. Include only the section(s) you change,
written out in full; leave every other section null, and it stays exactly as it is. Change only what the
evidence supports. Earlier candidates and their results are listed so you do not repeat a change that
already failed."""


GAMES = {
    "suns": ("Suns fall on a lawn and must be clicked before they expire. Score per game = (suns collected - "
             "0.5 x wasted clicks) / suns spawned. Counters: spawned, collected, expired; *_small = suns drawn "
             "smaller than usual; decoy_clicks = clicks on sun-like decoys."),
    "lawn": ("A small Plants vs. Zombies: zombies walk in from the right on scheduled waves and eat plants; "
             "peashooters shoot, potato mines and cherry bombs kill outright, wall-nuts block; one lawnmower per "
             "row clears it once, a zombie reaching the house a second time loses. Score per game = 0.5 x share "
             "of scheduled zombies killed + 0.5 for a win - 0.1 per lawnmower used. Counters: killed, scheduled, "
             "mowers_used, plants_placed, plants_eaten, sun_collected, outcome_* (win, lose, timeout)."),
}


def player_profile(s: Settings) -> dict[str, Any]:
    planner_model = {"qwen": s.qwen_model, "claude": s.planner_model}.get(s.planner, s.planner)
    return {
        "planner": {"model": planner_model, "every_seconds": s.plan_interval,
                    "input": "screenshot" if s.planner_send_image else "perception summary text only"},
        "jev": {"every_seconds": s.fast_interval,
                "tactics": ("playbook jev section: the first action in `priorities` that applies and can be placed "
                            "now; which card (role lists, in order), which row and which column (the *_cols, "
                            "offsets and wall_* fields) also come from that section"),
                "judgment_model": s.onejev_model if s.decider == "onejev" else None,
                "judgment": "yes/no per row with zombies: is any of them tough (playbook jev.tough_question)",
                "hard_rules": "fixed, not editable: cell free and on the lawn, card usable and not just used, "
                              "enough sun, instant kills only within 1 column of a zombie"},
        "look_step": {"every_seconds": s.look_interval if s.planner_mode == "split" else None,
                      "code_reads": "card usable or not, lawn type",
                      "model_reads": "sun number, zombie types and nearest column per row"},
        "loadout": s.loadout,
        "perception_can_recognise": list(perception.CAPABILITIES),
    }


def history_lines(ledger: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    rows = [e for e in ledger if e.get("kind") == "candidate"][-limit:]
    return [{"candidate": e["candidate"], "parent": e["parent"], "changes": e.get("changes"),
             "parent_mean": e.get("parent_mean"), "candidate_mean": e.get("candidate_mean"),
             "promoted": e.get("promoted"), "reason": e.get("reason")} for e in rows]


def _short(v: Any) -> Any:
    return v if not isinstance(v, str) or len(v) <= 160 else v[:157] + "..."


def diff(current: Playbook, proposal: Proposal) -> dict[str, list]:
    """Changed fields as 'section.field': [old, new]; long text is shortened for the ledger."""
    changes = {}
    for section in EDITABLE_SECTIONS:
        if getattr(proposal, section) is None:
            continue
        old = getattr(current, section).model_dump()
        new = getattr(proposal, section).model_dump()
        for k in new:
            if old[k] != new[k]:
                changes[f"{section}.{k}"] = [_short(old[k]), _short(new[k])]
    return changes


async def propose(current: Playbook, dev: dict[str, Any], ledger: list[dict[str, Any]], s: Settings,
                  timeout: float = 120) -> tuple[Proposal | None, str | None]:
    user = {
        "game": GAMES.get(dev.get("game", "suns"), ""),
        "player_profile": player_profile(s),
        "playbook_sections": SECTION_READERS,
        "playbook_schema": Proposal.model_json_schema(),
        "current_playbook": current.model_dump(include=set(EDITABLE_SECTIONS)),
        "development_results": dev,
        "earlier_candidates": history_lines(ledger),
    }
    body = {
        "model": s.qwen_model,
        "max_tokens": 4096,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": COACH_PROMPT},
                     {"role": "user", "content": json.dumps(user, indent=1, ensure_ascii=False)}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "proposal", "schema": Proposal.model_json_schema()}},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(s.qwen_url, json=body)
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return Proposal.model_validate_json(content), None
    except httpx.HTTPError as e:
        return None, f"coach call failed: {type(e).__name__}: {e}"
    except ValidationError as e:  # includes values outside the fixed rules' ranges
        return None, f"proposal rejected by rules: {e.errors()[:3]}"
    except (KeyError, IndexError, ValueError) as e:
        return None, f"bad coach response: {e!r}"
