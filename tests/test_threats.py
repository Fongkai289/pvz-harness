import asyncio
from dataclasses import replace
from types import SimpleNamespace

from server.scene import Zombie
from server.threats import observed_threats, threat_changes
from server.planner import Plan, PlanResult
from tests.test_action_selector import session
from tests.test_server import settings


def test_bottom_row_detected_without_omniscient_state(session):
    scene = session.latest_scene.model_copy(update={"zombies": [Zombie(type="normal", row=4)], "nearest_col": {4: 8}})
    rows = observed_threats(scene, {}, True)
    assert "第5行" in threat_changes([], rows)["4"]
    assert not threat_changes(rows, rows)  # unchanged observations never keep retriggering
    scene.nearest_col = {4: 3}
    near = observed_threats(scene, {}, True)
    assert "向房屋移动" in threat_changes(rows, near)["4"]
    assert "level" not in near[4]


def test_missing_layout_is_unknown_not_an_empty_defense(session):
    rows = observed_threats(session.latest_scene, {}, False)
    assert rows[2]["attackers"] is None
    assert "出现无攻击植物" not in str(threat_changes([], rows))  # unknown coverage, not zero
    full = observed_threats(session.latest_scene, {(2, 2): ["peashooter"]}, True)
    empty = observed_threats(session.latest_scene, {}, True)
    assert "防线消失" in threat_changes(full, empty)["2"]


def test_new_resources_request_new_guidance_without_blocking_actions(session):
    session.directive = Plan(objective="save", spend_limit=0)
    session._accept_scene(session.latest_scene, session.scene_received)
    session._plan_requested.clear()
    session._plan_reasons.clear()
    assert not session._plan_requested.is_set()
    newer = session.latest_scene.model_copy(update={"sun": 125, "frame_id": 2})
    session._accept_scene(newer, session.scene_received)
    assert session._plan_requested.is_set()
    assert "resources" in session._plan_reasons
    session._plan_requested.clear()
    session._plan_reasons.clear()
    session._accept_scene(newer, session.scene_received)
    assert not session._plan_requested.is_set()
    session.directive = Plan(objective="defend", spend_limit=100)
    # budget-checking never itself sends a click or rewrites a plan
    assert session.ws.sent == []


def test_pause_and_disabled_trigger_cannot_request_plans(session):
    session.paused = True
    session._request_replan({"4": "urgent"})
    assert not session._plan_requested.is_set()
    session.paused = False
    session.playbook.timing.danger_replan = False
    session._request_replan({"4": "urgent"})
    assert not session._plan_requested.is_set()


def test_budget_advice_is_data_not_a_replan_gating_rule(session):
    session.directive = Plan(objective="wall defense", spend_limit=50)
    ctx = session._decision_context()
    from server.action_selector import candidates
    assert any(ctx.scene.cards[p.slot].plant == "peashooter" for p in candidates(ctx))
    assert ctx.budget_message()["remaining_advice"] == 50
    assert not session._plan_requested.is_set()


def test_trigger_wakes_timer_and_inflight_changes_are_coalesced(session, monkeypatch):
    async def run():
        session.settings = replace(session.settings, plan_interval=.04, planner="mock")
        session._planner_started = None
        session.latest_jpeg = b"fixture"
        starts = []
        active = [0]
        second = asyncio.Event()
        async def plan(obs, jpeg):
            active[0] += 1
            assert active[0] == 1
            starts.append(asyncio.get_running_loop().time())
            if len(starts) == 1:
                session._request_replan({"4": "first change"})
                session._request_replan({"4": "latest change"})
            else:
                assert session._active_plan_reasons == ["latest change"]
                second.set()
            await asyncio.sleep(.005)
            active[0] -= 1
            return PlanResult(plan=Plan(objective="replan"), latency=.005)
        monkeypatch.setattr(session, "_plan", plan)
        monkeypatch.setattr(session, "_fresh_obs", lambda: session.latest_obs)
        # Prove the event wakes a long periodic timer, then enforces minimum spacing.
        monkeypatch.setattr(session, "_plan_delay", lambda: 20)
        session._request_replan({"4": "new uncovered zombie"})
        task = asyncio.create_task(session.plan_loop())
        try:
            await asyncio.wait_for(second.wait(), timeout=1)
            assert starts[1] - starts[0] >= .035
            assert session.ws.sent == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
