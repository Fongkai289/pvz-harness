"""When System0 says the plan no longer fits, player code plants in Jev's place until the next plan."""
import asyncio
from dataclasses import replace

import pytest

from rsi.code_runtime import CandidateProgram
from rsi.native_loop import run_episode
from rsi.native_player import SEED, player_input, run_entry
from rsi.postgame import EpisodeRecorder, analyze_live, fallback_stats, find_moments, query_episode
from rules.playbook_rules import FallbackParams
from server.action_selector import Selection
from server.planner import Plan, PlanResult
from server.scene import Card, Scene, Zombie
from tests.test_native_throughput import Arena, RPC
from tests.test_server import settings
from tests.test_system0_actions import live, session

# ---- seed fallback() ----

CARDS = [Card(slot=0, plant="sunflower", ready=True, brightness=1),
         Card(slot=1, plant="peashooter", ready=True, brightness=1),
         Card(slot=2, plant="wall-nut", ready=True, brightness=1),
         Card(slot=3, plant="cherry bomb", ready=True, brightness=1)]


def seed_fallback(zombies, plants):
    scene = Scene(frame_id=1, lawn="day", rows=5, sun=300, cards=CARDS,
                  zombies=[Zombie(type=t, row=r, col=c) for t, r, c in zombies])
    taken = {(p["row"], p["col"]) for p in plants}
    legal = [[s, r, c] for s in range(4) for r in range(5) for c in range(9) if (r, c) not in taken]
    observation = player_input(scene, plants, None, 0, 0, legal=legal, sun_available=300)
    return run_entry(CandidateProgram(SEED), "fallback", observation, {})


def test_seed_shores_up_the_row_hit_soonest_and_saves_bombs_for_close_calls():
    row2 = [{"row": 2, "col": 1, "plants": ["peashooter"]}]
    # Row 0: pole vaulter at the house in ~22 s; row 2: buckethead at the front plant in ~24 s.
    assert seed_fallback([("buckethead", 2, 6), ("pole vaulting", 0, 8)], row2) == [
        {"type": "plant", "slot": 1, "row": 0, "col": 0}]
    # A zombie two tiles from the front plant (~9 s) is a close call: emergency card first.
    close = seed_fallback([("normal", 3, 3)], [{"row": 3, "col": 1, "plants": ["peashooter"]}])
    assert close[0]["slot"] == 3 and close[0]["row"] == 3


def test_seed_keeps_the_sun_when_no_row_is_in_danger():
    assert seed_fallback([], []) == []
    shooters = [{"row": 1, "col": c, "plants": ["peashooter"]} for c in range(3)]
    assert seed_fallback([("normal", 1, 8)], shooters) == []  # 3 shooters kill it in ~6 s, it needs ~28 s


# ---- live session ----

CODE = '''def perceive(image):
    return image

def decide(observation, memory):
    return {"planner_context": {"replan_reasons": ["new threat: row 2"]}, "jev_context": {},
            "request_replan": observation.get("plan_status") is not None}

def options(observation, memory):
    return [{"id": "hold", "text": "Hold the line in row 2", "tags": ["defense"], "rows": [2]}]

def act(observation, memory, decision):
    return []

def fallback(observation, memory):
    memory["status"] = observation["plan_status"]
    BODY
'''
PLANT = 'legal = observation["legal"]\n    return [{"type": "plant", "slot": legal[0][0], "row": legal[0][1], "col": legal[0][2]}]'


def test_live_fallback_plants_in_jevs_place_until_the_next_plan(live):
    sess = live(CODE.replace("BODY", PLANT), fallback=FallbackParams(allow_planting=True))
    sess.episode_active = True
    asyncio.run(sess._jev_step(sess.latest_obs))
    assert sess.selector.menus == []  # Jev was not asked
    assert len(sess.ws.sent) == 2 and len(sess.used_slots) == 1
    assert all(d["meta"]["decision_source"] == "system0_fallback" for k, d in sess.events if k == "cmd")
    step = next(d for k, d in sess.events if k == "fallback")
    assert step["action"] == "plant" and step["rejected"] is None and step["reasons"] == ["new threat: row 2"]
    assert sess.player_runtime.memory["status"]["fallback_reasons"] == ["new threat: row 2"]
    asyncio.run(sess._jev_step(sess.latest_obs))  # inside min_plant_interval_s: hold, still no Jev
    assert len(sess.ws.sent) == 2 and sess.selector.menus == []
    old = sess._plan_id
    sess._end_fallback()  # what plan_loop does when the next plan arrives
    sess._plan_id += 1
    assert not sess._fallback_window()
    end = next(d for k, d in sess.events if k == "fallback_end")
    assert end["plan_id"] == old and len(end["plants"]) == 1
    with pytest.raises(PermissionError):
        asyncio.run(sess.send("click", sess.latest_obs, meta={"decision_source": "system0_fallback"},
                              x=10, y=10, reason="plant at (3, 7)"))


@pytest.mark.parametrize("off", ["playbook", "master_switch", "danger_replan"])
def test_switched_off_fallback_leaves_jev_in_charge(live, off):
    from rules.playbook_rules import TimingParams
    sess = live(CODE.replace("BODY", PLANT), fallback=FallbackParams(allow_planting=off != "playbook"),
                **({"timing": TimingParams(danger_replan=False)} if off == "danger_replan" else {}))
    if off == "master_switch":
        sess.settings = replace(sess.settings, system0_actions=False)
    sess.episode_active = True
    asyncio.run(sess._jev_step(sess.latest_obs))
    assert sess.selector.menus and sess._fallback is None and not sess.ws.sent
    assert not any(k.startswith("fallback") for k, d in sess.events)


def test_failing_fallback_hands_the_step_to_jev_with_the_reasons(live):
    sess = live(CODE.replace("BODY", 'return memory["missing"]'), fallback=FallbackParams(allow_planting=True))
    sess.episode_active = True
    asyncio.run(sess._jev_step(sess.latest_obs))
    assert sess.selector.menus  # Jev decided this step
    assert any(k == "system0_error" and d.get("entry") == "fallback" for k, d in sess.events)
    jev = next(d for k, d in sess.events if k == "jev")
    assert jev["decision_context"]["system0_derived_context"]["plan_no_longer_fits"] == ["new threat: row 2"]


# ---- research bridge ----

def test_bridge_fallback_holds_until_the_spaced_replan(settings, monkeypatch):
    from rsi.native_bridge import Bridge
    from rsi.native_jobs import digest
    from tests.test_native_throughput import public
    async def run():
        bridge = Bridge(settings)
        try:
            version = bridge.jobs.code.add(CODE.replace("BODY", PLANT))
            book = bridge.jobs.store.load()
            updated = book.model_copy(update={"version": "v002", "parent": book.version, "player_code": version,
                                              "fallback": FallbackParams(allow_planting=True)})
            bridge.jobs.store.write(updated)
            bridge.jobs.store.set_current(updated.version)
            job = bridge.jobs.enqueue("fallback-research")
            task = bridge.jobs.request(job)
            async def plan(*args, **kwargs):
                return PlanResult(plan=Plan(objective="fixture", valid_for_seconds=60), latency=0)
            jev = []
            async def select_menu(ctx, menu, preferred=()):
                jev.append(ctx.tool_context)
                return Selection(decision=menu[0], latency=0.0)
            monkeypatch.setattr(bridge.planner, "decide", plan)
            monkeypatch.setattr(bridge.selector, "select_menu", select_menu)
            base = dict(request_digest=digest(task), episode_id="e1", **public())
            first = await bridge.decide(job, dict(base, tick=0))
            assert first["fallback"] is None and len(jev) == 1  # no plan yet, nothing to fall back from
            state = {k: first[k] for k in ("plan", "plan_tick", "spent")}
            held = await bridge.decide(job, dict(base, tick=400, **state))
            assert held["replan"] is None and len(jev) == 1  # inside the 8 s spacing, player code holds
            assert held["fallback"]["action"] == "plant" and held["placement"] == held["fallback"]["placement"]
            assert held["fallback"]["reasons"] == ["new threat: row 2"] and held["spent"] > first["spent"]
            state = {k: held[k] for k in ("plan", "plan_tick", "spent")}
            ready = await bridge.decide(job, dict(base, tick=800, **state))
            assert ready["replan"]["trigger"] == "system0" and ready["fallback"] is None and len(jev) == 2
        finally:
            await bridge.close()
    asyncio.run(run())


# ---- post-game ----

def test_research_loop_records_fallback_windows_apart_from_jev(tmp_path):
    class FallbackRPC(RPC):
        def call(self, op, **payload):
            result = super().call(op, **payload)
            step = {"window": 0, "reasons": ["defense lost: 2:1 peashooter"], "action": "plant",
                    "placement": result["placement"], "rejected": None}
            return {**result, "fallback": step}
    result = run_episode(Arena(), {"job_id": "fixture", "mode": "state"}, {"level": 2, "seed": 17},
                         FallbackRPC(), tmp_path)
    tables = result["postgame"]["tables"]
    assert tables["fallback"]["windows"] == 1 and tables["fallback"]["plants"] == 1
    assert tables["fallback"]["reasons"] == {"defense lost": 1}
    assert tables["decision_stats"]["jev_decisions"] == 0 and tables["decision_stats"]["fallback_plants"] == 1


def test_fallback_windows_close_on_the_next_plan_and_are_queryable():
    recorder = EpisodeRecorder()
    step = {"window": 0, "reasons": ["new threat: zombie in uncovered row 2"], "action": "plant",
            "placement": {"slot": 1, "row": 2, "col": 0}, "rejected": None}
    recorder.fallback(400, step)
    recorder.fallback(500, {**step, "action": "wait", "placement": None})
    recorder.plan(800, {"trigger": "system0", "reasons": step["reasons"]})
    [window] = recorder.fallbacks
    assert (window["t_start"], window["t_end"], len(window["plants"])) == (400, 800, 1)
    windows = [{k: v for k, v in window.items() if k not in ("open", "window")}]
    stats = fallback_stats(windows, [{"t": 450, "row": 2}, {"t": 900, "row": 0}])
    assert stats["seconds"] == 4.0 and stats["mowers_during"] == 1 and stats["list"][0]["mowers"] == [2]
    report = {"episodes": [{"level": 1, "postgame": {"fallbacks": windows}}]}
    assert find_moments(report, "fallback")[0]["t"] == 400
    assert query_episode(report, 0, 0, 1000)["fallbacks"][0]["plants"] == [step["placement"]]
    events = [{"t_server": 0, "kind": "fallback_start", "reasons": step["reasons"]},
              {"t_server": 1, "kind": "fallback", "action": "plant", "placement": step["placement"], "rejected": None},
              {"t_server": 5, "kind": "fallback_end"}]
    live_stats = analyze_live(events)
    assert live_stats["fallback"]["windows"] == 1 and live_stats["fallback"]["seconds"] == 5.0
    assert live_stats["decision_stats"]["fallback_plants"] == 1
