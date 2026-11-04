import json
import time
from dataclasses import replace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from common.protocol import EpisodeEvent, FrameHeader, Hello, StateMsg, dumps, pack_frame
from server.app import create_app
from server.config import Settings
from server.planner import MockPlanner


@pytest.fixture(autouse=True)
def synthetic_card_reader(monkeypatch):
    # These tests exercise session scheduling and authority, not copyrighted card art.
    monkeypatch.setattr("server.seed_packets.read_bank", lambda image: [])
    monkeypatch.setattr("server.session.known_plants", lambda: ["peashooter", "sunflower", "cherry bomb"])


@pytest.fixture
def settings(tmp_path):
    return Settings(
        host="127.0.0.1", port=0, token="secret", planner="mock",
        planner_model="unused", planner_effort="low", planner_send_image=False,
        planner_include_summary=True, planner_timeout=5.0, planner_mode="end_to_end", loadout=["peashooter", "sunflower"],
        look_interval=1.0, plants_interval=1.0,
        qwen_url="http://qwen.invalid", qwen_model="Qwen3.8-27B",
        decider="rules", onejev_url="http://onejev.invalid", onejev_model="OneJev-27B",
        decider_timeout=1.0,
        plan_interval=0.2, fast_interval=0.05,
        runs_dir=tmp_path / "runs", data_dir=tmp_path / "data",
        frame_log_interval=0.0, max_run_bytes=10_000_000,
    )


def frame_with_sun(frame_id: int, x: int, y: int) -> bytes:
    img = np.zeros((600, 800, 3), np.uint8)
    img[:] = (40, 140, 40)
    cv2.circle(img, (x, y), 20, (0, 215, 255), -1)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return pack_frame(FrameHeader(frame_id=frame_id, t_capture=time.time(), width=800, height=600),
                      buf.tobytes())


def test_rejects_bad_token(settings):
    client = TestClient(create_app(settings, MockPlanner(delay=0)))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(dumps(Hello(token="wrong", client_id="t")))
        msg = ws.receive()
        assert msg["type"] == "websocket.close"
        assert msg["code"] == 1008


def test_opt_in_client_receives_model_timing_separately_from_commands(settings):
    settings = replace(settings, look_interval=0.01, plants_interval=0.01, fast_interval=10)
    client = TestClient(create_app(settings, MockPlanner(delay=0), looker=FakeLooker(settings.loadout)))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(dumps(Hello(token="secret", client_id="metrics", telemetry=True)))
        assert json.loads(ws.receive_text())["telemetry"] is True
        ws.send_bytes(frame_with_sun(1, 300, 250))
        samples = []
        while True:
            sample = json.loads(ws.receive_text())
            samples.append(sample)
            if sample.get("status") == "completed":
                break
        assert sample["type"] == "model_timing"
        assert sample["stage"] in {"scene", "plants"}
        assert sample["latency_ms"] == 10
        assert sample["error"] is False
        assert any(s.get("status") == "idle" for s in samples)
        assert any(s.get("status") == "running" for s in samples)
        assert sample["call_id"] == 1


def test_model_loop_retries_after_unexpected_failure(settings):
    class RecoveringLooker(FakeLooker):
        calls = 0

        async def look(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("injected failure")
            return await super().look(*args, **kwargs)

    settings = replace(settings, look_interval=0.01, plants_interval=10, fast_interval=10)
    client = TestClient(create_app(settings, MockPlanner(delay=0), looker=RecoveringLooker(settings.loadout)))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(dumps(Hello(token="secret", client_id="retry", telemetry=True)))
        ws.receive_text()
        ws.send_bytes(frame_with_sun(1, 300, 250))
        samples = []
        while True:
            sample = json.loads(ws.receive_text())
            samples.append(sample)
            if sample.get("stage") == "scene" and sample.get("status") == "completed":
                break
        assert sample["call_id"] == 2
        assert any(s.get("status") == "failed" and s.get("call_id") == 1 for s in samples)


def test_sun_in_frame_gets_clicked_and_logged(settings):
    client = TestClient(create_app(settings, MockPlanner(delay=0)))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(dumps(Hello(token="secret", client_id="test")))
        session_id = json.loads(ws.receive_text())["session_id"]
        ws.send_text(dumps(EpisodeEvent(event="start", level="1-1")))
        ws.send_bytes(frame_with_sun(1, 300, 250))
        ws.send_text(dumps(StateMsg(t=time.time(), frame_id=1, data={"sun": 50})))
        cmd = json.loads(ws.receive_text())
        assert cmd["action"] == "click"
        assert abs(cmd["x"] - 300) < 5 and abs(cmd["y"] - 250) < 5
        assert cmd["frame_id"] == 1
        ws.send_text(dumps(EpisodeEvent(event="end", level="1-1")))

    events = [json.loads(line) for line in (settings.runs_dir / session_id / "events.jsonl").open()]
    kinds = {e["kind"] for e in events}
    assert {"hello", "frame", "state", "cmd", "episode", "session_closed"} <= kinds
    assert all("token" not in e for e in events)
    assert list((settings.runs_dir / session_id / "frames").glob("*.jpg"))


def test_planner_has_no_input_authority(settings):
    settings = replace(settings, fast_interval=10.0)
    client = TestClient(create_app(settings, MockPlanner(delay=0.1)))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(dumps(Hello(token="secret", client_id="test")))
        session_id = json.loads(ws.receive_text())["session_id"]
        ws.send_bytes(frame_with_sun(1, 300, 250))
        time.sleep(.5)
    events = [json.loads(line) for line in (settings.runs_dir / session_id / "events.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "plan" for e in events)
    assert not any(e["kind"] == "cmd" for e in events)


class FakeLooker:
    """Stands in for the Qwen look step: a zombie close in row 2, a cherry bomb ready, one sunflower seen."""

    def __init__(self, loadout):
        from server.scene import Card, Scene, Zombie
        self.scene = Scene(frame_id=0, lawn="day", rows=5, sun=300,
                           cards=[Card(slot=i, plant=p, ready=True, brightness=200) for i, p in enumerate(loadout)],
                           zombies=[Zombie(type="football", row=2)], nearest_col={2: 2})

    async def look(self, image, frame_id, loadout, t_capture=0.0):
        from server.scene import LookResult
        return LookResult(self.scene.model_copy(update={"frame_id": frame_id}), 0.01)

    async def look_plants(self, image, loadout):
        from server.scene import PlantsResult
        return PlantsResult({(1, 0): ["sunflower"]}, 0.01)


def test_split_mode_end_to_end_with_jev(settings):
    """Look -> Jev (rules) -> hard rules -> clicks, through a real session (caught a name clash once)."""
    from server import geometry
    from server.action_selector import RulesSelector
    from server.planner import Plan, PlanResult

    class GuidancePlanner(MockPlanner):
        async def decide(self, scene, knowledge, jpeg=None, context=None):
            return PlanResult(Plan(objective="Defend threatened rows", spend_limit=300), 0.01)

    loadout = ["peashooter", "sunflower", "cherry bomb"]
    settings = replace(settings, planner_mode="split", loadout=loadout, look_interval=0.05,
                       plants_interval=0.05, fast_interval=0.05, plan_interval=30)
    client = TestClient(create_app(settings, GuidancePlanner(delay=0), RulesSelector(), FakeLooker(loadout)))
    clicks = []
    with client.websocket_connect("/ws") as ws:
        ws.send_text(dumps(Hello(token="secret", client_id="split")))
        session_id = json.loads(ws.receive_text())["session_id"]
        deadline = time.time() + 5
        while time.time() < deadline and len(clicks) < 3:
            ws.send_bytes(frame_with_sun(len(clicks) + 1, 700, 560))
            cmd = json.loads(ws.receive_text())
            if cmd["action"] == "click" and cmd["reason"] != "collect sun":
                clicks.append((cmd["x"], cmd["y"], cmd["reason"]))
    assert clicks[0][:2] == geometry.seed_slot_center(2)  # cherry bomb card
    assert clicks[1][:2] == geometry.cell_center(2, 2)  # on the close zombie's cell
    events = [json.loads(line) for line in (settings.runs_dir / session_id / "events.jsonl").open()]
    jev = [e for e in events if e["kind"] == "jev"]
    assert jev and jev[0]["action"] == "plant" and jev[0]["rejected"] is None
    assert jev[0]["selector_source"] == "rules" and "emergency" in jev[0]["reason"]
    assert any(e["kind"] == "plants" for e in events)
