"""Slow layer: a model looks at a screenshot (and/or the perception summary) and returns a plan.

Must never import server.judge or read StateMsg (tests/test_isolation.py).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import anthropic
import httpx
from pydantic import BaseModel, Field, ValidationError

from server import geometry
from server.perception import Observation, describe
from server.scene import Scene

if TYPE_CHECKING:
    from server.config import Settings

log = logging.getLogger(__name__)


class Placement(BaseModel):
    slot: int = Field(description="Seed packet slot, 0 = leftmost")
    row: int = Field(description="Lawn row, 0 = top")
    col: int = Field(description="Lawn column, 0 = leftmost (house side)")


class Plan(BaseModel):
    placements: list[Placement] = Field(description="Plants to place now, in order")
    # Answer length is most of the latency (a 467-token note took 7.6 s, a 70-token one 1.2 s).
    note: str = Field(max_length=300, description="One short sentence on the current situation and why")


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

    async def decide(self, scene: Scene, knowledge: str, jpeg: bytes | None = None) -> PlanResult: ...


# Fixed task contract (code, not playbook): what the planner is and how to answer. The playbook's
# planner.knowledge is appended after it, so the coach can change strategy but not the contract.
CONTRACT = f"""You are playing Plants vs. Zombies from screenshots only.
The lawn has {geometry.ROWS} rows (0 = top) and {geometry.COLS} columns (0 = left, next to the house).
Zombies walk in from the right. Seed packets are in the bar at the top, slot 0 is the leftmost.
Choose which plants to place right now, as a list of placements (seed slot, row, column).
Sun collection is handled for you."""


# Split mode: a separate look step turned the screenshot into a scene; this step only decides.
DECIDE_CONTRACT = f"""You are playing Plants vs. Zombies. A separate look step has read the screen for you and
describes it below; it can miss things, and "unknown" means it was unsure.
Rows are numbered from 0 at the top (the description says how many there are) and columns 0-{geometry.COLS - 1}
from the left, next to the house. Zombies walk in from the right.
Choose which plants to place right now, as a list of placements (seed slot, row, column), using only
the usable seed slots. Keep the note to one short sentence. Sun collection is handled for you."""


def system_prompt(knowledge: str, contract: str = CONTRACT) -> str:
    return f"{contract}\n\nStrategy knowledge:\n{knowledge}" if knowledge.strip() else contract


def user_text(obs: Observation, include_summary: bool) -> str:
    text = f"Frame {obs.frame_id}. What should be planted now?"
    return f"{text}\n{describe(obs)}" if include_summary else text


class MockPlanner:
    """Stands in until an API key is configured: returns an empty plan after a fixed delay."""

    def __init__(self, delay: float = 0.5) -> None:
        self.delay = delay

    async def plan(self, jpeg: bytes, obs: Observation, knowledge: str = "") -> PlanResult:
        start = time.monotonic()
        await asyncio.sleep(self.delay)
        return PlanResult(
            plan=Plan(placements=[], note="mock planner"), latency=time.monotonic() - start
        )

    async def decide(self, scene: Scene, knowledge: str, jpeg: bytes | None = None) -> PlanResult:
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
        return await self._request(system_prompt(knowledge), content)

    async def decide(self, scene: Scene, knowledge: str, jpeg: bytes | None = None) -> PlanResult:
        """Split mode: decide from the look step's scene; the screenshot is attached only as a reference."""
        content = [_image_part(jpeg)] if jpeg is not None else []
        content.append({"type": "text", "text": f"{scene.describe()}\nWhat should be planted now?"})
        return await self._request(system_prompt(knowledge, DECIDE_CONTRACT), content)

    async def _request(self, system: str, content: list[dict]) -> PlanResult:
        start = time.monotonic()
        body = {
            "model": self.model,
            "max_tokens": 1024,
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
            plan = Plan.model_validate_json(payload["choices"][0]["message"]["content"])
        except httpx.HTTPStatusError as e:
            return _failed(start, f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except httpx.HTTPError as e:
            return _failed(start, f"{type(e).__name__}: {e}")
        except (KeyError, IndexError, ValidationError) as e:
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
                            {"type": "text", "text": user_text(obs, self.include_summary)},
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

    async def decide(self, scene: Scene, knowledge: str, jpeg: bytes | None = None) -> PlanResult:
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
