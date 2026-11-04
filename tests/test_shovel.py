"""The shovel: System0 act()/fallback() may dig up one plant, before its plant; no built-in strategy."""
import asyncio

import cv2
import numpy as np
import pytest

from rsi.portable_runtime import player_state as portable_state
from rules import hard_rules
from server import geometry
from server.action_gate import PlantAction, ShovelAction, parse_actions, split_actions
from server.action_selector import DecisionContext, Selection, reject_actions
from server.planner import Placement, Plan
from server.shovel import PATH, find_shovel
from tests.test_action_selector import scene, session
from tests.test_server import settings
from tests.test_system0_actions import MenuSelector, live

DIG_AND_REPLANT = '''def perceive(image):
    return image

def decide(observation, memory):
    return {"planner_context": {}, "jev_context": {}, "request_replan": False}

def options(observation, memory):
    return [{"id": "swap", "text": "Swap the plant in row 2, column 3", "rows": [2]}]

def act(observation, memory, decision):
    return ACTIONS
'''


def frame_with_shovel(x0=456):
    image = np.zeros((600, 800, 3), np.uint8)
    image[0:geometry.SHOVEL_H, x0:x0 + geometry.SHOVEL_W] = cv2.imread(str(PATH))
    return image


@pytest.fixture(autouse=True)
def synthetic_shovel(request, monkeypatch, tmp_path):
    if request.node.originalname == "test_shovel_box_is_found_by_code_wherever_the_seed_bank_puts_it":
        yield  # This separate accuracy check still needs the real local crop.
        return
    from server import shovel
    path = tmp_path / "synthetic-shovel.png"
    crop = np.random.default_rng(17).integers(0, 256, (geometry.SHOVEL_H, geometry.SHOVEL_W, 3), dtype=np.uint8)
    assert cv2.imwrite(str(path), crop)
    monkeypatch.setattr(shovel, "PATH", path)
    monkeypatch.setitem(globals(), "PATH", path)
    shovel._template.cache_clear()
    yield
    shovel._template.cache_clear()


def test_gate_allows_one_shovel_in_act_and_fallback_but_not_in_reflex():
    both = [{"type": "shovel", "row": 2, "col": 3}, {"type": "plant", "slot": 1, "row": 2, "col": 3}]
    actions = parse_actions(both, "act", suns=0)
    assert split_actions(actions) == (ShovelAction(2, 3), PlantAction(1, 2, 3))
    assert parse_actions(both[:1], "fallback", suns=0) == [ShovelAction(2, 3)]
    for raw, entry in [(both[:1] * 2, "act"), (both[:1], "reflex"),
                       ([{"type": "shovel", "row": 2, "col": 3, "slot": 0}], "act"),
                       ([{"type": "shovel", "row": 2, "col": 9}], "act")]:
        with pytest.raises(ValueError):
            parse_actions(raw, entry, suns=0, allow_planting=True)


def test_hard_rule_needs_the_shovel_and_a_diggable_plant():
    sc = scene({}).model_copy(update={"shovel": True})
    lawn = {(2, 3): ["peashooter"], (1, 1): ["grave buster"]}
    assert hard_rules.check_shovel(2, 3, sc, lawn) is None
    assert "no plant" in hard_rules.check_shovel(2, 4, sc, lawn)
    assert "grave buster" in hard_rules.check_shovel(1, 1, sc, lawn)
    assert "outside" in hard_rules.check_shovel(5, 3, sc, lawn)
    for shovel in (False, None):
        assert "no shovel" in hard_rules.check_shovel(2, 3, sc.model_copy(update={"shovel": shovel}), lawn)


def test_a_plant_of_the_same_decision_may_use_the_dug_cell():
    sc = scene({}).model_copy(update={"shovel": True})
    ctx = DecisionContext(sc, {(2, 3): ["peashooter"]}, set(), Plan(objective="fixture"))
    place = Placement(slot=1, row=2, col=3)
    assert "already has" in ctx.reject(place)
    assert reject_actions(ctx, place, ShovelAction(2, 3)) is None
    assert reject_actions(ctx, place, ShovelAction(2, 4)).startswith("shovel:")
    # Research: the engine's mask predates the dig; a slot it lets plant elsewhere may plant there.
    masked = DecisionContext(sc, {(2, 3): ["peashooter"]}, set(), Plan(objective="fixture"),
                             allowed_positions={(1, 0, 5)})
    assert reject_actions(masked, place, ShovelAction(2, 3)) is None
    assert reject_actions(masked, Placement(slot=0, row=2, col=3), ShovelAction(2, 3)) is not None


def test_shovel_box_is_found_by_code_wherever_the_seed_bank_puts_it():
    assert find_shovel(frame_with_shovel(456)) == 456
    assert find_shovel(frame_with_shovel(568)) == 568
    assert find_shovel(np.zeros((600, 800, 3), np.uint8)) is None
    assert geometry.shovel_center(456) == (491, 36)


def test_lawn_ledger_applies_digs_and_plants_in_order(session):
    session.seen_plants, session.seen_plants_frame = {(2, 3): ["peashooter"], (0, 0): ["sunflower"]}, 1
    session.placed_since = [(1, 2, 3, None), (1, 2, 3, "wall-nut"), (2, 0, 0, None)]
    assert session.lawn() == {(2, 3): ["wall-nut"]}
    session.seen_plants_frame = 2  # a newer plant read already shows the first two
    session.seen_plants = {(2, 3): ["wall-nut"], (0, 0): ["sunflower"]}
    assert session.lawn() == {(2, 3): ["wall-nut"]}


def prepared(sess, shovel=True):
    sess.latest_scene = sess.latest_scene.model_copy(update={"shovel": shovel})
    sess.seen_plants = {(2, 3): ["peashooter"]}
    sess.latest_jpeg = cv2.imencode(".jpg", frame_with_shovel())[1].tobytes()
    return sess


def test_live_dig_then_replant_clicks_shovel_cell_card_cell(live):
    sess = prepared(live(DIG_AND_REPLANT.replace("ACTIONS", '[{"type": "shovel", "row": 2, "col": 3}, '
                                                            '{"type": "plant", "slot": 1, "row": 2, "col": 3}]')))
    async def run():
        decision = await sess._jev_step(sess.latest_obs)
        assert decision == (Placement(slot=1, row=2, col=3), ShovelAction(2, 3))
        await sess._execute_placement(*decision)
    asyncio.run(run())
    assert [(c["x"], c["y"]) for c in sess.ws.sent] == [
        geometry.shovel_center(456), geometry.cell_center(2, 3), geometry.seed_slot_center(1), geometry.cell_center(2, 3)]
    assert sess.lawn()[(2, 3)] == [sess.latest_scene.cards[1].plant]
    jev = next(data for kind, data in sess.events if kind == "jev")
    assert jev["action"] == "shovel+plant" and jev["shovel"] == {"row": 2, "col": 3} and not jev["rejected"]
    assert any(kind == "shovel" and data["rejected"] is None for kind, data in sess.events)


@pytest.mark.parametrize("problem", ["no_shovel_on_screen", "empty_cell"])
def test_live_dig_is_refused_without_shovel_or_plant(live, problem):
    sess = prepared(live(DIG_AND_REPLANT.replace("ACTIONS", '[{"type": "shovel", "row": 2, "col": 3}]')),
                    shovel=problem != "no_shovel_on_screen")
    if problem == "empty_cell":
        sess.seen_plants = {}
    assert asyncio.run(sess._jev_step(sess.latest_obs)) is None
    jev = next(data for kind, data in sess.events if kind == "jev")
    assert jev["rejected"].startswith("shovel:") and not sess.ws.sent


def test_portable_state_reports_the_shovel():
    raw = {"global_state": {"sun": 50, "shovel": True}, "cards": [{"slot": 0, "plant_type": 1, "ready": True, "cost": 50}],
           "plants": [], "zombies": []}
    assert portable_state(raw, [True] * 757, 0)["scene"]["shovel"] is True


def test_research_bridge_returns_the_dig_for_the_portable_executor(settings, monkeypatch):
    from rsi.native_bridge import Bridge
    from rsi.native_jobs import digest
    from server.planner import PlanResult
    from tests.test_native_throughput import public
    code = DIG_AND_REPLANT.replace("ACTIONS", '[{"type": "shovel", "row": 4, "col": 0}, '
                                              '{"type": "plant", "slot": 0, "row": 4, "col": 0}]')
    async def run(backend):
        bridge = Bridge(settings)
        try:
            bridge.jobs.backend = backend
            book = bridge.jobs.store.load()
            version = "v002" if backend == "pvz_portable" else "v003"  # versions are immutable
            book = book.model_copy(update={"version": version, "parent": book.version,
                                           "player_code": bridge.jobs.code.add(code)})
            bridge.jobs.store.write(book)
            bridge.jobs.store.set_current(book.version)
            job = bridge.jobs.enqueue("shovel-" + backend)
            task = bridge.jobs.request(job)
            async def plan(*args, **kwargs):
                return PlanResult(plan=Plan(objective="fixture"), latency=0)
            async def select_menu(ctx, menu, preferred=()):
                return Selection(decision=menu[0], latency=0.0)
            monkeypatch.setattr(bridge.planner, "decide", plan)
            monkeypatch.setattr(bridge.selector, "select_menu", select_menu)
            state = public()
            state["scene"]["shovel"] = True
            return await bridge.decide(job, dict(request_digest=digest(task), episode_id="e1", tick=0, **state))
        finally:
            await bridge.close()
    result = asyncio.run(run("pvz_portable"))
    assert result["shovel"] == {"row": 4, "col": 0} and result["placement"] == {"slot": 0, "row": 4, "col": 0}
    refused = asyncio.run(run("pvz_rl"))  # that executor has no shovel: the scene says so
    assert refused["shovel"] is None and refused["decision"]["rejected"].startswith("shovel: no shovel")


def test_training_loop_digs_before_the_step(tmp_path):
    from rsi.native_loop import run_episode
    from tests.test_native_throughput import Arena, RPC
    calls = []
    class DigArena(Arena):
        def shovel(self, cell):
            calls.append(("shovel", cell, self.tick))
        def step(self, placement):
            calls.append(("step", placement, self.tick))
            return super().step(placement)
    class DigRPC(RPC):
        def call(self, op, **payload):
            result = super().call(op, **payload)
            if op == "decide" and not any(c[0] == "shovel" for c in calls):
                result = {**result, "shovel": {"row": 4, "col": 0}}
            return result
    run_episode(DigArena(), {"job_id": "fixture", "mode": "state"}, {"level": 2, "seed": 17}, DigRPC(), tmp_path)
    first = next(i for i, c in enumerate(calls) if c[0] == "shovel")
    assert calls[first + 1][0] == "step" and calls[first][2] == calls[first + 1][2]
