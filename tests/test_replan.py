"""Replanning: the plan states replan_when, System0 checks it, fixed bounds cap the Planner rate."""
import asyncio

import pytest

from rsi.code_runtime import CandidateProgram
from rsi.native_player import SEED, run_entry
from rsi.postgame import EpisodeRecorder, analyze_live, find_moments, planner_stats
from rules.replan_rules import (MIN_PERIODIC_REFRESH_S, MIN_PLAN_LIFETIME_S, MIN_PLAN_SPACING_S, event_spacing,
                                plan_lifetime, refresh_seconds)
from server.action_selector import Selection
from server.planner import Plan, PlanResult, ReplanWhen
from tests.test_server import settings
from tests.test_system0_actions import live, MenuSelector, session


def test_fixed_bounds_cannot_be_shortened_by_plan_or_playbook():
    assert plan_lifetime(6) == MIN_PLAN_LIFETIME_S and plan_lifetime(90) == 90
    assert refresh_seconds(4, 6) == refresh_seconds(20, 60) == MIN_PERIODIC_REFRESH_S  # v013 asks for 6 s: 25 s
    assert refresh_seconds(30, 6) == 30 and refresh_seconds(60, 30) == MIN_PLAN_LIFETIME_S
    assert MIN_PERIODIC_REFRESH_S + 15 <= MIN_PLAN_LIFETIME_S  # room for a slow answer before the plan lapses
    assert event_spacing(2) == MIN_PLAN_SPACING_S and event_spacing(12) == 12


def test_replan_when_is_bounded_structured_data():
    assert Plan(objective="x").replan_when == ReplanWhen()
    for bad in ({"covered_rows": [6]}, {"key_plants": [{"row": 0, "col": 9, "plant": "peashooter"}]},
                {"max_failed_steps": 1}, {"goal_plants": [{"plant": "sunflower", "at_least": 0}]}, {"extra": 1}):
        with pytest.raises(ValueError):
            Plan(objective="x", replan_when=bad)


# ---- seed decide() ----

def decide(observation, memory):
    return run_entry(CandidateProgram(SEED), "decide", observation, memory)


def observe(zombies=(), plants=(), plan=None, plan_id=1, failed=0):
    return {"scene": {"rows": 5, "cards": [], "nearest_col": {},
                      "zombies": [{"type": t, "row": r, "col": 7} for r, t in zombies]},
            "plants": [{"row": r, "col": c, "plants": [p]} for r, c, p in plants],
            "guidance": (plan or Plan(objective="hold")).model_dump(),
            "plan_status": {"id": plan_id, "age_s": 1.0, "failed_steps": failed}}


def reasons(result):
    assert result["request_replan"] is bool(result["planner_context"].get("replan_reasons"))
    return result["planner_context"].get("replan_reasons", [])


def test_seed_flags_new_rows_and_types_once_confirmed_and_once_per_plan():
    plan = Plan(objective="hold", priority_rows=[2], replan_when={"covered_types": ["normal"]})
    memory = {}
    assert reasons(decide(observe([(1, "normal")], plan=plan), memory)) == []  # on the lawn when the plan came
    assert reasons(decide(observe([(1, "normal"), (2, "normal")], plan=plan), memory)) == []
    seen = observe([(1, "normal"), (3, "normal"), (2, "buckethead")], plan=plan)
    assert reasons(decide(seen, memory)) == []  # one sighting may be a misread
    found = reasons(decide(seen, memory))
    assert any("uncovered row 3" in r for r in found) and any("unexpected buckethead" in r for r in found)
    assert reasons(decide(seen, memory)) == []  # one request per plan
    fresh = observe([(1, "normal"), (3, "normal"), (2, "buckethead")], plan=plan, plan_id=2)
    assert reasons(decide(fresh, memory)) == [] and reasons(decide(fresh, memory)) == []


def test_seed_counts_lost_defenses_but_not_used_up_single_use_plants():
    memory = {}
    lawn = [(2, 1, "peashooter"), (3, 4, "cherry bomb")]
    plan = Plan(objective="hold", replan_when={"key_plants": [{"row": 2, "col": 1, "plant": "peashooter"},
                                                              {"row": 3, "col": 4, "plant": "cherry bomb"}]})
    assert reasons(decide(observe(plants=lawn, plan=plan), memory)) == []
    assert reasons(decide(observe(plants=lawn[:1], plan=plan), memory)) == []  # cherry bomb went off
    assert reasons(decide(observe(plan=plan), memory)) == []
    assert reasons(decide(observe(plan=plan), memory)) == ["defense lost: 2:1 peashooter"]
    # Without key_plants the seed watches the attackers and walls present when the plan arrived.
    memory = {}
    decide(observe(plants=[(0, 0, "sunflower"), (1, 2, "wall-nut")]), memory)
    assert memory["replan"]["keys"] == [["1:2", "wall-nut"]]


def test_seed_reports_goal_reached_target_gone_and_unworkable_plan():
    goal = Plan(objective="eco", replan_when={"goal_plants": [{"plant": "sunflower", "at_least": 2}]})
    memory = {}
    two = [(0, 0, "sunflower"), (1, 0, "sunflower")]
    decide(observe(plants=two[:1], plan=goal), memory)
    decide(observe(plants=two, plan=goal), memory)
    assert reasons(decide(observe(plants=two, plan=goal), memory)) == ["goal reached: planned plant counts are on the lawn"]
    clear = Plan(objective="kill", replan_when={"goal_clear_rows": [2]})
    memory = {}
    decide(observe([(2, "normal")], plan=clear), memory)
    decide(observe(plan=clear), memory)
    assert reasons(decide(observe(plan=clear), memory)) == ["target gone: rows [2] are clear"]
    stuck = Plan(objective="x", replan_when={"max_failed_steps": 2})
    memory = {}
    assert reasons(decide(observe(plan=stuck, failed=1), memory)) == []
    decide(observe(plan=stuck, failed=2), memory)
    assert "plan unworkable" in reasons(decide(observe(plan=stuck, failed=3), memory))[0]


# ---- live session ----

REQUESTING = '''def perceive(image):
    return image

def decide(observation, memory):
    status = observation.get("plan_status")
    return {"planner_context": {"status": status, "replan_reasons": ["new threat: row 3"]},
            "jev_context": {}, "request_replan": status is not None}

def options(observation, memory):
    return [{"id": "hold", "text": "Hold the line in row 2", "tags": ["defense"], "rows": [2]}]

def act(observation, memory, decision):
    return []
'''


def test_live_replans_only_on_system0_request_and_counts_unexecutable_steps(live):
    sess = live(REQUESTING)
    sess.episode_active = True
    sess._plan_requested.clear()
    sess._plan_reasons.clear()
    asyncio.run(sess._jev_step(sess.latest_obs))
    assert sess._plan_requested.is_set() and list(sess._plan_reasons.values()) == ["new threat: row 3"]
    assert sess._plan_failures == 1
    asyncio.run(sess._jev_step(sess.latest_obs))
    status = [d["result"]["planner_context"]["status"] for k, d in sess.events if k == "system0" and d["entry"] == "decide"]
    assert status[-1]["failed_steps"] == 1 and status[-1]["id"] == sess._plan_id
    sess._plan_requested.clear()
    sess.playbook.timing.danger_replan = False
    asyncio.run(sess._jev_step(sess.latest_obs))
    assert not sess._plan_requested.is_set()


# ---- research bridge ----

def test_bridge_honours_system0_request_after_fixed_spacing(settings, monkeypatch):
    from rsi.native_bridge import Bridge
    from rsi.native_jobs import digest
    from tests.test_native_throughput import public
    async def run():
        bridge = Bridge(settings)
        try:
            version = bridge.jobs.code.add(REQUESTING)
            book = bridge.jobs.store.load()
            updated = book.model_copy(update={"version": "v002", "parent": book.version, "player_code": version})
            bridge.jobs.store.write(updated)
            bridge.jobs.store.set_current(updated.version)
            job = bridge.jobs.enqueue("replan-research")
            task = bridge.jobs.request(job)
            contexts = []
            async def plan(*args, **kwargs):
                contexts.append(kwargs["context"])
                return PlanResult(plan=Plan(objective="fixture", valid_for_seconds=60), latency=0)
            async def select_menu(ctx, menu, preferred=()):
                return Selection(decision=menu[0], latency=0.0)
            monkeypatch.setattr(bridge.planner, "decide", plan)
            monkeypatch.setattr(bridge.selector, "select_menu", select_menu)
            base = dict(request_digest=digest(task), episode_id="e1", **public())
            first = await bridge.decide(job, dict(base, tick=0))
            assert first["replan"]["trigger"] == "initial" and first["decision"]["decision_unexecutable"]
            state = {k: first[k] for k in ("plan", "plan_tick", "spent")}
            early = await bridge.decide(job, dict(base, tick=400, **state))
            assert early["replan"] is None and len(contexts) == 1  # requested, but inside the 8 s spacing
            ready = await bridge.decide(job, dict(base, tick=800, **state))
            assert ready["replan"] == {"trigger": "system0", "reasons": ["new threat: row 3"]}
            assert contexts[1]["replan_reasons"] == ["new threat: row 3"]
            assert contexts[1]["system0_derived_context"]["status"] == {"id": 0, "age_s": 8.0, "failed_steps": 2,
                                                                        "fallback_reasons": []}
        finally:
            await bridge.close()
    asyncio.run(run())


# ---- post-game ----

def test_planner_calls_are_counted_per_trigger_in_research_and_live_tables():
    recorder = EpisodeRecorder()
    recorder.timeline = [{"t": 0}, {"t": 12000}]
    recorder.plan(0, {"trigger": "initial", "reasons": []})
    recorder.plan(2000, {"trigger": "periodic", "reasons": []})
    recorder.plan(2900, {"trigger": "system0", "reasons": ["new threat: zombie in uncovered row 3"]})
    stats = planner_stats(recorder.plans, 12000)
    assert stats == {"calls": 3, "by_trigger": {"initial": 1, "periodic": 1, "system0": 1}, "per_minute": 1.5,
                     "reasons": {"new threat": 1}}
    report = {"episodes": [{"postgame": {"plans": recorder.plans}}]}
    assert [m["trigger"] for m in find_moments(report, "replan")] == ["initial", "periodic", "system0"]
    events = [{"t_server": 0, "kind": "plan", "plan": {"objective": "a"}, "trigger": "initial", "trigger_reasons": []},
              {"t_server": 30, "kind": "plan", "plan": None, "trigger": "periodic"},  # failed call: no plan
              {"t_server": 60, "kind": "plan", "plan": {"objective": "b"}, "trigger": "system0",
               "trigger_reasons": ["defense lost: 2:1 peashooter"]}]
    live_stats = analyze_live(events)["planner_calls"]
    assert live_stats["calls"] == 2 and live_stats["per_minute"] == 2.0 and live_stats["reasons"] == {"defense lost": 1}


def test_a_goal_already_met_when_the_plan_arrives_never_requests_a_replan():
    """Qwen often restates what is on the lawn as a goal; that used to replan every 8 s."""
    met = Plan(objective="eco", replan_when={"goal_plants": [{"plant": "sunflower", "at_least": 1}]})
    memory = {}
    lawn = [(0, 0, "sunflower")]
    for _ in range(5):
        assert reasons(decide(observe(plants=lawn, plan=met), memory)) == []
    # Lost and planted again under the same plan: now the goal was open, so reaching it counts.
    decide(observe(plants=[], plan=met), memory)
    decide(observe(plants=lawn, plan=met), memory)
    assert reasons(decide(observe(plants=lawn, plan=met), memory)) == ["goal reached: planned plant counts are on the lawn"]
