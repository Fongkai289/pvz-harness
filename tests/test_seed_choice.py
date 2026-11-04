"""选植物: code reads the seed chooser, Qwen picks, the client clicks the packets; the operator starts the level."""
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import pytest

from server import geometry
from server.seed_choice import choose, packet_center, packet_rect, read_chooser
from tests.test_server import settings

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SEEN = ["peashooter", "sunflower", "cherry bomb", "wall-nut", "potato mine", "snow pea", "chomper"]


def chooser_image():
    return cv2.imread(str(FIXTURES / "seed_chooser.jpg"))  # live 1.2.0.1096, 2026-11-03: 7 plants, 6 slots


class Looker:
    """Records the request; answers with a fixed Qwen JSON message."""
    model, url = "fixture-qwen", "http://qwen.invalid/v1/chat/completions"

    def __init__(self, answer):
        self.answer, self.bodies = answer, []
        self.client = self

    async def aclose(self):
        pass

    async def post(self, url, json=None):
        self.bodies.append(json)
        content = self.answer if isinstance(self.answer, str) else __import__("json").dumps(self.answer)
        if self.answer is None:  # the model server refuses the request
            return SimpleNamespace(status_code=400, text='{"error":{"message":"Grammar error"}}')
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: {"choices": [{"message": {"content": content}}]})


PICK = {"zombies": ["normal", "conehead", "buckethead"], "reason": "Economy, attackers and answers to the bucket.",
        "plants": ["sunflower", "peashooter", "snow pea", "wall-nut", "cherry bomb", "potato mine"]}


def test_chooser_is_read_by_code_from_the_live_screen():
    chooser = read_chooser(chooser_image())
    assert chooser.slots == 6 and chooser.available == SEEN
    assert packet_center(6) == (365, 163)  # chomper, chooser index = seed type


@pytest.mark.parametrize("case", ["gameplay", "bank_not_empty"])
def test_anything_but_a_fresh_chooser_is_refused(case):
    image = cv2.imread(str(FIXTURES / "day_1-6_f0073.jpg")) if case == "gameplay" else chooser_image()
    if case == "bank_not_empty":
        x0, y0, x1, y1 = packet_rect(1)
        bx0, by0, bx1, by1 = geometry.seed_card_rect(0)
        image[by0:by1, bx0:bx1] = cv2.resize(image[y0:y1, x0:x1], (bx1 - bx0, by1 - by0))
    with pytest.raises(ValueError, match="选卡界面|卡槽里已经有卡"):
        read_chooser(image)


def test_qwen_must_pick_exactly_the_free_slots_from_what_is_offered():
    image, chooser = chooser_image(), read_chooser(chooser_image())
    looker = Looker(PICK)
    result = asyncio.run(choose(looker, image, chooser, "Bring wall-nuts against buckets."))
    assert result["plants"] == PICK["plants"] and result["zombies"] == PICK["zombies"]
    assert [(c["x"], c["y"]) for c in result["clicks"]] == [packet_center(SEEN.index(p)) for p in PICK["plants"]]
    body = looker.bodies[0]
    schema = body["response_format"]["json_schema"]["schema"]["properties"]["plants"]
    assert schema["minItems"] == schema["maxItems"] == 6 and schema["items"]["enum"] == SEEN
    assert "uniqueItems" not in schema  # vLLM's grammar rejects it with HTTP 400
    with pytest.raises(RuntimeError, match="HTTP 400: .*Grammar error"):
        asyncio.run(choose(Looker(None), image, chooser, ""))
    assert body["messages"][0]["content"][0]["type"] == "image_url"
    assert "Bring wall-nuts against buckets." in body["messages"][0]["content"][1]["text"]
    for plants in (PICK["plants"][:5], PICK["plants"][:5] + ["sunflower"], PICK["plants"][:5] + ["repeater"]):
        with pytest.raises(RuntimeError):
            asyncio.run(choose(Looker({**PICK, "plants": plants}), image, chooser, ""))


def test_endpoint_needs_the_token_records_the_choice_and_refuses_other_screens(settings):
    import base64
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.planner import MockPlanner
    app = create_app(settings, planner=MockPlanner(0), looker=Looker(PICK))
    client = TestClient(app)
    def post(image, token=settings.token):
        jpeg = base64.b64encode(cv2.imencode(".jpg", image)[1]).decode()
        return client.post("/seed_choice", json={"jpeg": jpeg}, headers={"Authorization": f"Bearer {token}"})
    assert post(chooser_image(), "wrong").status_code == 401
    answer = post(chooser_image())
    assert answer.status_code == 200 and answer.json()["plants"] == PICK["plants"]
    records = list((settings.data_dir / "seed_choices").glob("*.json"))
    assert len(records) == 1 and json.loads(records[0].read_text(encoding="utf-8"))["available"] == SEEN
    refused = post(cv2.imread(str(FIXTURES / "day_1-6_f0073.jpg")))
    assert refused.status_code == 409 and "选卡界面" in refused.json()["detail"]


# ---- client ----

@pytest.fixture
def wc():
    return pytest.importorskip("client.windows_client")  # Windows only; the server tests above still run


class Game:
    hwnd = 1

    def __init__(self, frames):
        self.frames = frames
    def capture(self):
        return self.frames[0] if len(self.frames) == 1 else self.frames.pop(0)
    def has_focus(self):
        return True
    def client_rect(self):
        return 1000, 50, 800, 600
    def owns(self, x, y):
        return True


def run_choice(wc, monkeypatch, frames, answer=None, link_active=False):
    clicks, posted = [], []
    def post(url, json=None, headers=None, timeout=None, trust_env=None):
        posted.append((url, headers))
        data = answer or {**PICK, "clicks": [dict(zip(("x", "y"), packet_center(SEEN.index(p))), plant=p)
                                             for p in PICK["plants"]]}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: data)
    monkeypatch.setattr(wc, "focus_for_operator", lambda hwnd: None)
    monkeypatch.setattr(wc, "click_screen", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(wc.httpx, "post", post)
    monkeypatch.setattr(wc.time, "sleep", lambda s: None)
    active = threading.Event()
    if link_active:
        active.set()
    view = SimpleNamespace(base="http://server.invalid", token="test-only")
    message = wc.choose_seeds(Game(frames), view, threading.RLock(), active)
    return message, clicks, posted


def test_client_clicks_each_chosen_packet_and_leaves_the_start_to_the_operator(wc, monkeypatch):
    message, clicks, posted = run_choice(wc, monkeypatch, [chooser_image()])
    assert posted[0][0] == "http://server.invalid/seed_choice" and posted[0][1]["Authorization"] == "Bearer test-only"
    assert clicks == [(1000 + x, 50 + y) for x, y in (packet_center(SEEN.index(p)) for p in PICK["plants"])]
    assert "一起摇滚吧" in message and "sunflower" in message


def test_client_stops_when_the_screen_changes_or_the_server_points_elsewhere(wc, monkeypatch):
    gameplay = cv2.imread(str(FIXTURES / "day_1-6_f0073.jpg"))
    message, clicks, _ = run_choice(wc, monkeypatch, [chooser_image()] * 4 + [gameplay])  # two looks before, one per click
    assert len(clicks) == 2 and "离开了选卡界面" in message
    elsewhere = {**PICK, "clicks": [{"x": 232, "y": 566, "plant": "sunflower"}]}  # the start button
    message, clicks, _ = run_choice(wc, monkeypatch, [chooser_image()], elsewhere)
    assert not clicks and "不在选卡区" in message
    message, clicks, posted = run_choice(wc, monkeypatch, [gameplay])
    assert not clicks and not posted and "不是选卡界面" in message


def test_choose_button_only_outside_a_connection_and_once_at_a_time(wc):
    panel = wc.ControlPanel(Mock())
    assert not panel.button_states()["选植物"]  # needs the server view
    panel.operator_view = SimpleNamespace()
    assert panel.button_states()["选植物"]
    panel.link_active.set()
    assert not panel.button_states()["选植物"]
    panel.link_active.clear()
    panel.choosing = True
    assert not panel.button_states()["选植物"]


# ---- training (Portable): the same choice from the chooser's public facts as text ----

OFFER = {"level": 8, "slots": 6, "available": SEEN, "preview": ["normal", "conehead", "buckethead"]}


def test_training_asks_qwen_in_words_with_the_level_offer_and_preview():
    from server.seed_choice import choose_text
    looker = Looker(PICK)
    result = asyncio.run(choose_text(looker, 8, OFFER["preview"], SEEN, 6, "Notes."))
    assert result["plants"] == PICK["plants"] and "clicks" not in result
    content = looker.bodies[0]["messages"][0]["content"]
    assert [part["type"] for part in content] == ["text"]
    assert "level 1-8" in content[0]["text"] and "conehead, buckethead" in content[0]["text"]


def run_bridge(settings, monkeypatch, answer, offer=OFFER):
    from rsi.native_bridge import Bridge
    from rsi.native_jobs import digest
    async def run():
        bridge = Bridge(settings)
        try:
            monkeypatch.setattr(bridge, "looker", Looker(answer))
            job = bridge.jobs.enqueue("seed-choice")
            return await bridge.handle(dict(op="choose_seeds", job_id=job["id"],
                                            request_digest=digest(bridge.jobs.request(job)), **offer))
        finally:
            await bridge.close()
    return asyncio.run(run())


def test_bridge_picks_for_training_and_keeps_the_fixed_order_if_qwen_fails(settings, monkeypatch):
    assert run_bridge(settings, monkeypatch, PICK)["plants"] == PICK["plants"]
    failed = run_bridge(settings, monkeypatch, {**PICK, "plants": ["sunflower"]})
    assert failed["plants"] == SEEN[:6] and "不合法" in failed["error"]
    with pytest.raises(ValueError):
        run_bridge(settings, monkeypatch, PICK, {**OFFER, "available": SEEN + ["sunflower"]})


def test_training_episode_chooses_seeds_before_the_level_starts(tmp_path):
    from rsi.native_loop import run_episode
    from tests.test_native_throughput import Arena, RPC
    calls = []
    class ChooserArena(Arena):
        def prepare(self, level, seed):
            calls.append(("prepare", level, seed))
            return dict(OFFER, level=level), None
        def choose(self, plants):
            calls.append(("choose", plants))
            return self.reset(2, 17)
    class ChoiceRPC(RPC):
        def call(self, op, **payload):
            if op == "choose_seeds":
                calls.append(("choose_seeds", payload["available"], payload["preview"]))
                return {"plants": PICK["plants"], "zombies": payload["preview"], "reason": "fixture"}
            return super().call(op, **payload)
    result = run_episode(ChooserArena(), {"job_id": "fixture", "mode": "state"}, {"level": 2, "seed": 17},
                         ChoiceRPC(), tmp_path)
    assert [c[0] for c in calls] == ["prepare", "choose_seeds", "choose"]
    assert calls[2] == ("choose", PICK["plants"]) and result["seed_choice"]["plants"] == PICK["plants"]


def test_portable_arena_speaks_the_harness_prepare_and_choose_commands():
    from rsi.portable_runtime import PortableArena
    arena = PortableArena.__new__(PortableArena)
    sent = []
    snapshot = {"tick": 0, "sun": 50, "wave": 0, "waves": 10, "plants": [], "zombies": [], "cards": [],
                "mower_ticks": [-1] * 6, "allowed": [], "outcome": "running", "terminal": False}
    def call(command):
        sent.append(command)
        if command.startswith("prepare"):
            return {"ok": True, "chooser": True, "level": 8, "slots": 6, "available": [0, 1, 2, 3, 4, 5, 6],
                    "preview": [0, 2, 4]}
        return snapshot
    arena.engine, arena.has_reset, arena.seen_mowers = SimpleNamespace(call=call), False, set()
    offer, info = arena.prepare(8, 11)
    assert info is None and offer == {"level": 8, "slots": 6, "available": SEEN,
                                      "preview": ["normal", "conehead", "buckethead"]}
    arena.choose(["chomper", "sunflower"])
    assert sent == ["prepare 8 11", "choose 6 1"]


@pytest.mark.parametrize("activates", [True, False])
def test_an_unfocused_game_is_activated_by_a_click_on_the_chooser_title(wc, monkeypatch, activates):
    clicks = []
    class Unfocused(Game):
        def has_focus(self):
            return activates and bool(clicks)  # Windows refuses SetForegroundWindow; a click activates it
    monkeypatch.setattr(wc, "focus_for_operator", lambda hwnd: None)
    monkeypatch.setattr(wc, "click_screen", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(wc.time, "sleep", lambda s: None)
    data = {**PICK, "clicks": [dict(zip(("x", "y"), packet_center(SEEN.index(p))), plant=p) for p in PICK["plants"]]}
    monkeypatch.setattr(wc.httpx, "post", lambda *a, **k: SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                                                         json=lambda: data))
    view = SimpleNamespace(base="http://server.invalid", token="test-only")
    message = wc.choose_seeds(Unfocused([chooser_image()]), view, threading.RLock(), threading.Event())
    assert clicks[0] == (1000 + wc.CHOOSER_TITLE[0], 50 + wc.CHOOSER_TITLE[1])
    if activates:
        assert len(clicks) == 7 and "一起摇滚吧" in message
    else:
        assert len(clicks) == 1 and "没能切到前台" in message
