"""Planner never supplies a placement; Jev's actual choice reaches the executor."""
import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError

from rules.playbook_rules import JevPolicy
from server.action_selector import DecisionContext, OneJevSelector, Selection, candidates
from server.perception import Observation
from server.planner import MockPlanner, Placement, Plan, QwenPlanner
from server.playbook import PlaybookStore
from server.session import Session
from tests.test_jev import scene
from tests.test_server import settings


def context(**guidance):
    return DecisionContext(scene({2: 6}), {}, set(), Plan(objective="Balance development and defense", **guidance))


@pytest.mark.parametrize("field,value", [("placements", []), ("slot", 1), ("col", 2),
                                         ("tool", "click"), ("prompt", "change Jev")])
def test_planner_schema_rejects_placement_and_interface_fields(field, value):
    with pytest.raises(ValidationError):
        Plan(objective="defend", **{field: value})


@pytest.mark.parametrize("fields", [{"priority_rows": [1, 1]}, {"blocked_rows": [-1]},
                                     {"priority_rows": [6]}, {"sun_reserve": -1},
                                     {"spend_limit": True}, {"valid_for_seconds": 0},
                                     {"priority_rows": [2], "blocked_rows": [2]}])
def test_planner_constraints_are_validated(fields):
    with pytest.raises(ValidationError):
        Plan(objective="defend", **fields)


def test_candidates_include_all_available_columns_and_plant_types():
    ctx = context()
    offered = candidates(ctx)
    assert {p.col for p in offered if p.slot == 1 and p.row == 3} == set(range(9))
    assert {p.slot for p in offered} == set(range(6))
    assert all(ctx.reject(p) is None for p in offered)


def test_constraints_are_enforced_before_model_call():
    ctx = context(blocked_rows=[3], sun_reserve=100, spend_limit=100)
    ctx.sun_spent = 150  # 150 available; leave 100, so only <=50-sun cards
    ctx.directive_spent = 50
    ctx.used_slots = {3}
    ctx.planted = {(1, 0): ["sunflower"]}
    offered = candidates(ctx)
    assert offered
    assert all(p.slot in (1, 4) and p.row != 3 and (p.row, p.col) != (1, 0) for p in offered)
    ctx.directive_spent = 100
    assert candidates(ctx) == []


def test_unknown_or_inconsistent_observations_do_not_create_candidates():
    ctx = context()
    ctx.scene.sun = None
    assert not candidates(ctx)
    ctx.scene.sun = 300
    ctx.scene.cards[0].slot = 7
    assert not candidates(ctx)


def scorer(group="slot1_row3", column="col7", seen=None):
    def handler(request):
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        keys = body["questions"]
        winner = column if any(k.startswith("col") for k in keys) else group
        return httpx.Response(200, json={"answers": {
            k: {"noul": 0.95 if k == winner else 0.05} for k in keys}})
    return handler


def selector(handler, timeout=1):
    return OneJevSelector("http://jev.invalid/v1/systemone", "test-jev", timeout,
                          httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_jev_selects_plant_row_and_column_not_a_rule_chosen_location():
    seen = []
    ctx = context(priority_rows=[3], sun_reserve=25, spend_limit=100)
    result = asyncio.run(selector(scorer(seen=seen)).select(ctx, JevPolicy()))
    assert result.placement == Placement(slot=1, row=3, col=7)  # outside legacy economy columns 0/1
    assert len(seen) == 2 and len(seen[0]["questions"]) <= 61 and len(seen[1]["questions"]) <= 10
    assert set(seen[1]["questions"]) == {"wait", *(f"col{i}" for i in range(9))}
    assert '"priority_rows": [3]' in seen[0]["state"]
    assert '"budget_remaining": 100' in seen[0]["state"]
    for request in seen:
        assert set(request) == {"model", "state", "questions"}  # never images/media
        assert all(q["type"] == "noul" for q in request["questions"].values())
    assert set(result.scores) == {"plant_row", "column"}


@pytest.mark.parametrize("stage", ["group", "column"])
def test_jev_can_choose_wait_at_either_stage(stage):
    seen = []
    result = asyncio.run(selector(scorer(group="wait" if stage == "group" else "slot1_row3",
                                        column="wait", seen=seen)).select(context(), JevPolicy()))
    assert result.placement is None and result.error is None
    assert len(seen) == (1 if stage == "group" else 2)


@pytest.mark.parametrize("bad", ["missing", "extra", "nan", "range", "bool", "http", "json"])
def test_bad_model_answer_waits_without_rules_fallback(bad):
    def handler(request):
        keys = json.loads(request.content)["questions"]
        answers = {k: {"noul": 0.2} for k in keys}
        if bad == "missing":
            answers.pop("wait")
        elif bad == "extra":
            answers["invented"] = {"noul": 1}
        elif bad in {"nan", "range", "bool"}:
            answers["wait"]["noul"] = {"nan": "nan", "range": 1.1, "bool": True}[bad]
        elif bad == "http":
            return httpx.Response(503)
        elif bad == "json":
            return httpx.Response(200, text="bad json")
        return httpx.Response(200, json={"answers": answers})
    result = asyncio.run(selector(handler).select(context(), JevPolicy()))
    assert result.source == "failed" and result.error and result.placement is None


def test_deadline_covers_both_jev_calls():
    calls = []
    async def slow(request):
        calls.append(request)
        await asyncio.sleep(0.06)
        return scorer()(request)
    result = asyncio.run(selector(slow, timeout=0.1).select(context(), JevPolicy()))
    assert len(calls) == 2 and result.source == "failed" and result.placement is None


def test_no_legal_actions_does_not_call_shared_model():
    ctx = context(spend_limit=0)
    result = asyncio.run(selector(lambda _: pytest.fail("no model call expected")).select(ctx, JevPolicy()))
    assert result.placement is None


@pytest.fixture
def session(settings):
    class Socket:
        def __init__(self):
            self.sent = []
        async def send_text(self, text):
            self.sent.append(json.loads(text))
    store = PlaybookStore(settings.data_dir)
    sess = Session(Socket(), replace(settings, loadout=[c.plant for c in scene({}).cards]),
                   MockPlanner(0), selector(scorer()), store.load(), "role-test")
    sess.latest_scene = scene({2: 6})
    sess.latest_obs = Observation(frame_id=1, t_capture=0)
    sess.latest_recv = sess.scene_received = sess.directive_received = time.monotonic()
    sess.directive = Plan(objective="Observe and develop", priority_rows=[3], spend_limit=50)
    yield sess
    sess.close()


def test_actual_model_choice_reaches_clicks_and_consumes_guidance_budget(session):
    from server import geometry
    async def run():
        place = await session._jev_step(session.latest_obs)
        assert place == Placement(slot=1, row=3, col=7)
        await session._execute_placement(place)
        assert session.directive_spent == 50 and session.sun_spent == 50
        assert await session._jev_step(session.latest_obs) is None
    asyncio.run(run())
    assert [(c["x"], c["y"]) for c in session.ws.sent] == [geometry.seed_slot_center(1), geometry.cell_center(3, 7)]
    assert (1, 3, 7, "sunflower") in session.placed_since


@pytest.mark.parametrize("change", ["pause", "frame_expiry", "guidance_expiry", "scene_expiry", "new_guidance",
                                    "new_scene", "used_card", "occupied", "spent"])
def test_inflight_decision_is_rechecked_before_clicking(session, change):
    class DelayedSelector:
        async def select(self, ctx, policy):
            if change == "pause": session.paused = True
            if change == "frame_expiry": session.latest_recv -= 20
            if change == "guidance_expiry": session.directive_received -= 200
            if change == "scene_expiry": session.scene_received -= 200
            if change == "new_guidance": session.directive = Plan(objective="wait", spend_limit=0)
            if change == "new_scene": session.latest_scene = session.latest_scene.model_copy()
            if change == "used_card": session.used_slots.add(1)
            if change == "occupied": session.seen_plants[(3, 7)] = ["peashooter"]
            if change == "spent": session.directive_spent = 50
            return Selection(placement=Placement(slot=1, row=3, col=7))
    session.selector = DelayedSelector()
    assert asyncio.run(session._jev_step(session.latest_obs)) is None
    assert session.ws.sent == []


def test_executor_checks_again_after_selection(session):
    async def run():
        place = await session._jev_step(session.latest_obs)
        session.directive = Plan(objective="wait", spend_limit=0)
        await session._execute_placement(place)
    asyncio.run(run())
    assert session.ws.sent == [] and session.directive_spent == 0


def test_new_look_preserves_postcapture_commands_and_cumulative_planner_budget(session):
    session.placed_since = [(1, 0, 0, "sunflower"), (3, 3, 7, "peashooter")]
    session.directive_spent = 150
    session._accept_scene(scene({}).model_copy(update={"frame_id": 2}), time.monotonic())
    assert session.used_slots == {0} and session.sun_spent == 100
    assert session.directive_spent == 150  # a new look is not a new spending allowance


def test_slow_inference_does_not_refresh_old_scene(session):
    session._accept_scene(scene({}), time.monotonic() - 60)
    assert session._decision_context() is None


def test_episode_boundaries_discard_guidance_and_old_frames(session):
    from common.protocol import EpisodeEvent, dumps
    async def run():
        await session.on_text(dumps(EpisodeEvent(event="end", level="test")))
        assert not session.episode_active and session.directive is None
        await session.on_text(dumps(EpisodeEvent(event="start", level="next")))
        assert session.episode_active and session.latest_obs is None and session.latest_scene is None
    asyncio.run(run())


def test_no_guidance_or_scene_never_uses_planner_as_action_queue(session):
    session.directive = None
    assert asyncio.run(session._jev_step(session.latest_obs)) is None
    session.directive = Plan(objective="defend")
    session.latest_scene = None
    assert asyncio.run(session._jev_step(session.latest_obs)) is None
    assert not hasattr(session.policy, "set_plan")


def test_old_placement_response_is_rejected_by_planner_transport():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "placements": [{"slot": 0, "row": 1, "col": 2}], "note": "old contract"})}}]})
    planner = QwenPlanner("http://qwen.invalid", "test", False, True, 1,
                          httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = asyncio.run(planner.decide(scene({}), ""))
    assert result.plan is None and result.error
