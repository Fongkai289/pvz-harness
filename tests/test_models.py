"""Model clients against fake HTTP transports (no shared pool is called)."""

import asyncio
import json

import httpx
import pytest

from server.perception import Blob, Observation
from server.planner import Placement, Plan, QwenPlanner

OBS = Observation(frame_id=3, t_capture=0.0, suns=[Blob(300, 250, 1200, 25.0)])
PLACEMENT = Placement(slot=0, row=2, col=1)


def run(coro):
    return asyncio.run(coro)


def qwen_planner(handler, send_image):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return QwenPlanner("http://qwen/v1/chat/completions", "Qwen3.8-27B", send_image=send_image,
                       include_summary=True, timeout=5, client=client)


def test_qwen_text_only_sends_summary_and_parses_plan():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        plan = Plan(objective="Defend threatened rows", priority_rows=[2], sun_reserve=50, spend_limit=150)
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            "choices": [{"message": {"content": plan.model_dump_json()}}]})

    result = run(qwen_planner(handler, send_image=False).plan(b"jpeg", OBS, "Buy Sunflowers first."))
    assert result.error is None and result.plan.priority_rows == [2] and result.plan.sun_reserve == 50
    assert result.input_tokens == 120
    system = seen["messages"][0]["content"]
    assert system.startswith("You are the strategic planner") and system.endswith("Buy Sunflowers first.")
    content = seen["messages"][1]["content"]
    assert all(part["type"] == "text" for part in content)
    assert "static_reference_not_live_state" in content[1]["text"]
    assert "Code automatically collects visible suns" in system
    assert "1 sun visible" in content[0]["text"]
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["chat_template_kwargs"] == {"enable_thinking": False}


def test_qwen_image_mode_sends_image_and_reports_rejection():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "At most 0 image(s) may be provided"}})

    result = run(qwen_planner(handler, send_image=True).plan(b"jpeg", OBS))
    assert result.plan is None and "HTTP 400" in result.error and "0 image" in result.error
    assert seen["messages"][1]["content"][0]["type"] == "image_url"


def test_qwen_repeated_advice_preserves_first_choice_order():
    payload = dict(objective="Expand sun production", priorities=["economy", "defense", "economy"],
                   priority_rows=[4, 2, 4], blocked_rows=[0, 0], preferred_decisions=["economy", "wait", "economy"])
    def handler(request):
        return httpx.Response(200, json={"choices":[{"message":{"content":json.dumps(payload)}}]})
    result = run(qwen_planner(handler, send_image=False).plan(b"jpeg", OBS))
    assert result.error is None
    assert result.plan.priorities == ["economy", "defense"]
    assert result.plan.priority_rows == [4, 2]
    assert result.plan.blocked_rows == [0]
    assert result.plan.preferred_decisions == ["economy", "wait"]


@pytest.mark.parametrize('fields', [
    {'priority_rows':[2, 2], 'blocked_rows':[2]},
    {'priority_rows':[6, 6]}, {'priority_rows':['2', '2']},
    {'priorities':['unknown', 'unknown']}, {'spend_limit':-1},
    {'preferred_decisions':['not a menu id', 'not a menu id']},
    {'threat_assessment':[{'row':0, 'urgency':'high', 'reason':'near'},
                          {'row':0, 'urgency':'low', 'reason':'far'}]},
])
def test_normalizing_repeated_advice_keeps_other_validation(fields):
    with pytest.raises(ValueError): Plan(objective='Defend', **fields)


def test_qwen_plan_cut_off_by_the_output_limit_says_so():
    """A long plan (threat rows, replan_when) must fit; a cut-off one is reported as such, not as bad JSON."""
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"usage": {"completion_tokens": 4096},
                                         "choices": [{"finish_reason": "length", "message": {"content": '{\n  "objective": "Est'}}]})

    result = run(qwen_planner(handler, send_image=False).plan(b"jpeg", OBS))
    assert result.plan is None and "output limit" in result.error and "4096 tokens" in result.error
    assert seen["max_tokens"] >= 4096
    full = Plan(objective="x" * 200, priority_rows=[0, 1, 2, 3, 4], threat_assessment=[
        {"row": r, "urgency": "high", "reason": "y" * 120} for r in range(5)],
        replan_when={"covered_rows": [0, 1, 2, 3, 4], "covered_types": ["normal", "conehead", "buckethead"],
                     "key_plants": [{"row": r, "col": c, "plant": "peashooter"} for r in range(5) for c in range(2)]})
    assert len(full.model_dump_json(indent=2)) / 3 < seen["max_tokens"]  # ~3 characters per token


def test_plan_text_is_short_by_schema_and_an_overrun_is_cut_not_fatal():
    """Output tokens are the Planner's latency (HoK keeps a goal under 300 characters)."""
    schema = Plan.model_json_schema()
    assert schema["properties"]["tactics"]["maxLength"] == 240
    assert schema["properties"]["objective"]["maxLength"] == 160
    assert schema["$defs"]["ThreatAssessment"]["properties"]["reason"]["maxLength"] == 80
    long = Plan(objective="o" * 500, tactics="t" * 900, note="n" * 400, budget_policy="b" * 700,
                threat_assessment=[{"row": 0, "urgency": "high", "reason": "r" * 300}])
    assert (len(long.objective), len(long.tactics), len(long.note), len(long.budget_policy)) == (160, 240, 160, 160)
    assert len(long.threat_assessment[0].reason) == 80
    with pytest.raises(ValueError):
        Plan(objective="")
