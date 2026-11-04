"""Automatic collection cannot grant arbitrary input or interfere with planting."""
import asyncio
import json
import time
from dataclasses import replace

import pytest

from common.protocol import EpisodeEvent, dumps
from server.perception import Blob, Observation
from server.planner import Placement
from server.sun_collector import SunCollector, MAX_FRAME_AGE
from tests.test_action_selector import session
from tests.test_server import settings, frame_with_sun


def seen(frame, x=300, y=350):
    return Observation(frame, time.time(), [Blob(x, y, 1200, 25)])


def confirmed(session):
    session.settings = replace(session.settings, decider="onejev")
    now = time.monotonic()
    old, new = seen(10), seen(11, x=305)
    session.sun_collector.observe(old, now - .2)
    session.sun_collector.observe(new, now)
    session.latest_obs, session.latest_recv = new, now


def test_confirmation_needs_distinct_recent_frames_matching_position_and_area():
    c = SunCollector()
    a, b = seen(1), seen(2, 310)
    c.observe(a, 1)
    assert not c.targets(a, 1)
    c.observe(a, 1.1)
    assert not c.targets(a, 1.1)
    c.observe(b, 1.3)
    assert c.targets(b, 1.3) == b.suns
    assert not c.targets(a, 1.3)
    assert not c.targets(b, 1.3 + MAX_FRAME_AGE + .01)
    c.observe(seen(3, 500), 1.5)
    assert not c.confirmed
    c.observe(seen(4, 500), 4)
    assert not c.confirmed  # long pause must reconfirm, not reuse an old target
    c.observe(Observation(5, 0, [Blob(500, 350, 100, 25)]), 4.2)
    assert not c.confirmed


def test_a_landed_sun_confirms_at_one_frame_per_second_but_a_falling_one_waits():
    c = SunCollector()
    c.observe(seen(1, 300), 10.0)
    c.observe(seen(2, 300), 11.4)  # slow link: frames 1.4 s apart, the sun has not moved
    assert c.confirmed
    c.observe(Observation(3, 0, [Blob(300, 200, 2000, 25)]), 12.4)
    c.observe(Observation(4, 0, [Blob(300, 267, 2000, 25)]), 13.4)  # falling 67 px/s
    assert not c.confirmed


@pytest.mark.parametrize("x,y", [(50, 30), (490, 40), (740, 25), (500, 585), (795, 300), (-1, 350)])
def test_collector_excludes_cards_shovel_menu_and_progress_ui(x, y):
    c = SunCollector()
    c.observe(seen(1, x, y), 1)
    c.observe(seen(2, x, y), 1.2)
    assert not c.targets(c.current, 1.2)


def test_collects_without_any_model_context_or_spending_budget(session):
    confirmed(session)
    session.directive = session.latest_scene = session.latest_zombies = None
    session.split_vision = True
    events = []
    session.runlog.event = lambda kind, **data: events.append((kind, data))
    asyncio.run(session._collect_suns())
    assert [(c["x"], c["y"]) for c in session.ws.sent] == [(305, 350)]
    assert session._model_calls["jev"] == 0
    assert session._sun_permit is None
    assert next(data for kind, data in events if kind == "cmd")["meta"]["decision_source"] == "sun_collector"
    assert session.sun_spent == session.directive_spent == 0


def test_real_frame_path_collects_only_after_confirmation_and_deduplicates(session):
    session.settings = replace(session.settings, decider="onejev")
    async def run():
        await session.on_frame(frame_with_sun(1, 300, 350))
        await session._collect_suns()
        assert not session.ws.sent
        await session.on_frame(frame_with_sun(2, 300, 350))
        await session._collect_suns()
        await session._collect_suns()  # same frame
        await session.on_frame(frame_with_sun(3, 303, 350))
        await session._collect_suns()  # new frame, cooldown still applies
    asyncio.run(run())
    assert len(session.ws.sent) == 1


@pytest.mark.parametrize("block", ["paused", "ended", "stale", "new_game"])
def test_pause_end_stale_frames_and_new_game_stop_collection(session, block):
    confirmed(session)
    async def run():
        if block == "paused": session.paused = True
        if block == "ended": session.episode_active = False
        if block == "stale": session.sun_collector.received -= 2
        if block == "new_game": await session.on_text(dumps(EpisodeEvent(event="start", level="1-1")))
        await session._collect_suns()
    asyncio.run(run())
    assert not session.ws.sent


def test_metadata_alone_cannot_forge_automatic_input(session):
    confirmed(session)
    with pytest.raises(PermissionError):
        asyncio.run(session.send("click", session.latest_obs, meta={"decision_source": "sun_collector"},
                                 x=305, y=350, reason="collect sun"))
    click = session.policy.sun_candidates(session.latest_obs)[0]
    session._sun_permit = (session.latest_obs, click)
    with pytest.raises(PermissionError):
        asyncio.run(session.send("click", session.latest_obs, meta={"decision_source": "sun_collector"},
                                 x=200, y=30, reason="collect sun"))
    assert not session.ws.sent


@pytest.mark.parametrize("change", ["pause", "end", "new_frame"])
def test_send_revalidates_when_waiting_on_socket_lock(session, change):
    confirmed(session)
    async def run():
        await session._send_lock.acquire()
        task = asyncio.create_task(session._collect_suns())
        await asyncio.sleep(0)
        assert session._sun_permit is not None
        if change == "pause": session.paused = True
        if change == "end": session.episode_active = False
        if change == "new_frame": session.latest_obs = seen(12, 500)
        session._send_lock.release()
        await task
    asyncio.run(run())
    assert not session.ws.sent and session._sun_permit is None
    assert not session.policy._recent_sun_clicks


def test_collecting_never_interleaves_select_card_and_plant_clicks(session):
    confirmed(session)
    session._model_calls["jev"] = 1
    session._selected_context = session._decision_context()
    async def run():
        selected, release = asyncio.Event(), asyncio.Event()
        async def send(text):
            command = json.loads(text)
            session.ws.sent.append(command)
            if command["reason"].startswith("select slot"):
                selected.set()
                await release.wait()
        session.ws.send_text = send
        planting = asyncio.create_task(session._execute_placement(Placement(slot=1, row=3, col=7)))
        await selected.wait()
        collector = asyncio.create_task(session._collect_suns())
        await asyncio.sleep(0)
        assert len(session.ws.sent) == 1
        release.set()
        await asyncio.gather(planting, collector)
    asyncio.run(run())
    assert [c["reason"] for c in session.ws.sent] == ["select slot 1", "plant at row 3 col 7", "collect sun"]


def test_slow_or_failed_jev_does_not_hold_up_sun_loop(session):
    from server.action_selector import Selection
    confirmed(session)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowSelector:
            async def select(self, ctx, policy):
                entered.set()
                await release.wait()
                return Selection(source="failed", error="timeout", latency=1)
        session.selector = SlowSelector()
        thinking = asyncio.create_task(session._jev_step(session.latest_obs))
        await entered.wait()
        await session._collect_suns()
        assert len(session.ws.sent) == 1 and not thinking.done()
        release.set()
        await thinking
    asyncio.run(run())
    assert session.ws.sent[0]["reason"] == "collect sun"


def test_live_websocket_collects_before_first_planner_or_jev_call(settings):
    from fastapi.testclient import TestClient
    from common.protocol import Hello
    from server.app import create_app
    from server.planner import MockPlanner
    from tests.test_server import FakeLooker
    settings = replace(settings, decider="onejev", fast_interval=10, look_interval=30,
                       plan_interval=30, plants_interval=30)
    with TestClient(create_app(settings, MockPlanner(delay=30), looker=FakeLooker(settings.loadout))) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(dumps(Hello(token="secret", client_id="collection-test")))
            welcome = json.loads(ws.receive_text())
            ws.send_bytes(frame_with_sun(1, 300, 350))
            ws.send_bytes(frame_with_sun(2, 300, 350))
            click = json.loads(ws.receive_text())
            assert click["reason"] == "collect sun" and click["frame_id"] == 2
        events = [json.loads(line) for line in (settings.runs_dir / welcome["session_id"] / "events.jsonl").read_text().splitlines()]
        assert not any(e["kind"] == "jev" for e in events)
        assert any(e["kind"] == "cmd" and e["meta"]["decision_source"] == "sun_collector" for e in events)
