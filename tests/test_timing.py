import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rsi.coach import Proposal, diff, player_profile
from rsi.human_edit import edit
from rules.playbook_rules import TimingParams
from server.config import effective_settings, load_settings
from server.playbook import PlaybookStore
from server.session import Session
from tests.test_action_selector import session
from tests.test_server import settings


def test_defaults_and_version_overrides_do_not_mutate_server(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("PVZ_TOKEN", "test")
    monkeypatch.delenv("PVZ_PLANTS_INTERVAL", raising=False)
    monkeypatch.delenv("PVZ_PLAN_INTERVAL", raising=False)
    defaults = load_settings(tmp_path / "absent.env")
    assert (defaults.plants_interval, defaults.plan_interval) == (4, 6)
    book = PlaybookStore(settings.data_dir).load()
    assert effective_settings(settings, book) == settings
    book, changes = edit(book, "v002", "timing", sets=["timing.plants_seconds=4", "timing.planner_seconds=12"])
    tuned = effective_settings(settings, book)
    assert (tuned.plants_interval, tuned.plan_interval) == (4, 12)
    assert settings.plan_interval == .2
    assert changes["timing.planner_seconds"] == [None, 12]
    assert player_profile(settings, book)["planner"]["every_seconds"] == 12
    sess = Session(SimpleNamespace(), settings, None, None, book, "timing-test")
    assert sess.settings == tuned
    sess.close()


@pytest.mark.parametrize("field,value", [("plants_seconds", 0), ("plants_seconds", True),
    ("plants_seconds", 31), ("planner_seconds", 3), ("planner_seconds", 61),
    ("planner_seconds", float("nan")), ("planner_seconds", float("inf"))])
def test_timing_rejects_unbounded_or_invalid_values(field, value):
    with pytest.raises(ValidationError):
        TimingParams(**{field: value})


def test_coach_can_propose_versioned_timing_without_changing_strategy(settings):
    book = PlaybookStore(settings.data_dir).load()
    proposal = Proposal(timing=TimingParams(plants_seconds=4, planner_seconds=12), rationale="measure latency")
    candidate = proposal.apply(book, "v002")
    assert candidate.planner == book.planner
    assert candidate.parent == book.version
    assert diff(book, proposal)["timing.plants_seconds"] == [None, 4]
    assert "jev" not in candidate.active_dump("onejev")
    assert "jev" in candidate.active_dump("rules")


@pytest.mark.parametrize("duration,expected", [(2.5, [104, 108, 112]), (6, [104, 110, 116])])
def test_plant_cadence_includes_inference_time_without_overlap(session, monkeypatch, duration, expected):
    clock = [100.0]
    starts = []
    async def sleep(delay):
        clock[0] += delay
    async def call(stage, callback):
        starts.append(clock[0])
        clock[0] += duration
        if len(starts) == 3:
            raise asyncio.CancelledError
        return None
    monkeypatch.setattr("server.session.time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr("server.session.asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(session, "_fresh_obs", lambda: session.latest_obs)
    monkeypatch.setattr(session, "_call_model", call)
    session.settings = replace(session.settings, plants_interval=4)
    session.looker = SimpleNamespace()
    session.latest_jpeg = b"fixture"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session.plants_loop())
    assert starts == expected


def test_skipped_look_runs_when_the_next_frame_lands_not_at_the_next_tick(session, monkeypatch):
    # A slow link delivers a frame every ~5 s; a 4 s tick kept missing the 2 s freshness window.
    fresh, starts = [None], []
    async def idle(*args, **kwargs):
        pass
    async def call(stage, callback):
        starts.append(asyncio.get_running_loop().time())
        raise asyncio.CancelledError
    monkeypatch.setattr(session, "_fresh_obs", lambda: fresh[0])
    monkeypatch.setattr(session, "report_model_state", idle)
    monkeypatch.setattr(session, "_call_model", call)
    session.settings = replace(session.settings, plants_interval=0.4)
    session.looker = SimpleNamespace()
    session.latest_jpeg = b"fixture"
    async def scenario():
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(session.plants_loop())
        await asyncio.sleep(0.5)  # the first tick found no fresh frame
        fresh[0] = SimpleNamespace(frame_id=1)
        arrived = loop.time()
        session._frame_arrived.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return arrived
    arrived = asyncio.run(scenario())
    assert starts and starts[0] - arrived < 0.2  # the next fixed tick would have been 0.3 s later


def test_planner_period_counts_inference_and_refreshes_before_expiry(session, monkeypatch):
    monkeypatch.setattr("server.session.time", SimpleNamespace(monotonic=lambda: 105.0))
    session.settings = replace(session.settings, plan_interval=30)
    session._planner_started = 100
    session.directive_received = 104
    session._planner_latency = 4
    session.directive.valid_for_seconds = 60
    assert session._plan_delay() == 25  # the 30 s cycle counts from the call's start, inference included
    session.directive.valid_for_seconds = 5
    assert session._plan_delay() == 25  # a short validity is raised to 40 s and cannot shorten the cycle
    session.settings = replace(session.settings, plan_interval=4)
    assert session._plan_delay() == 20  # nor can the configured period go under the 25 s routine floor


def test_operator_api_and_session_telemetry_use_the_same_versioned_periods(settings):
    import json
    from fastapi.testclient import TestClient
    from common.protocol import Hello, dumps
    from server.app import create_app
    from server.planner import MockPlanner
    settings = replace(settings, decider="onejev")
    store = PlaybookStore(settings.data_dir)
    book, _ = edit(store.load(), "v002", "requested timing", sets=[
        "timing.plants_seconds=4", "timing.planner_seconds=12"])
    store.write(book)
    store.set_current(book.version)
    with TestClient(create_app(settings, MockPlanner(0))) as client:
        body = client.get("/playbook", headers={"Authorization": "Bearer secret"}).json()
        assert "jev" not in body["playbook"] and "decider" not in body["playbook"]
        assert body["effective_timing"]["plants_seconds"] == 4
        assert body["effective_timing"]["planner_seconds"] == 12
        with client.websocket_connect("/ws") as ws:
            ws.send_text(dumps(Hello(token="secret", client_id="timing", telemetry=True)))
            session_id = json.loads(ws.receive_text())["session_id"]
            updates = [json.loads(ws.receive_text()) for _ in range(5)]
            periods = {m["stage"]: m["interval_s"] for m in updates}
            assert periods["plants"] == 4 and periods["plan"] == 12
        hello = json.loads((settings.runs_dir / session_id / "events.jsonl").read_text().splitlines()[0])
        assert hello["effective_timing"] == body["effective_timing"]
