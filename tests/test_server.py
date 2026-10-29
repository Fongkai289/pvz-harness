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
        plan_interval=0.2, fast_interval=0.05, pause_for_planner=False,
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


def test_pause_for_planner_wraps_the_call(settings):
    settings = replace(settings, pause_for_planner=True, fast_interval=10.0)
    client = TestClient(create_app(settings, MockPlanner(delay=0.1)))
    with client.websocket_connect("/ws") as ws:
        ws.send_text(dumps(Hello(token="secret", client_id="test")))
        ws.receive_text()
        ws.send_bytes(frame_with_sun(1, 300, 250))
        actions = [json.loads(ws.receive_text())["action"] for _ in range(2)]
        assert actions == ["pause", "resume"]


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
    from server.decider import NoJudge

    loadout = ["peashooter", "sunflower", "cherry bomb"]
    settings = replace(settings, planner_mode="split", loadout=loadout, look_interval=0.05,
                       plants_interval=0.05, fast_interval=0.05, plan_interval=30)
    client = TestClient(create_app(settings, MockPlanner(delay=0), NoJudge(), FakeLooker(loadout)))
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
    assert jev and jev[0]["action"] == "emergency" and jev[0]["rejected"] is None
    assert any(e["kind"] == "plants" for e in events)
