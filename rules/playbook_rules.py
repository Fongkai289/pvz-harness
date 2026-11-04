"""Fixed rules: what a playbook may contain and the allowed ranges.

The coach edits playbooks; it never edits this file, the judge, or the test seeds. A
candidate that fails validation is rejected before it is ever played.

Each section has one reader during play (named in its docstring); the coach reads and
edits all of them. Sections added later carry defaults, so older versions still load.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rules.lessons import parse_condition, vocabulary


class PerceptionParams(BaseModel):
    """Read by the perception code (pixels -> sun blobs)."""

    model_config = ConfigDict(extra="forbid")

    sun_hsv_lo: tuple[int, int, int] = Field(description="OpenCV HSV lower bound (H 0-179, S/V 0-255)")
    sun_hsv_hi: tuple[int, int, int] = Field(description="OpenCV HSV upper bound")
    sun_min_area: int = Field(ge=50, le=5000, description="smallest blob, in pixels, counted as a sun")
    sun_max_area: int = Field(ge=100, le=20000, description="largest blob, in pixels, counted as a sun")

    @model_validator(mode="after")
    def _ranges(self) -> PerceptionParams:
        for lo, hi, top in zip(self.sun_hsv_lo, self.sun_hsv_hi, (179, 255, 255)):
            if not 0 <= lo <= hi <= top:
                raise ValueError(f"HSV bounds out of order or range: {self.sun_hsv_lo} / {self.sun_hsv_hi}")
        if self.sun_min_area >= self.sun_max_area:
            raise ValueError("sun_min_area must be below sun_max_area")
        return self


class FastParams(BaseModel):
    """Read by the fast policy code (sun clicks, placement execution)."""

    model_config = ConfigDict(extra="forbid")

    sun_click_cooldown: float = Field(ge=0.2, le=10.0, description="seconds before re-clicking the same spot")
    sun_same_spot_px: int = Field(ge=5, le=100, description="clicks closer than this count as the same spot")


JEV_ACTIONS = ("emergency", "attack", "reinforce", "follow_plan", "economy", "wait")  # code executes these; fixed
ATTACK_ROWS = ("threatened", "all")


class JevPolicy(BaseModel):
    """Legacy tactics for the explicit rules baseline; not used by the OneJev action selector.

    Historical policy format retained for existing playbook versions:

    Three layers (2026-10-28): hard rules in rules/hard_rules.py (fixed); these tactics, all numbers and
    orderings that code compares and follows (coach-editable): which action wins, which card, which row
    and which column; and one fuzzy yes/no judgment asked of OneJev. OneJev picked "emergency" for calm
    scenes when asked to choose the action itself, but answered single yes/no facts 6/6 correctly (27B).

    Actions, checked in the order of `priorities`; the first that holds AND can be placed now (passing the
    hard rules) wins:
      emergency  zombie at column <= danger_col (or <= tough_danger_col in a row judged tough) and a
                 usable emergency card: place it at that zombie's column + each of emergency_offsets in turn;
                 a card in armed_plants (needs time to arm) goes armed_lead_cols columns ahead (left) instead
      attack     a row with fewer shooters than shooters_per_row (shooters_per_tough_row if judged tough):
                 threatened rows first (closest zombie first), then, if attack_rows is "all", the others top
                 to bottom; the first free column of attack_cols
      reinforce  zombie at column <= wall_trigger_col in a row without a wall: the first free column from
                 min(zombie column - wall_gap, wall_max_col) down to wall_min_col
      follow_plan  the planner's next step
      economy    no zombie at column <= economy_safe_col and fewer than economy_target economy plants: the
                 row with the fewest, the first free column of economy_cols. While fewer than economy_first
                 are on the lawn, economy is checked right after emergency and ignores economy_safe_col
                 (2026-10-29 real run: zombies were always within column 5, so no Sunflower was ever placed)
      wait       always
    Within a role, cards are tried in the order of its plant list, and the first card and column the hard
    rules allow is taken.
    """

    model_config = ConfigDict(extra="forbid")

    priorities: list[str] = Field(default=list(JEV_ACTIONS), description=f"an ordering of {JEV_ACTIONS}")
    # when
    danger_col: int = Field(default=3, ge=0, le=8, description="zombie at or left of this column: emergency")
    tough_danger_col: int = Field(default=5, ge=0, le=8, description="same, for rows judged to hold a tough zombie")
    wall_trigger_col: int = Field(default=7, ge=0, le=9, description="zombie at or left of this column: wall the row")
    economy_safe_col: int = Field(default=5, ge=0, le=9, description="no zombie at or left of this column to add economy")
    economy_target: int = Field(default=10, ge=0, le=60, description="stop adding economy plants after this many")
    economy_first: int = Field(default=0, ge=0, le=20,
                               description="the first this many economy plants come right after emergency")
    shooters_per_row: int = Field(default=1, ge=0, le=5, description="attack until each row has this many shooters")
    shooters_per_tough_row: int = Field(default=2, ge=0, le=6, description="same, for rows judged tough")
    attack_rows: str = Field(default="all", description=f"one of {ATTACK_ROWS}: which rows attack fills")
    # where
    emergency_offsets: list[int] = Field(default=[0, -1, 1], min_length=1, max_length=3,
                                         description="columns tried relative to the zombie, in order (-1..1)")
    armed_lead_cols: int = Field(default=3, ge=1, le=6,
                                 description="columns ahead of the zombie for armed_plants (then one further)")
    attack_cols: list[int] = Field(default=[2, 1, 3, 4], min_length=1, max_length=9,
                                   description="columns tried for a shooter, in order")
    wall_gap: int = Field(default=1, ge=0, le=4, description="columns between the zombie and its wall")
    wall_max_col: int = Field(default=6, ge=0, le=8)
    wall_min_col: int = Field(default=3, ge=0, le=8)
    economy_cols: list[int] = Field(default=[0, 1], min_length=1, max_length=9,
                                    description="columns tried for economy plants, in order")
    # which cards, in order of preference
    emergency_plants: list[str] = Field(default=["cherry bomb", "squash", "jalapeno", "potato mine"], max_length=10)
    armed_plants: list[str] = Field(default=["potato mine"], max_length=10,
                                    description="emergency plants that need time to arm, placed ahead of the zombie")
    attack_plants: list[str] = Field(default=["snow pea", "repeater", "threepeater", "peashooter"], max_length=10)
    wall_plants: list[str] = Field(default=["wall-nut", "tall-nut", "pumpkin"], max_length=10)
    economy_plants: list[str] = Field(default=["sunflower", "sun-shroom", "twin sunflower"], max_length=10)
    tough_question: str = Field(
        default=("The zombies in this row are: {types}. Is any of them one that ordinary peashooters struggle "
                 "to stop (armoured, shielded, very fast or jumping)?"),
        min_length=20, max_length=400, description="yes/no question for OneJev; must contain {types}")

    @model_validator(mode="after")
    def _check(self) -> JevPolicy:
        if sorted(self.priorities) != sorted(JEV_ACTIONS):
            raise ValueError(f"priorities must be an ordering of exactly {JEV_ACTIONS}")
        if "{types}" not in self.tough_question:
            raise ValueError("tough_question must contain {types}")
        if self.attack_rows not in ATTACK_ROWS:
            raise ValueError(f"attack_rows must be one of {ATTACK_ROWS}")
        if any(not -1 <= o <= 1 for o in self.emergency_offsets):
            raise ValueError("emergency_offsets must be within -1..1 (instant kills reach one column)")
        if any(not 0 <= c <= 8 for c in self.attack_cols + self.economy_cols):
            raise ValueError("columns must be 0..8")
        if self.wall_min_col > self.wall_max_col:
            raise ValueError("wall_min_col must not exceed wall_max_col")
        return self


class PlannerKnowledge(BaseModel):
    """Read by both Planner and Jev as editable strategy knowledge alongside their role contracts.

    Describe budget adjustments, tactical spacing and threat reasoning from observed facts.
    These are model advice, not code filters. Do not encode exact actions or hidden state.
    """

    model_config = ConfigDict(extra="forbid")

    knowledge: str = Field(
        default=("Give Jev stage objectives, priorities and a budget consistent with the observed lawn.\n"
                 "Leave concrete plant and cell choices to Jev; reserve sun when appropriate."),
        max_length=8000)


class TimingParams(BaseModel):
    """Read by the session scheduler: target seconds between call starts, with no overlap.

    Coach-editable between evaluated versions, not live model tool permissions.
    Null inherits the server setting; explicit values override it for this version.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    resources_seconds: float | None = Field(default=None, ge=2, le=30, strict=True)
    zombies_seconds: float | None = Field(default=None, ge=2, le=30, strict=True)
    plants_seconds: float | None = Field(default=None, ge=2, le=30, strict=True)
    planner_seconds: float | None = Field(default=None, ge=4, le=60, strict=True)
    frame_log_seconds: float | None = Field(default=None, ge=1, le=60, strict=True)
    danger_replan: bool = True
    danger_min_seconds: float = Field(default=4, ge=2, le=12, strict=True)


class DecisionParams(BaseModel):
    """Read by the Jev step: how Jev's alternatives are presented.

    auto: the named decision menu when player_code defines options() and act(), otherwise
    the built-in hierarchical (live) or scheduled (research) placement choice.
    hierarchical/flat force the built-in placement choice even when a menu exists (A/B).
    """

    model_config = ConfigDict(extra="forbid")
    jev_mode: Literal["auto", "hierarchical", "flat"] = "auto"


class ReflexParams(BaseModel):
    """Read by the action gate for player_code reflex() actions (every 0.25 s).

    Sun pickup reflexes are always permitted; planting from a reflex is off unless enabled,
    and every reflex plant still passes the hard rules.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    allow_planting: bool = False
    min_plant_interval_s: float = Field(default=4, ge=2, le=60, strict=True)


class FallbackParams(BaseModel):
    """Read when System0's decide() has said the current plan no longer fits (request_replan).

    Until the next plan arrives, player_code fallback() replaces Jev and may plant directly;
    off unless enabled, and every plant still passes the hard rules.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    allow_planting: bool = False
    min_plant_interval_s: float = Field(default=2, ge=1, le=30, strict=True)


class Lesson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    when: str = Field(description="One expression with and/or/not, comparisons and + - * /; unknown values make "
                                  "a comparison false. Names: " + vocabulary())
    then: str = Field(min_length=1, max_length=300, description="One short piece of advice for Planner and Jev")

    @model_validator(mode="after")
    def check_condition(self):
        parse_condition(self.when)
        return self


class LessonBook(BaseModel):
    """Read by Planner and Jev: each lesson is shown only while its condition holds on the current lawn.

    Conditions use a fixed vocabulary of observed facts; at most max_shown matching lessons, in
    listed order, go into a call. planner.knowledge stays for advice that always applies.
    """

    model_config = ConfigDict(extra="forbid")
    items: list[Lesson] = Field(default_factory=list, max_length=30)
    max_shown: int = Field(default=4, ge=1, le=8, strict=True)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({lesson.id for lesson in self.items}) != len(self.items):
            raise ValueError("lesson ids must be unique")
        return self


class Playbook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^v\d{3,}$")
    parent: str | None = None
    perception: PerceptionParams
    fast: FastParams
    jev: JevPolicy = Field(default_factory=JevPolicy)
    planner: PlannerKnowledge = Field(default_factory=PlannerKnowledge)
    timing: TimingParams = Field(default_factory=TimingParams)
    decision: DecisionParams = Field(default_factory=DecisionParams)
    reflex: ReflexParams = Field(default_factory=ReflexParams)
    fallback: FallbackParams = Field(default_factory=FallbackParams)
    lessons: LessonBook = Field(default_factory=LessonBook)
    notes: str = Field(default="", max_length=2000)
    player_code: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$",
                                    description="Immutable evaluated System0 code; only the coach's gated promotion can set it")

    @model_validator(mode="before")
    @classmethod
    def discard_retired_decider(cls, data):
        # Immutable historical versions remain readable; never serialize this dead section again.
        if isinstance(data, dict) and "decider" in data:
            data = {k: v for k, v in data.items() if k != "decider"}
        return data

    def active_dump(self, selector: str) -> dict:
        return self.model_dump(exclude={"jev"} if selector == "onejev" else set())


EDITABLE_SECTIONS = ("perception", "fast", "jev", "planner", "timing", "decision", "reflex", "fallback", "lessons")
