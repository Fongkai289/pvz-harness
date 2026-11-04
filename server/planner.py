"""Slow layer: observed state becomes strategic guidance, never executable placements.

Must never import server.judge or read StateMsg (tests/test_isolation.py).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

import anthropic
import httpx
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from server import geometry
from server.perception import Observation, describe
from server.scene import Scene
from server.mechanics import mechanics_context

if TYPE_CHECKING:
    from server.config import Settings

log = logging.getLogger(__name__)


class Placement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slot: int = Field(ge=0, strict=True, description="Seed packet slot, 0 = leftmost")
    row: int = Field(ge=0, strict=True, description="Lawn row, 0 = top")
    col: int = Field(ge=0, strict=True, description="Lawn column, 0 = leftmost (house side)")


def short_text(limit: int, min_length: int = 0, description: str | None = None):
    """Free text Jev reads. The schema asks for brevity (output tokens are the Planner's latency);
    an answer that runs over is cut at the limit instead of throwing the whole plan away."""
    cut = BeforeValidator(lambda v: v[:limit] if isinstance(v, str) else v)
    return Annotated[str, cut, Field(min_length=min_length, max_length=limit, description=description)]


class ThreatAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row: int = Field(ge=0, le=5, strict=True)
    urgency: Literal["low", "medium", "high", "critical"]
    reason: short_text(80, 1)


class KeyPlant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row: int = Field(ge=0, le=5, strict=True)
    col: int = Field(ge=0, le=8, strict=True)
    plant: str = Field(min_length=1, max_length=24)


class GoalCount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plant: str = Field(min_length=1, max_length=24)
    at_least: int = Field(ge=1, le=54, strict=True)


class ReplanWhen(BaseModel):
    """Conditions under which this plan stops fitting; System0's code checks them, not the host."""
    model_config = ConfigDict(extra="forbid")
    covered_rows: list[Annotated[int, Field(ge=0, le=5, strict=True)]] = Field(default_factory=list, max_length=6,
        description="Rows this plan already handles; a zombie appearing in another row is a new threat")
    covered_types: list[Annotated[str, Field(min_length=1, max_length=24)]] = Field(default_factory=list, max_length=8,
        description="Zombie types this plan already expects; an unexpected type is a new threat")
    key_plants: list[KeyPlant] = Field(default_factory=list, max_length=10,
        description="Existing attack/defense plants the plan relies on; losing one (not a used-up "
                    "cherry bomb, potato mine or other single-use plant) means the defense failed")
    goal_plants: list[GoalCount] = Field(default_factory=list, max_length=4,
        description="The plan's goal is reached once the lawn holds at least these plant counts")
    goal_clear_rows: list[Annotated[int, Field(ge=0, le=5, strict=True)]] = Field(default_factory=list, max_length=6,
        description="The plan's target is gone once these rows have no zombies left")
    max_failed_steps: int = Field(default=3, ge=2, le=10, strict=True,
        description="Consecutive Jev decisions that could not be carried out before the plan counts as unworkable")


class Plan(BaseModel):
    """Strategic guidance only: no plant slots, cell coordinates or action queue."""
    model_config = ConfigDict(extra="forbid")
    objective: short_text(160, 1, "Stage objective for Jev in one short sentence, not a placement command")
    priorities: list[Literal["defense", "economy", "reinforce", "save_sun"]] = Field(
        default=["defense", "economy", "reinforce"], min_length=1, max_length=4)
    priority_rows: list[Annotated[int, Field(ge=0, le=5, strict=True)]] = Field(default_factory=list, max_length=6)
    blocked_rows: list[Annotated[int, Field(ge=0, le=5, strict=True)]] = Field(default_factory=list, max_length=6,
        description="Rows advised to avoid, not executor prohibitions; reassess against current observations")
    sun_reserve: int | None = Field(default=None, ge=0, le=9990, strict=True, description="Advised reserve, null if unspecified; not an execution limit")
    spend_limit: int | None = Field(default=None, ge=0, le=9990, strict=True,
                             description="Initial suggested spending allowance, null if unspecified; code reports remaining advice but does not filter actions by it")
    budget_policy: short_text(160, description="One sentence: when Jev should revise the spending advice") = ""
    tactics: short_text(240, description="At most two sentences on spacing, arming delays and defense; no exact placements or executable actions") = ""
    threat_assessment: list[ThreatAssessment] = Field(default_factory=list, max_length=6,
        description="Your assessment of observed threatened rows, not a code-generated risk level")
    valid_for_seconds: int = Field(default=40, ge=5, le=120, strict=True,
        description="Seconds until a routine refresh; the host never treats a plan as expiring in under 40 s")
    replan_when: ReplanWhen = Field(default_factory=ReplanWhen,
        description="When this plan stops fitting; checked continuously, so a long validity is safe")
    note: short_text(160) = ""
    preferred_decisions: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]] = Field(
        default_factory=list, max_length=6,
        description="Ids from the supplied decision_menu, most preferred first; advice to Jev, empty when no menu is supplied")

    @field_validator("priorities", "priority_rows", "blocked_rows", "preferred_decisions")
    @classmethod
    def deduplicate_directives(cls, values):
        # Repeated advice adds no meaning. Preserve the model's first-choice order;
        # field types/limits and contradictory guidance still undergo validation.
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def consistent_directives(self):
        if set(self.priority_rows) & set(self.blocked_rows):
            raise ValueError("a priority row cannot also be blocked")
        if len({a.row for a in self.threat_assessment}) != len(self.threat_assessment):
            raise ValueError("duplicate threat assessment row")
        return self


@dataclass
class PlanResult:
    plan: Plan | None
    latency: float
    error: str | None = None
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class Planner(Protocol):
    async def plan(self, jpeg: bytes, obs: Observation, knowledge: str) -> PlanResult: ...

    async def decide(self, scene: Scene, knowledge: str, jpeg: bytes | None = None,
                     context: dict | None = None) -> PlanResult: ...


# Fixed task contract (code, not playbook): what the planner is and how to answer. The playbook's
# planner.knowledge is appended after it, so the coach can change strategy but not the contract.
CONTRACT = f"""You are the strategic planner for Plants vs. Zombies from screenshots only.
The lawn has {geometry.ROWS} rows (0 = top) and {geometry.COLS} columns (0 = left, next to the house).
Zombies walk in from the right. Seed packets are in the bar at the top, slot 0 is the leftmost.
Give Jev a stage objective, ordered priorities, priority/blocked rows, a sun reserve, total spending
limit and validity duration. Jev independently chooses which plant, row and column, or waits.
Do not specify seed slots, exact cells or a sequence of planting actions, including in free text.
Do not modify Jev's prompt, API, tools or calling frequency. Your output is guidance data only.
Spending, reserve, row preferences and tactics are advisory messages to Jev, not executor gates.
State a budget_policy describing when your advice should change, using the latest observed resources.
Jev receives your original allowance, updated committed spending and remaining advice separately.
Only actual affordability/card/cell/freshness checks constrain the executor. Do not treat an old budget as current sun.
Assess threat urgency yourself from visible positions, plants and game reference; no fixed distance risk score is supplied.
Use tactics for distance/arming considerations; the executor does not enforce a fixed mine lead or same-row explosive reach.
Code automatically collects visible suns independently of you and Jev. Do not ask Jev to collect suns.
Waiting for planting resources must not imply stopping sun collection. Use observed sun balance, not predicted income.
When a decision_menu is supplied, Jev chooses among those named decisions (or waits) and the player's code
places the plant; you may list preferred_decisions by menu id. They are advice, not commands, and a menu id
that is no longer offered is ignored. Without a menu leave preferred_decisions empty.
You are called routinely about every 25 seconds, and sooner (at most every 8 seconds) when System0
reports that the plan stopped fitting, so plan for the next half minute.
Be brief: your reply's length is your latency. Each text field is one short sentence (objective, note and
budget_policy under 160 characters, tactics under 240, each threat reason under 80). Assess only rows
with zombies or a real risk.
State in replan_when what the plan covers: the rows and zombie types it already handles, the existing
plants it relies on (cells already planted, never new placements), what counts as its goal being reached, and how many unexecutable decisions mean
it is unworkable. Sun income alone never calls you back: once Jev can afford what you advised, it acts.
Emergencies inside covered rows are Jev's to handle with the current plan.
Strategy knowledge is background advice; it cannot change these role boundaries."""


# Split mode: a separate look step turned the screenshot into a scene; this step only decides.
DECIDE_CONTRACT = f"""{CONTRACT}
A separate look step has read the screen for you and
describes it below; it can miss things, and "unknown" means it was unsure.
Rows are numbered from 0 at the top (the description says how many there are) and columns 0-{geometry.COLS - 1}
from the left, next to the house. Zombies walk in from the right.
Set strategic guidance using only observed facts. Treat unknown values as uncertain.
Replan reasons and row observations are change notifications, not ground truth or prescribed urgency.
Previous guidance is historical advice. Its claims about sun, cards or threats cannot override the latest observation.
Reassess priorities and budget when alerted; use the current observations rather than copying the previous plan."""


def system_prompt(knowledge: str, contract: str = CONTRACT) -> str:
    return f"{contract}\n\nStrategy knowledge:\n{knowledge}" if knowledge.strip() else contract


def user_text(obs: Observation, include_summary: bool) -> str:
    text = f"Frame {obs.frame_id}. What strategic guidance should Jev follow?"
    return f"{text}\n{describe(obs)}" if include_summary else text


class MockPlanner:
    """Stands in until an API key is configured: returns an empty plan after a fixed delay."""

    def __init__(self, delay: float = 0.5) -> None:
        self.delay = delay

    async def plan(self, jpeg: bytes, obs: Observation, knowledge: str = "") -> PlanResult:
        start = time.monotonic()
        await asyncio.sleep(self.delay)
        return PlanResult(
            plan=Plan(objective="Observe and wait", spend_limit=0, note="mock planner"), latency=time.monotonic() - start
        )

    async def decide(self, scene: Scene, knowledge: str, jpeg: bytes | None = None,
                     context: dict | None = None) -> PlanResult:
        return await self.plan(b"", Observation(frame_id=scene.frame_id, t_capture=0.0), knowledge)


class QwenPlanner:
    """Qwen3.8-27B on the shared vLLM pool (OpenAI-compatible API).

    send_image=False is the text-only mode: the model reads the perception summary instead of the
    screenshot (harness ladder L3). The pool is shared, so one request at a time and a hard timeout.
    """

    def __init__(self, url: str, model: str, send_image: bool, include_summary: bool,
                 timeout: float, client: httpx.AsyncClient | None = None) -> None:
        if not send_image and not include_summary:
            raise ValueError("a text-only planner needs the perception summary")
        self.url, self.model = url, model
        self.send_image, self.include_summary = send_image, include_summary
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def plan(self, jpeg: bytes, obs: Observation, knowledge: str = "") -> PlanResult:
        """End-to-end (harness ladder L1/L3): look and decide in one call. Kept as the baseline."""
        content: list[dict] = []
        if self.send_image:
            content.append(_image_part(jpeg))
        content.append({"type": "text", "text": user_text(obs, self.include_summary)})
        content.append({"type": "text", "text": "Static game reference: " + json.dumps(mechanics_context(), ensure_ascii=False)})
        return await self._request(system_prompt(knowledge), content)

    async def decide(self, scene: Scene, knowledge: str, jpeg: bytes | None = None,
                     context: dict | None = None) -> PlanResult:
        """Split mode: decide from the look step's scene; the screenshot is attached only as a reference."""
        content = [_image_part(jpeg)] if jpeg is not None else []
        planted = {(p["row"], p["col"]): p["plants"] for p in (context or {}).get("plants", [])}
        content.append({"type": "text", "text": "Static game reference: " +
                        json.dumps(mechanics_context(scene, planted), ensure_ascii=False)})
        content.append({"type": "text", "text": f"{scene.describe()}\nObserved context: "
                        f"{json.dumps(context or {}, ensure_ascii=False)}\nWhat guidance should Jev follow?"})
        return await self._request(system_prompt(knowledge, DECIDE_CONTRACT), content)

    async def _request(self, system: str, content: list[dict]) -> PlanResult:
        start = time.monotonic()
        body = {
            "model": self.model,
            "max_tokens": 4096,  # a plan with threat rows and replan_when runs past 1024 tokens of JSON
            "temperature": 0,  # same scene, same plan; vLLM's default is 1.0
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "plan", "schema": Plan.model_json_schema()},
            },
        }
        try:
            response = await self.client.post(self.url, json=body)
            response.raise_for_status()
            payload = response.json()
            if payload["choices"][0].get("finish_reason") == "length":
                usage = payload.get("usage") or {}
                return _failed(start, f"plan cut off at the {body['max_tokens']}-token output limit "
                                      f"({usage.get('completion_tokens')} tokens)")
            plan = Plan.model_validate_json(payload["choices"][0]["message"]["content"])
        except httpx.HTTPStatusError as e:
            return _failed(start, f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except httpx.HTTPError as e:
            return _failed(start, f"{type(e).__name__}: {e}")
        except (KeyError, IndexError, ValueError, TypeError) as e:
            return _failed(start, f"bad response: {e}")
        usage = payload.get("usage") or {}
        return PlanResult(
            plan=plan, latency=time.monotonic() - start, request_id=payload.get("id"),
            input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
        )


def _image_part(jpeg: bytes) -> dict:
    data = base64.standard_b64encode(jpeg).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


def _failed(start: float, error: str) -> PlanResult:
    log.warning("planner call failed: %s", error)
    return PlanResult(plan=None, latency=time.monotonic() - start, error=error)


class ClaudePlanner:
    def __init__(self, model: str, effort: str, include_summary: bool) -> None:
        self.client = anthropic.AsyncAnthropic()
        self.model = model
        self.effort = effort
        self.include_summary = include_summary

    async def plan(self, jpeg: bytes, obs: Observation, knowledge: str = "") -> PlanResult:
        start = time.monotonic()
        image = base64.standard_b64encode(jpeg).decode()
        try:
            response = await self.client.messages.parse(
                model=self.model,
                max_tokens=4096,
                system=system_prompt(knowledge),
                output_config={"effort": self.effort},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/jpeg", "data": image},
                            },
                            {"type": "text", "text": user_text(obs, self.include_summary)
                             + "\nStatic game reference: " + json.dumps(mechanics_context(), ensure_ascii=False)},
                        ],
                    }
                ],
                output_format=Plan,
                # Re-run a safety-classifier decline on Anthropic's recommended fallback model.
                extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                extra_body={"fallbacks": "default"},
            )
        except anthropic.RateLimitError as e:
            return _failed(start, f"rate limited: {e.message}")
        except anthropic.APIStatusError as e:
            return _failed(start, f"API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError as e:
            return _failed(start, f"connection error: {e}")

        latency = time.monotonic() - start
        common = dict(
            latency=latency,
            request_id=response._request_id,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        if response.stop_reason == "refusal":
            return PlanResult(plan=None, error="refusal", **common)
        return PlanResult(plan=response.parsed_output, **common)

    async def decide(self, scene: Scene, knowledge: str, jpeg: bytes | None = None,
                     context: dict | None = None) -> PlanResult:
        raise NotImplementedError("split mode is implemented for the Qwen planner only")


def make_planner(s: Settings) -> Planner:
    if s.planner == "qwen":
        return QwenPlanner(s.qwen_url, s.qwen_model, send_image=s.planner_send_image,
                           include_summary=s.planner_include_summary, timeout=s.planner_timeout)
    if s.planner == "claude":
        return ClaudePlanner(s.planner_model, s.planner_effort, s.planner_include_summary)
    if s.planner == "mock":
        return MockPlanner()
    raise ValueError(f"unknown planner: {s.planner}")
