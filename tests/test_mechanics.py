"""Public reference is scoped, immutable to callers, and never live game state."""
import asyncio
import json
from typing import get_args

import httpx
import pytest
from fastapi.testclient import TestClient

from client.model_activity import ModelActivity
from client.operator_view import OperatorView
from server.app import create_app
from server.mechanics import DEFAULT_PLANTS, catalog, mechanics_context
from server.planner import MockPlanner, Plan, QwenPlanner
from server.scene import ZombieType
from tests.test_action_selector import context, scorer, selector
from tests.test_jev import scene, POLICY
from tests.test_server import settings


def test_reference_covers_current_cards_and_recognized_zombies():
    data = catalog()
    assert set(get_args(ZombieType)) == set(data["zombies"])
    assert all(data["plants"][p].get("sources") for p in DEFAULT_PLANTS)
    assert data["zombies"]["unknown"]["full_hp"] is None
    assert data["zombies"]["ducky tube"]["full_hp"] is None
    assert set(data["zombies"]["newspaper"]["seconds_per_tile"]) == {"paper_intact", "paper_destroyed"}
    assert data["plants"]["potato mine"]["arming_s_reference_range"] == [14, 15]
    assert all(key in data["sources"] for p in data["plants"].values() for key in p.get("sources", []))


def test_context_only_adds_relevant_static_reference_without_mutating_scene():
    observed = scene({1: 5}, types={1: "buckethead"})
    original = observed.model_dump()
    result = mechanics_context(observed, {(2, 1): ["pumpkin", "unverified plant"]})
    assert set(result["zombies"]) == {"buckethead"}
    assert set(result["plants"]) == set(DEFAULT_PLANTS) | {"pumpkin", "unverified plant"}
    assert result["plants"]["unverified plant"]["full_hp"] is None
    assert result["kind"] == "static_reference_not_live_state"
    assert observed.model_dump() == original
    assert "remaining_hp" not in json.dumps(result)
    result["zombies"]["buckethead"]["full_hp"] = 0
    data = catalog()
    data["plants"]["peashooter"]["full_hp"] = 0
    assert mechanics_context(observed)["zombies"]["buckethead"]["full_hp"] > 0
    assert catalog()["plants"]["peashooter"]["full_hp"] == 300


def test_both_decision_roles_receive_reference_without_jev_images():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": Plan(objective="defend").model_dump_json()}}]})
    planner = QwenPlanner("http://test", "test", False, True, 2,
                          httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = asyncio.run(planner.decide(scene({1: 5}), "", context={"plants": [{"row": 1, "col": 2, "plants": ["pumpkin"]}]}))
    assert result.error is None
    assert "static_reference_not_live_state" in json.dumps(requests)
    assert "pumpkin" in json.dumps(requests)
    seen = []
    asyncio.run(selector(scorer(seen=seen)).select(context(), POLICY))
    for request in seen:
        assert set(request) == {"model", "state", "questions"}
        state, _ = json.JSONDecoder().raw_decode(request["state"])
        assert state["game_reference"]["kind"] == "static_reference_not_live_state"


def test_reference_endpoint_requires_authentication(settings):
    with TestClient(create_app(settings, MockPlanner(delay=0))) as client:
        assert client.get("/mechanics").status_code == 401
        response = client.get("/mechanics", headers={"Authorization": "Bearer secret"})
        assert response.status_code == 200
        assert response.json()["version"] == client.get("/health").json()["mechanics_version"]


def test_operator_reference_preserves_limits_and_playbook_parameters():
    view = OperatorView("ws://test/ws", "test")
    view.mechanics = catalog()
    text = view.mechanics_text()
    assert "没有实时血量计算" in text and "秒/格" in text and "14秒与15秒" in text
    view.playbook = {"current": "v1", "playbook": {"planner": {"knowledge": "策略正文", "max_actions": 3},
                        "timing": {"danger_replan": True}}, "effective_timing": {"planner_seconds": 6}}
    assert "danger_replan" in view.playbook_text() and "max_actions" in view.playbook_text()


def test_model_rows_keep_distinct_states_and_frozen_age():
    pytest.importorskip("tkinter")
    from client.control_view import model_row
    sample = ModelActivity().snapshot("jev")
    assert model_row(sample)[0] == "未触发"
    sample.update(status="running", age_s=2.5, count=1, recent_count=1)
    assert model_row(sample)[1] == "已等待 2.5s"
    sample.update(status="completed", latency_ms=231)
    assert model_row(sample)[:2] == ("完成", "231 ms")
    sample.update(status="failed")
    assert model_row(sample)[0] == "失败"
