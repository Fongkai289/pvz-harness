"""System0 operation authority: decision menu, act(), reflexes, the gate, post-game tables."""
import asyncio
import json
import time
from dataclasses import replace

import pytest

from rsi.code_runtime import CandidateProgram, CandidateValidationError
from rsi.native_loop import run_episode
from rsi.native_player import NativeCodeStore, SEED
from rsi.native_runtime import player_state
from rsi.postgame import EpisodeRecorder, alignment, analyze, analyze_live, encode_state, signature
from rules.playbook_rules import DecisionParams, Playbook, ReflexParams
from server.action_gate import MenuItem, PlantAction, SunAction, parse_actions, parse_menu
from server.action_selector import Selection
from server.planner import Placement
from tests.test_action_selector import session
from tests.test_native_rsi import raw
from tests.test_native_throughput import Arena, RPC
from tests.test_server import settings
from tests.test_sun_collection import confirmed


# ---- gate ----

def test_menu_validation_rejects_bad_ids_tags_rows_and_size():
    good = [{"id": "defend_row_2", "text": "Row 2: add a peashooter", "tags": ["defense"], "rows": [2],
             "params": {"row": 2}}]
    menu = parse_menu(good, 5)
    assert menu[0].public() == {"id": "defend_row_2", "text": "Row 2: add a peashooter", "tags": ["defense"], "rows": [2]}
    assert menu[0].call_argument()["params"] == {"row": 2}
    for bad in ([{"id": "wait", "text": "x"}], [{"id": "Bad", "text": "x"}], [{"id": "a", "text": ""}],
                [{"id": "a", "text": "x", "tags": ["attack"]}], [{"id": "a", "text": "x", "rows": [5]}],
                [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}], [{"id": "a", "text": "x", "slot": 1}],
                [{"id": f"d{i}", "text": "x"} for i in range(25)], {"id": "a"}):
        with pytest.raises(ValueError):
            parse_menu(bad, 5)


def test_action_limits_types_and_reflex_planting_switch():
    assert parse_actions([{"type": "plant", "slot": 1, "row": 2, "col": 3}], "act", suns=0) == [PlantAction(1, 2, 3)]
    assert parse_actions([{"type": "collect_sun", "sun": 1}, {"type": "collect_sun", "sun": 0}], "reflex",
                         suns=2) == [SunAction(1), SunAction(0)]
    plant = {"type": "plant", "slot": 0, "row": 0, "col": 0}
    for raw_actions, entry, kwargs in (
            ([{"type": "collect_sun", "sun": 0}], "act", {"suns": 1}),          # act cannot pick up suns
            ([plant, plant], "act", {"suns": 0}),                              # one plant per decision
            ([plant], "reflex", {"suns": 0}),                                  # reflex planting off by default
            ([{"type": "collect_sun", "sun": 2}], "reflex", {"suns": 2}),      # only confirmed targets
            ([{"type": "collect_sun", "sun": 0}] * 2, "reflex", {"suns": 2}),  # distinct targets
            ([{"type": "click", "x": 1, "y": 2}], "reflex", {"suns": 0}),       # no coordinates
            ([{"type": "plant", "slot": 0, "row": 0, "col": 9}], "act", {"suns": 0})):
        with pytest.raises(ValueError):
            parse_actions(raw_actions, entry, **kwargs)
    assert parse_actions([plant], "reflex", suns=0, allow_planting=True) == [PlantAction(0, 0, 0)]


def test_optional_entries_have_fixed_arity_and_seed_defines_all():
    assert CandidateProgram(SEED).entries == {"perceive", "decide", "options", "act", "reflex", "fallback"}
    with pytest.raises(CandidateValidationError):
        CandidateProgram(SEED + "\ndef extra_entry_check(a):\n    return a\n".replace("extra_entry_check", "act"))


def test_playbook_sections_have_safe_defaults_and_ranges():
    book = Playbook.model_validate({"version": "v001", "perception": {"sun_hsv_lo": [15, 80, 150],
        "sun_hsv_hi": [40, 255, 255], "sun_min_area": 300, "sun_max_area": 6000},
        "fast": {"sun_click_cooldown": 1.0, "sun_same_spot_px": 30}})
    assert book.decision.jev_mode == "auto" and book.reflex.allow_planting is False
    assert book.fallback.allow_planting is False  # versions written before the section never plant from fallback()
    with pytest.raises(ValueError):
        ReflexParams(min_plant_interval_s=1)
    with pytest.raises(ValueError):
        DecisionParams(jev_mode="rules")


# ---- live session ----

@pytest.fixture
def live(session):
    from server.session import Session
    made = []
    def make(source=SEED, **book_updates):
        code = NativeCodeStore(session.settings.data_dir).add(source)
        book = session.playbook.model_copy(update={"player_code": code, **book_updates})
        sess = Session(session.ws, replace(session.settings, decider="onejev", planner_mode="split"), session.planner,
                       MenuSelector(), book, f"system0-{len(made)}")
        for name in ("latest_scene", "latest_obs", "directive", "latest_recv", "scene_received", "directive_received"):
            setattr(sess, name, getattr(session, name))
        sess.looker = object()
        sess.seen_plants_frame, sess.plants_received = 1, time.monotonic()
        sess.events = []
        made.append(sess)
        asyncio.run(sess._system0())  # cold worker start-up must not age the fixture's frames
        sess.latest_recv = sess.scene_received = sess.directive_received = sess.plants_received = time.monotonic()
        sess.runlog.event = lambda kind, **data: sess.events.append((kind, data))
        return sess
    yield make
    for sess in made:
        sess.close()


class MenuSelector:
    def __init__(self, pick=lambda menu: next((m for m in menu if m.id.startswith("defend_row")), menu[0])):
        self.pick, self.menus = pick, []

    async def select_menu(self, ctx, menu, preferred=()):
        self.menus.append([m.id for m in menu])
        return Selection(decision=self.pick(menu), reason="fixture", latency=0.001)

    async def select(self, *args, **kwargs):
        pytest.fail("the built-in placement choice must not run in menu mode")


def test_jev_picks_a_named_decision_and_act_places_it_through_hard_rules(live):
    sess = live()
    async def run():
        place, shovel = await sess._jev_step(sess.latest_obs)
        assert shovel is None
        assert isinstance(place, Placement) and sess.latest_scene.cards[place.slot].plant in {"peashooter", "snow pea"}
        await sess._execute_placement(place)
    asyncio.run(run())
    jev = next(data for kind, data in sess.events if kind == "jev")
    assert jev["decision"]["id"].startswith("defend_row") and jev["act"] and not jev.get("rejected")
    assert {m["id"] for m in jev["menu"]} >= {"economy", "emergency_row_2"}
    assert len(sess.ws.sent) == 2 and sess.used_slots == {jev["act"][0]["slot"]}
    assert all(data["meta"]["decision_source"] == "onejev" for kind, data in sess.events if kind == "cmd")


def test_planner_sees_the_latest_menu_and_may_prefer_decisions(live):
    sess = live()
    seen = []
    async def plan(scene, knowledge, jpeg=None, context=None):
        seen.append(context)
        from server.planner import Plan, PlanResult
        return PlanResult(plan=Plan(objective="fixture", preferred_decisions=["economy"]), latency=0)
    sess.planner.decide = plan
    async def run():
        await sess._jev_step(sess.latest_obs)
        await sess._plan(sess.latest_obs, b"")
    asyncio.run(run())
    assert any(item["id"] == "economy" for item in seen[0]["decision_menu"])


CUSTOM = '''def perceive(image):
    return image

def decide(observation, memory):
    return {"planner_context": {}, "jev_context": {}, "request_replan": False}

def options(observation, memory):
    return [{"id": "hold", "text": "Hold the line in row 2", "tags": ["defense"], "rows": [2]}]

def act(observation, memory, decision):
    return ACTIONS
'''


@pytest.mark.parametrize("actions,reason", [
    ("[]", "decision_unexecutable"),
    ('[{"type": "plant", "slot": 9, "row": 2, "col": 3}]', "rejected"),
    ('[{"type": "collect_sun", "sun": 0}]', "act_error")])
def test_unexecutable_illegal_or_unauthorized_act_results_wait(live, actions, reason):
    sess = live(CUSTOM.replace("ACTIONS", actions))
    assert asyncio.run(sess._jev_step(sess.latest_obs)) is None
    jev = next(data for kind, data in sess.events if kind == "jev")
    assert jev.get(reason) and not sess.ws.sent
    assert jev["action"] == ("plant" if reason == "rejected" else "wait")  # a rejected plant is logged as proposed


def test_forced_builtin_mode_ignores_the_menu(live):
    sess = live(decision=DecisionParams(jev_mode="hierarchical"))
    calls = []
    async def select(ctx, policy):
        calls.append(ctx)
        return Selection(reason="Jev chose wait")
    sess.selector.select = select
    asyncio.run(sess._jev_step(sess.latest_obs))
    assert calls and not sess.selector.menus


def warm(sess):
    """Start the isolated worker first: a cold start can outlast a sun target's 0.8 s freshness."""
    asyncio.run(sess._system0())


def test_seed_reflex_collects_confirmed_suns_with_its_own_label(live):
    sess = live()
    warm(sess)
    confirmed(sess)
    asyncio.run(sess._reflex())
    assert [(c["x"], c["y"]) for c in sess.ws.sent] == [(305, 350)]
    assert next(d for k, d in sess.events if k == "cmd")["meta"]["decision_source"] == "system0_reflex"


REFLEX = '''def perceive(image):
    return image

def decide(observation, memory):
    return {"planner_context": {}, "jev_context": {}, "request_replan": False}

def reflex(observation, memory):
    BODY
'''


@pytest.mark.parametrize("body", ["return 1 // 0", 'return [{"type": "plant", "slot": 1, "row": 3, "col": 7}]'])
def test_reflex_errors_and_unpermitted_planting_fall_back_to_host_pickup(live, body):
    sess = live(REFLEX.replace("BODY", body))
    warm(sess)
    confirmed(sess)
    asyncio.run(sess._reflex())
    assert [(c["x"], c["y"]) for c in sess.ws.sent] == [(305, 350)]
    assert next(d for k, d in sess.events if k == "cmd")["meta"]["decision_source"] == "sun_collector"
    assert any(k == "system0_error" and d["entry"] == "reflex" for k, d in sess.events)


def test_enabled_reflex_planting_is_gated_rate_limited_and_recorded(live):
    sess = live(REFLEX.replace("BODY", 'return [{"type": "plant", "slot": 1, "row": 3, "col": 7}]'),
                reflex=ReflexParams(allow_planting=True, min_plant_interval_s=30))
    sess.latest_obs = type(sess.latest_obs)(frame_id=2, t_capture=0)
    asyncio.run(sess._reflex())
    assert len(sess.ws.sent) == 2 and sess.used_slots == {1} and sess.sun_spent > 0
    assert next(d for k, d in sess.events if k == "reflex_plant")["rejected"] is None
    sess.ws.sent.clear()
    sess.latest_obs = type(sess.latest_obs)(frame_id=3, t_capture=0)
    asyncio.run(sess._reflex())  # inside min_plant_interval_s and no suns: nothing to do
    assert not sess.ws.sent
    with pytest.raises(PermissionError):
        asyncio.run(sess.send("click", sess.latest_obs, meta={"decision_source": "system0_reflex"},
                              x=10, y=10, reason="plant at (3, 7)"))


def test_repeated_reflex_timeouts_disable_reflexes_for_the_episode(live):
    sess = live()
    async def slow(*args, **kwargs):
        raise TimeoutError("System0 call exceeded wall-time limit")
    sess.player_runtime.step = slow
    for frame in range(5):
        confirmed(sess)
        sess.latest_obs = type(sess.latest_obs)(frame_id=20 + frame, t_capture=0, suns=sess.latest_obs.suns)
        sess.sun_collector.current = sess.latest_obs
        sess.policy._recent_sun_clicks.clear()
        asyncio.run(sess._reflex())
    assert sess._reflex_disabled and not sess._reflex_active()


# ---- research ----

def test_research_reflex_plants_only_engine_legal_cells_and_records_postgame(tmp_path):
    class ReflexRPC(RPC):
        def call(self, op, **payload):
            if op == "reflex":
                self.calls.append(("reflex", payload))
                return {"placement": {"slot": 0, "row": 1, "col": 1}, "spent": payload["spent"] + 100}
            result = super().call(op, **payload)
            return {**result, "placement": None, "reflex_planting": True}
    mask = [True] * 757
    mask[1 + 0 * 54 + 1 * 9 + 1] = False
    for allowed in (True, False):
        arena, rpc = Arena(), ReflexRPC()
        if not allowed:
            arena.info = (lambda base: lambda: {**base(), "action_mask": mask})(arena.info)
        result = run_episode(arena, {"job_id": "fixture", "mode": "state", "book": {"reflex": {"min_plant_interval_s": 2}}},
                             {"level": 2, "seed": 17}, rpc, tmp_path / str(allowed))
        reflex_calls = [c for c in rpc.calls if isinstance(c, tuple)]
        assert reflex_calls and all("postgame" not in c[1] and "timeline" not in c[1] for c in reflex_calls)
        assert ({"slot": 0, "row": 1, "col": 1} in arena.actions) is allowed
        assert result["throughput"]["reflex_rejected_by_engine"] == (0 if allowed else len(reflex_calls))
        assert result["postgame"]["timeline"] and "alignment" in result["postgame"]["tables"]


def test_hidden_engine_fields_never_change_player_inputs():
    plain, noisy = raw(), raw()
    noisy["zombies"][0].update(hp=1, armor=900, target="house")
    noisy["global_state"].update(zombie_spawn_plan=[1, 2, 3], next_wave_tick=42)
    noisy["seed"], noisy["score"] = 1, 2
    assert player_state(plain, [True] * 757, 0) == player_state(noisy, [True] * 757, 0)


# ---- post-game tables ----

def test_alignment_compares_first_decision_after_a_new_plan_with_the_last_one_before():
    defend = {"priorities": ["defense"], "priority_rows": [2], "preferred_decisions": []}
    economy = {"priorities": ["economy"], "priority_rows": [], "preferred_decisions": ["economy"]}
    row2 = signature(None, {"slot": 0, "row": 2, "col": 3}, "peashooter")
    sun = signature({"id": "economy", "tags": ["economy"], "rows": []}, None, None)
    wait = signature(None, None, None)
    decisions = [dict(plan_key=1, plan=defend, sig=row2), dict(plan_key=2, plan=economy, sig=wait),
                 dict(plan_key=2, plan=economy, sig=sun), dict(plan_key=3, plan=defend, sig=row2)]
    result = alignment(decisions)
    assert result["plan_changes"] == 2
    assert result["after"] == {"agreed": 2, "total": 2, "rate": 1.0}
    assert result["before"] == {"agreed": 0, "total": 2, "rate": 0.0}


def test_tables_find_missed_chances_and_mower_moments():
    state = player_state(raw(), [True] * 757, 0)  # peashooter card ready, 125 sun, zombie in row 4
    recorder = EpisodeRecorder()
    for tick in range(0, 600, 100):
        recorder.observe(tick, state)
    recorder.decision(100, {"plan_tick": 0, "plan": {"priorities": ["defense"]}, "scene": state["scene"]}, None)
    recorder.mower(500, 4)
    recorder.observe(600, {**state, "scene": {**state["scene"], "zombies": []}})
    tables = recorder.report()["tables"]
    assert tables["missed_chances"][0]["row"] == 4 and tables["missed_chances"][0]["seconds"] >= 3
    assert tables["costly_moments"][0]["kind"] == "mower" and tables["costly_moments"][0]["row"] == 4
    assert tables["decision_stats"]["waits"] == 1
    assert encode_state(state, 0)["z"][4][0] == 1


def test_live_tables_use_logged_player_view():
    base = time.time()
    scene = {"sun": 200, "rows": 5, "cards": [{"slot": 0, "plant": "peashooter", "ready": True}],
             "zombies": [{"type": "normal", "row": 1, "col": 5}], "nearest_col": {"1": 5}}
    guidance = {"objective": "x", "priorities": ["defense"], "priority_rows": [1]}
    events = [{"t_server": base, "kind": "hello"}]
    events += [{"t_server": base + i, "kind": "jev", "guidance": guidance, "action": "wait", "placement": None,
                "decision_context": {"scene": scene, "plants": [], "sun_spent_since_scene": 0}} for i in range(1, 6)]
    events += [{"t_server": base + 6, "kind": "state", "data": {"mower_events": [{"row": 1, "time_s": 30}]}}]
    tables = analyze_live(events)
    assert tables["missed_chances"] and tables["missed_chances"][0]["row"] == 1
    assert tables["costly_moments"][0]["kind"] == "mower" and tables["decision_stats"]["waits"] == 5


def test_bridge_menu_mode_and_reflex_op_use_player_code_and_hard_rules(settings, monkeypatch):
    from rsi.native_bridge import Bridge
    from rsi.native_jobs import digest
    from server.planner import Plan, PlanResult
    from tests.test_native_throughput import public
    async def run():
        bridge = Bridge(settings)
        try:
            version = bridge.jobs.code.add(SEED)
            book = bridge.jobs.store.load()
            updated = book.model_copy(update={"version": "v002", "parent": book.version, "player_code": version,
                                              "reflex": ReflexParams(allow_planting=True)})
            bridge.jobs.store.write(updated)
            bridge.jobs.store.set_current(updated.version)
            job = bridge.jobs.enqueue("menu-research")
            task = bridge.jobs.request(job)
            async def plan(*args, **kwargs):
                return PlanResult(plan=Plan(objective="fixture", preferred_decisions=["defend_row_4"]), latency=0)
            picked = []
            async def select_menu(ctx, menu, preferred=()):
                picked.append(([m.id for m in menu], list(preferred)))
                return Selection(decision=next(m for m in menu if m.id == "defend_row_4"), latency=0.0)
            monkeypatch.setattr(bridge.planner, "decide", plan)
            monkeypatch.setattr(bridge.selector, "select_menu", select_menu)
            monkeypatch.setattr(bridge.selector, "select", lambda *a, **k: pytest.fail("built-in choice in menu mode"))
            state = public()
            result = await bridge.decide(job, dict(request_digest=digest(task), episode_id="e1", tick=0, **state))
            assert result["decision"]["decision"]["id"] == "defend_row_4" and picked[0][1] == ["defend_row_4"]
            assert [result["placement"][k] for k in ("slot", "row")] == [0, 4]
            assert [result["placement"]["slot"], 4, result["placement"]["col"]] in state["allowed_positions"]
            assert result["reflex_planting"] is True
            answer = await bridge.handle(dict(op="reflex", job_id=job["id"], request_digest=digest(task),
                                              episode_id="e1", tick=25, plan=result["plan"], spent=result["spent"], **state))
            assert answer["placement"] is None  # the seed reflex only picks up suns
        finally:
            await bridge.close()
    asyncio.run(run())
