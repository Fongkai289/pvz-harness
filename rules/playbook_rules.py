"""Fixed rules: what a playbook may contain and the allowed ranges.

The coach edits playbooks; it never edits this file, the judge, or the test seeds. A
candidate that fails validation is rejected before it is ever played.

Each section has one reader during play (named in its docstring); the coach reads and
edits all of them. Sections added later carry defaults, so older versions still load.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


DECIDER_CHOICES = ("place", "wait")  # the code maps these keys to actions, so they are fixed


class DeciderQuestion(BaseModel):
    """Retired 2026-10-28 (replaced by JevPolicy); kept so playbook versions written before then still load."""

    model_config = ConfigDict(extra="forbid")

    instructions: str = Field(
        default="Should the next planned plant be placed right now?", min_length=10, max_length=400)
    criteria: dict[str, str] = Field(
        default={
            "place": "place it now: the lawn needs it and nothing more urgent is happening",
            "wait": "wait: placing it now would be wasted or something else must happen first",
        },
        description=f"exactly the keys {DECIDER_CHOICES}, each with a short description")

    @model_validator(mode="after")
    def _keys(self) -> DeciderQuestion:
        if set(self.criteria) != set(DECIDER_CHOICES):
            raise ValueError(f"criteria keys must be exactly {DECIDER_CHOICES}")
        if any(not 5 <= len(v) <= 300 for v in self.criteria.values()):
            raise ValueError("each option description must be 5-300 characters")
        return self


JEV_ACTIONS = ("emergency", "attack", "reinforce", "follow_plan", "economy", "wait")  # code executes these; fixed
ATTACK_ROWS = ("threatened", "all")


class JevPolicy(BaseModel):
    """Read by Jev's tactics code about once a second; the only model call is the tough-zombie judgment.

    Three layers (2026-10-28): hard rules in rules/hard_rules.py (fixed); these tactics, all numbers and
    orderings that code compares and follows (coach-editable): which action wins, which card, which row
    and which column; and one fuzzy yes/no judgment asked of OneJev. OneJev picked "emergency" for calm
    scenes when asked to choose the action itself, but answered single yes/no facts 6/6 correctly (27B).

    Actions, checked in the order of `priorities`; the first that holds AND can be placed now wins:
      emergency  zombie at column <= danger_col (or <= tough_danger_col in a row judged tough) and a
                 usable emergency card: place it at that zombie's column + each of emergency_offsets in turn
      attack     a row with fewer shooters than shooters_per_row (shooters_per_tough_row if judged tough):
                 threatened rows first (closest zombie first), then, if attack_rows is "all", the others top
                 to bottom; the first free column of attack_cols
      reinforce  zombie at column <= wall_trigger_col in a row without a wall: the first free column from
                 min(zombie column - wall_gap, wall_max_col) down to wall_min_col
      follow_plan  the planner's next step
      economy    no zombie at column <= economy_safe_col and fewer than economy_target economy plants: the
                 row with the fewest, the first free column of economy_cols
      wait       always
    Within a role, cards are tried in the order of its plant list.
    """

    model_config = ConfigDict(extra="forbid")

    priorities: list[str] = Field(default=list(JEV_ACTIONS), description=f"an ordering of {JEV_ACTIONS}")
    # when
    danger_col: int = Field(default=3, ge=0, le=8, description="zombie at or left of this column: emergency")
    tough_danger_col: int = Field(default=5, ge=0, le=8, description="same, for rows judged to hold a tough zombie")
    wall_trigger_col: int = Field(default=7, ge=0, le=9, description="zombie at or left of this column: wall the row")
    economy_safe_col: int = Field(default=5, ge=0, le=9, description="no zombie at or left of this column to add economy")
    economy_target: int = Field(default=10, ge=0, le=60, description="stop adding economy plants after this many")
    shooters_per_row: int = Field(default=1, ge=0, le=5, description="attack until each row has this many shooters")
    shooters_per_tough_row: int = Field(default=2, ge=0, le=6, description="same, for rows judged tough")
    attack_rows: str = Field(default="all", description=f"one of {ATTACK_ROWS}: which rows attack fills")
    # where
    emergency_offsets: list[int] = Field(default=[0, -1, 1], min_length=1, max_length=3,
                                         description="columns tried relative to the zombie, in order (-1..1)")
    attack_cols: list[int] = Field(default=[2, 1, 3, 4], min_length=1, max_length=9,
                                   description="columns tried for a shooter, in order")
    wall_gap: int = Field(default=1, ge=0, le=4, description="columns between the zombie and its wall")
    wall_max_col: int = Field(default=6, ge=0, le=8)
    wall_min_col: int = Field(default=3, ge=0, le=8)
    economy_cols: list[int] = Field(default=[0, 1], min_length=1, max_length=9,
                                    description="columns tried for economy plants, in order")
    # which cards, in order of preference
    emergency_plants: list[str] = Field(default=["cherry bomb", "squash", "jalapeno", "potato mine"], max_length=10)
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
    """Read by the planner model (Qwen) as strategy knowledge, appended to its fixed task contract.

    Write it for the planner: short, concrete rules it can act on from what it sees.
    """

    model_config = ConfigDict(extra="forbid")

    knowledge: str = Field(
        default=("Only place a plant you can afford and whose seed packet is not on cooldown.\n"
                 "Return an empty list when nothing should be placed."),
        max_length=8000)


class Playbook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^v\d{3,}$")
    parent: str | None = None
    perception: PerceptionParams
    fast: FastParams
    decider: DeciderQuestion = Field(default_factory=DeciderQuestion)  # retired, not read
    jev: JevPolicy = Field(default_factory=JevPolicy)
    planner: PlannerKnowledge = Field(default_factory=PlannerKnowledge)
    notes: str = Field(default="", max_length=2000)


EDITABLE_SECTIONS = ("perception", "fast", "jev", "planner")  # what the coach may propose changes to
