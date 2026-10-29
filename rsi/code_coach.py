"""Qwen coach proposes code from development evidence, without changing the judge."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from rsi.code_evaluate import Interrupted
from rsi.code_store import write_json
from server.config import Settings


class CodeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diagnosis: str = Field(min_length=1, max_length=3000)
    rationale: str = Field(min_length=1, max_length=3000)
    source: str = Field(min_length=1, max_length=24000)


SYSTEM = """You are an autonomous PvZ coach AND programmer. Discover the largest failure from
the development game records yourself, then improve the player's perception, state handling or
decision/reflex code. Do not wait for a person to identify a bug. You may rewrite the two functions
and add pure helper functions. Propose ONE coherent hypothesis per candidate, not a list of guesses.
Return complete replacement Python source, diagnosis with evidence, and rationale as JSON.

The simulator uses real PvZ-Emulator combat physics, with a procedural symbolic image renderer.
It is NOT the real game's screenshot. Learning here does not prove real-game vision improvement.
The player receives ONLY pixels and its own observations/memory. You may see privileged ground
truth in finished DEVELOPMENT games to diagnose mistakes. Never encode seeds, evaluation scores,
future spawn schedules, or facts unavailable on the displayed screen into the player.

Contract: perceive(image) -> JSON dict; decide(observation, memory) -> None or exactly
{'plant': one of the card names, 'row': 0..4, 'col': 0..8}. The memory dict is reset each episode.
One action per game second. Prefer simple fast code. Names, coordinates and colors are documented
in renderer_contract. Planting charges sun and enforces cooldowns and occupied cells in the engine.

Allowed: def, if, for, comprehensions, arithmetic/comparison/boolean operations, list/dict/tuple,
subscripts; common builtins range/len/min/max/sum/sorted/enumerate/abs/any/all/int/float/bool/round/zip;
data methods get/items/values/keys/append/extend/pop/copy/count/sort. Helpers in pixel_helpers.
No imports, classes, decorators, underscore names/attributes, files, network, eval, exec, print,
introspection or access to the evaluator. No while loops. Define both entry functions. Do not use
type annotations. Keep source under 24000 characters and observations/memory compact.

You cannot edit scoring, environment, test seeds, promotion rules, or test reports. Your code is
measured on paired development games and unseen confirmation/audit games under fixed rules.
Use earlier development failures to avoid repeating rejected proposals. Improving detection is
useful only if it produces better play; do not invent successes or alter reported results."""


def development_summary(report: dict) -> dict:
    episodes = report["episodes"]
    worst = sorted(episodes, key=lambda e: e["score"])[:2]
    summary = {
        "mean": report["mean"],
        "games": [{"score": e["score"], "outcome": e["outcome"], "truth": e["truth"],
                   "player_p95_ms": e["player_p95_ms"]} for e in episodes],
        # Seeds and engine private fields are not sent. Ground truth is post-game coaching only.
        "worst_traces": [e["trace"][-8:] for e in worst],
    }
    def redact(value):
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items() if k not in {
                "seed", "schedule", "future_spawns", "limitations", "engine"}}
        if isinstance(value, list):
            return [redact(v) for v in value]
        return value
    return redact(summary)


async def propose_code(source: str, development: dict, history: list[dict], settings: Settings,
                       *, controls: Path, renderer_contract: str, pixel_helpers: str) -> tuple[CodeProposal, dict]:
    if sys.platform != "linux":
        raise RuntimeError("Model calls must run on the configured Linux server, never the Windows game machine")
    body = {
        "model": settings.qwen_model, "max_tokens": 8192, "temperature": 0.3,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps({
            "current_source": source, "development": development_summary(development),
            "history": history, "renderer_contract": renderer_contract, "pixel_helpers": pixel_helpers,
        }, ensure_ascii=False)}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "code_candidate", "schema": CodeProposal.model_json_schema()}},
    }
    request_id = uuid.uuid4().hex
    audit_dir = controls / "coach_calls"
    audit_dir.mkdir(exist_ok=True)
    write_json(audit_dir / f"{request_id}-request.json", body)
    start = time.monotonic()
    async with httpx.AsyncClient(timeout=180) as client:
        request = asyncio.create_task(client.post(settings.qwen_url, json=body))
        try:
            while not request.done():
                if (controls / "STOP").exists() or (controls / "PAUSE").exists():
                    raise Interrupted("explicit stop/pause: coach request cancelled")
                await asyncio.wait({request}, timeout=0.2)
            response = await request
            response.raise_for_status()
            payload = response.json()
            write_json(audit_dir / f"{request_id}-response.json", {
                "latency_seconds": time.monotonic() - start, "response": payload})
            proposal = CodeProposal.model_validate_json(payload["choices"][0]["message"]["content"])
            return proposal, {"latency_seconds": time.monotonic() - start,
                              "model": settings.qwen_model, "usage": payload.get("usage"),
                              "request_id": request_id, "response_id": payload.get("id"),
                              "finish_reason": payload["choices"][0].get("finish_reason")}
        except BaseException as exc:
            write_json(audit_dir / f"{request_id}-error.json", {
                "latency_seconds": time.monotonic() - start,
                "error": f"{type(exc).__name__}: {exc}"})
            raise
        finally:
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
