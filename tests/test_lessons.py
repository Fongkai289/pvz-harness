"""Coach lessons reach Planner and Jev only while their condition holds on the current lawn."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from rsi.postgame import EpisodeRecorder, analyze_live, lesson_stats
from rules.lessons import evaluate, parse_condition
from rules.playbook_rules import LessonBook, Playbook
from server.action_selector import DecisionContext, OneJevSelector, Selection
from server.lessons import active_lessons
from server.planner import Plan, PlanResult
from tests.test_jev import scene
from tests.test_server import settings
from tests.test_system0_actions import live, session

VAULTER = {"id": "vaulter_wall", "when": "has_zombie('pole vaulting') and card_ready('wall-nut')",
           "then": "A pole vaulter jumps the first plant in its row: put a wall-nut in front of it."}
SAVE = {"id": "save_for_bomb", "when": "zombies >= 3 and sun < 150", "then": "Keep 150 sun for a cherry bomb."}
ROW2 = {"id": "row2_close", "when": "nearest_col(2) <= 4 and attackers_in_row(2) == 0",
        "then": "Row 2 has no shooter and a zombie is close."}


# ---- condition language ----

@pytest.mark.parametrize("bad", [
    "__import__('os')", "sun.real > 0", "[1][0]", "lambda: 1", "open('x')", "has_zombie(type='normal')",
    "has_zombie('a', 'b')", "unknown_fact > 1", "sun if sun else 0", "x := 1", "1 in [1]", "", "1 " * 200])
def test_conditions_outside_the_fixed_language_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_condition(bad)


def test_unknown_values_never_make_a_lesson_hold():
    facts = {"sun": None, "zombies": 2, "rows": 5}
    functions = {"nearest_col": lambda row: None}
    assert not evaluate("sun < 100", facts, functions)
    assert not evaluate("nearest_col(1) <= 3", facts, functions)
    assert evaluate("not sun >= 100 and 0 < zombies * 2 <= 4", facts, functions)
    assert evaluate("sun == None", facts, functions)
    assert not evaluate("sun + 50 > 0", facts, functions)


def test_playbook_validates_lessons_and_old_versions_have_none():
    assert LessonBook(items=[VAULTER, SAVE]).max_shown == 4
    for bad in ({"items": [VAULTER, VAULTER]}, {"items": [{**VAULTER, "when": "sun.real"}]},
                {"items": [{**VAULTER, "id": "Bad id"}]}, {"items": [VAULTER] * 31}, {"max_shown": 9}):
        with pytest.raises(ValidationError):
            LessonBook.model_validate(bad)
    old = Playbook.model_validate({"version": "v001", "perception": {"sun_hsv_lo": [15, 80, 150],
        "sun_hsv_hi": [40, 255, 255], "sun_min_area": 300, "sun_max_area": 6000},
        "fast": {"sun_click_cooldown": 1.0, "sun_same_spot_px": 30}})
    assert old.lessons.items == []


# ---- which lessons show ----

def test_only_matching_lessons_show_in_order_up_to_the_cap():
    book = LessonBook(items=[ROW2, VAULTER, SAVE], max_shown=2)
    lawn = scene({2: 3, 0: 8, 4: 8}, types={0: "pole vaulting"})
    shown, errors = active_lessons(book, lawn, {}, 100)
    assert [l["id"] for l in shown] == ["row2_close", "vaulter_wall"] and errors == {}  # save_for_bomb capped
    shown, _ = active_lessons(book, lawn, {(2, 0): ["peashooter"]}, None)  # shooter in row 2, sun unknown
    assert [l["id"] for l in shown] == ["vaulter_wall"]


def test_a_failing_condition_is_skipped_and_reported():
    broken = {"id": "broken", "when": "sun < 'many'", "then": "never shown"}
    shown, errors = active_lessons(LessonBook(items=[broken, SAVE]), scene({0: 5, 1: 5, 2: 5}), {}, 100)
    assert [l["id"] for l in shown] == ["save_for_bomb"] and list(errors) == ["broken"]


def test_jev_sees_lessons_only_when_some_hold():
    ctx = DecisionContext(scene({2: 3}), {}, set(), Plan(objective="hold"))
    selector = OneJevSelector("http://jev.invalid", "test", 1)
    assert "lessons_now" not in json.loads(selector.state(ctx))
    ctx.lessons = [{"id": "row2_close", "then": ROW2["then"]}]
    state = json.loads(selector.state(ctx))
    assert state["lessons_now"] == ctx.lessons and "never a command" in state["lessons_contract"]


# ---- live and research ----

def test_live_planner_and_jev_get_matching_lessons_and_errors_are_logged_once(live):
    sess = live()
    seen = {"id": "row2_seen", "when": "zombies_in_row(2) >= 1 and nearest_col(2) == 6", "then": "Row 2 is under attack."}
    sess.playbook = sess.playbook.model_copy(update={"lessons": LessonBook(items=[
        ROW2, seen, {"id": "broken", "when": "sun < 'many'", "then": "x"}])})
    sess.episode_active = True
    asyncio.run(sess._jev_step(sess.latest_obs))
    asyncio.run(sess._jev_step(sess.latest_obs))
    jev = [d for k, d in sess.events if k == "jev"]
    assert jev[0]["lessons"] == ["row2_seen"]  # the fixture zombie is at column 6: row2_close does not hold
    assert [d["lesson"] for k, d in sess.events if k == "lesson_error"] == ["broken"]
    asyncio.run(sess._plan(sess.latest_obs, b"jpeg"))
    context = sess._planning_context["context"]
    assert [l["id"] for l in context["lessons_now"]] == ["row2_seen"] and "lessons_contract" in context


def test_bridge_passes_lessons_to_planner_and_jev_and_reports_them(settings, monkeypatch):
    from rsi.native_bridge import Bridge
    from rsi.native_jobs import digest
    from tests.test_native_throughput import public
    async def run():
        bridge = Bridge(settings)
        try:
            book = bridge.jobs.store.load()
            always = {"id": "always", "when": "rows >= 1", "then": "Hold the lawn."}
            updated = book.model_copy(update={"version": "v002", "parent": book.version,
                                              "lessons": LessonBook(items=[always])})
            bridge.jobs.store.write(updated)
            bridge.jobs.store.set_current(updated.version)
            job = bridge.jobs.enqueue("lessons-research")
            task = bridge.jobs.request(job)
            contexts, jev = [], []
            async def plan(*args, **kwargs):
                contexts.append(kwargs["context"])
                return PlanResult(plan=Plan(objective="fixture", valid_for_seconds=60), latency=0)
            async def select(ctx, policy, mode=None):
                jev.append(ctx.lessons)
                return Selection(reason="fixture wait")
            monkeypatch.setattr(bridge.planner, "decide", plan)
            monkeypatch.setattr(bridge.selector, "select", select)
            result = await bridge.decide(job, dict(request_digest=digest(task), episode_id="e1", tick=0, **public()))
            assert contexts[0]["lessons_now"] == [{"id": "always", "then": "Hold the lawn."}]
            assert jev == [[{"id": "always", "then": "Hold the lawn."}]]
            assert result["lessons"] == {"planner": ["always"], "jev": ["always"], "errors": {}}
        finally:
            await bridge.close()
    asyncio.run(run())


# ---- post-game ----

def test_lesson_table_counts_showings_and_mowers_soon_after():
    recorder = EpisodeRecorder()
    recorder.plan(0, {"trigger": "initial", "reasons": []}, ["save_for_bomb"])
    recorder.decision(100, {"lessons": {"jev": ["save_for_bomb", "row2_close"]}}, None)
    recorder.decision(5000, {"lessons": {"jev": ["row2_close"]}}, None)
    stats = lesson_stats(recorder.plans, recorder.decisions, [{"t": 900, "row": 2}, {"t": 9000, "row": 1}])
    assert stats == {"row2_close": {"planner": 0, "jev": 2, "first_t": 100, "mowers_within_10s": 1},
                     "save_for_bomb": {"planner": 1, "jev": 1, "first_t": 0, "mowers_within_10s": 1}}
    events = [{"t_server": 0, "kind": "plan", "plan": {"objective": "a"}, "trigger": "initial",
               "observed_context": {"context": {"lessons_now": [{"id": "row2_close", "then": "x"}]}}},
              {"t_server": 1, "kind": "jev", "lessons": ["row2_close"], "decision_context": {}}]
    assert analyze_live(events)["lessons"]["row2_close"] == {"planner": 1, "jev": 1, "first_t": 0,
                                                             "mowers_within_10s": 0}
