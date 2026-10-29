"""Model clients against fake HTTP transports (no shared pool is called)."""

import asyncio
import json

import httpx

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
    assert [part["type"] for part in content] == ["text"]
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
