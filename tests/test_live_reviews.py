import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from common.protocol import Hello, StateMsg, EpisodeEvent, dumps
from rsi.review_live import LiveReview
from server.app import create_app
from server.planner import MockPlanner
from tests.test_server import settings


@pytest.mark.parametrize("outcome", ["win", "lose", None])
def test_confirmed_result_triggers_coach_once_after_close_but_pause_does_not(settings, monkeypatch, outcome):
    settings = replace(settings, auto_native_rsi=True)
    calls = []
    async def fake_review(path, settings):
        es = [json.loads(x) for x in path.read_text().splitlines()]
        assert es[-1]["kind"] == "session_closed"
        calls.append(path)
        result = LiveReview(diagnosis="测试", evidence=["状态事件"], objective="验证", uncertainties=["片段"])
        return dict(session=path.parent.name, coach=result.model_dump(), model_call={"model": "fake"}), path.parent
    monkeypatch.setattr("server.live_reviews.review", fake_review)
    app = create_app(settings, MockPlanner(0))
    with TestClient(app) as client:
        assert client.get("/playbook").status_code == 401
        headers = {"Authorization": "Bearer secret"}
        assert client.get("/playbook", headers=headers).json()["current"] == "v001"
        with client.websocket_connect("/ws") as ws:
            ws.send_text(dumps(Hello(token="secret", client_id="windows")))
            session_id = json.loads(ws.receive_text())["session_id"]
            ws.send_text(dumps(EpisodeEvent(event="start")))
            ws.send_text(dumps(StateMsg(t=0, data={"game": "real_pvz", "outcome": outcome,
                                                 "outcome_source": "goty-1096-readonly-result"})))
            ws.send_text(dumps(EpisodeEvent(event="end", info={"reason": "game_result" if outcome else "operator_or_disconnect"})))
        for _ in range(30):
            status = client.get(f"/reviews/{session_id}", headers=headers).json()
            if status["status"] == "completed" or outcome is None:
                break
            time.sleep(.02)
        assert len(calls) == (1 if outcome else 0)
        assert status["status"] == ("completed" if outcome else "not_triggered")
        assert client.get("/reviews/latest").status_code == 401
        assert client.get("/learning/latest").status_code == 401
        learning = client.get("/learning/latest", headers=headers).json()
        assert learning["status"] == ("waiting_runtime" if outcome else "not_triggered")
        if outcome:
            assert learning["source_session"] == session_id
            assert learning["candidate"] is None
            assert "suites" not in learning


@pytest.mark.parametrize("outcome,mowers,improve", [("win", 0, False), ("win", 2, True), ("lose", 0, True),
                                                    ("win", None, True)])
def test_only_games_that_cost_a_mower_get_a_coach_review(settings, monkeypatch, outcome, mowers, improve):
    from server.live_reviews import needs_improvement
    assert needs_improvement(outcome, mowers) is improve
    settings = replace(settings, auto_native_rsi=True)
    async def fake_review(path, settings):
        result = LiveReview(diagnosis="测试", evidence=["状态事件"], objective="验证", uncertainties=["片段"])
        return dict(session=path.parent.name, coach=result.model_dump(), model_call={"model": "fake"}), path.parent
    monkeypatch.setattr("server.live_reviews.review", fake_review)
    state = {"game": "real_pvz", "outcome": outcome, "outcome_source": "goty-1096-readonly-result"}
    if mowers is not None:
        state["mowers_used"] = mowers
    with TestClient(create_app(settings, MockPlanner(0))) as client:
        headers = {"Authorization": "Bearer secret"}
        with client.websocket_connect("/ws") as ws:
            ws.send_text(dumps(Hello(token="secret", client_id="windows")))
            session_id = json.loads(ws.receive_text())["session_id"]
            ws.send_text(dumps(EpisodeEvent(event="start")))
            ws.send_text(dumps(StateMsg(t=0, data=state)))
            ws.send_text(dumps(EpisodeEvent(event="end", info={"reason": "game_result"})))
        for _ in range(30):
            status = client.get(f"/reviews/{session_id}", headers=headers).json()
            if status["status"] in ("completed", "skipped"):
                break
            time.sleep(.02)
        if improve:
            assert status["status"] == "completed" and status["native_rsi_requested"] is True
        else:  # a win without a mower: no Coach call, nothing to train
            assert status["status"] == "skipped" and "review" not in status and status["reason"]
        learning = client.get("/learning/latest", headers=headers).json()
        assert learning["status"] == ("waiting_runtime" if improve else "not_triggered")
