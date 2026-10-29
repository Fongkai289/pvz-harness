"""Regression coverage for legacy rule tactics and fixed placement checks."""
import pytest
from pydantic import ValidationError

from rules import hard_rules
from rules.playbook_rules import JevPolicy
from server.decider import JevContext, Judgments, choose, facts
from server.planner import Placement
from server.scene import Card, Scene, Zombie

LOADOUT = ["peashooter", "sunflower", "cherry bomb", "wall-nut", "potato mine", "snow pea"]
POLICY = JevPolicy()
NONE = Judgments()
NO_ATTACK = POLICY.model_copy(update={"priorities": ["emergency", "reinforce", "follow_plan", "economy", "attack", "wait"],
                                      "attack_rows": "threatened", "shooters_per_row": 0, "shooters_per_tough_row": 0})


def scene(nearest: dict[int, int], ready=(0, 1, 2, 3, 4, 5), sun=300, types=None) -> Scene:
    return Scene(frame_id=1, lawn="day", rows=5, sun=sun,
                 cards=[Card(slot=i, plant=p, ready=i in ready, brightness=0) for i, p in enumerate(LOADOUT)],
                 zombies=[Zombie(type=(types or {}).get(r, "normal"), row=r) for r in nearest], nearest_col=nearest)


def ctx(sc: Scene, plan_next=None, planted=None, used=None) -> JevContext:
    return JevContext(scene=sc, plan_next=plan_next, planted=planted or {}, used_slots=used or set())


def act(c, policy=POLICY, j=NONE):
    return choose(c, policy, j)


def test_tactics_follow_priorities_and_thresholds():
    assert act(ctx(scene({2: 2})))[0] == "emergency"  # within danger_col 3
    assert act(ctx(scene({2: 5})))[0] == "attack"  # row 2 has no shooter
    shooters = {(r, 2): ["snow pea"] for r in range(5)}
    assert act(ctx(scene({2: 5}), planted=shooters))[0] == "reinforce"  # firepower there, no wall
    walled = {**shooters, (2, 4): ["wall-nut"]}
    assert act(ctx(scene({2: 5}), Placement(slot=1, row=1, col=0), walled))[0] == "follow_plan"
    assert act(ctx(scene({}), planted=shooters))[0] == "economy"
    assert act(ctx(scene({}, ready=())))[0] == "wait"


def test_attack_fills_threatened_rows_first_with_the_preferred_card():
    action, why, place = act(ctx(scene({3: 6})))
    assert action == "attack" and place.row == 3 and place.col == 2
    assert place.slot == 5  # snow pea is listed before peashooter in attack_plants
    tough = Judgments(tough={3: 0.9})
    one = {(3, 2): ["snow pea"]}
    assert act(ctx(scene({3: 6}), planted=one), j=tough)[2].row == 3  # tough row wants 2 shooters
    assert act(ctx(scene({3: 6}), planted=one))[2].row != 3  # normal row is satisfied, next row gets one


def test_where_comes_from_the_playbook():
    eco_far = NO_ATTACK.model_copy(update={"economy_cols": [3]})
    assert act(ctx(scene({})), eco_far)[2].col == 3
    wall_close = NO_ATTACK.model_copy(update={"wall_gap": 2, "wall_max_col": 8})
    assert act(ctx(scene({1: 7})), wall_close)[2].col == 5
    em_left = POLICY.model_copy(update={"emergency_offsets": [-1]})
    assert act(ctx(scene({2: 2})), em_left)[2].col == 1


def test_an_action_that_cannot_be_placed_does_not_block_the_rest():
    planned = Placement(slot=0, row=0, col=2)
    c = ctx(scene({2: 3}, ready=(0, 1, 3, 5)), planned, planted={(r, 2): ["snow pea"] for r in range(5)})
    action, reason, _ = act(c, POLICY.model_copy(update={"danger_col": 2}))
    assert action == "follow_plan" and "skipped, no placement: reinforce" in reason


def test_tough_judgment_widens_the_emergency_range():
    c = ctx(scene({2: 5}, types={2: "football"}), planted={(r, 2): ["snow pea", "peashooter"] for r in range(5)})
    assert act(c)[0] == "reinforce"
    action, reason, place = act(c, j=Judgments(tough={2: 0.9}, source="onejev"))
    assert action == "emergency" and "tough" in reason and place.row == 2


def checked(c, sun_spent=0):
    """The context the session builds: tactics see the hard rules."""
    c.check = lambda p: hard_rules.check(p, c.scene, c.planted, c.used_slots, sun_spent)
    return c


def test_armed_plants_go_ahead_of_the_zombie():
    # Real run 2026-10-29: a potato mine went in at the zombie's own column, i.e. behind it once armed.
    tough_row = Judgments(tough={2: 0.9})
    action, _, place = act(checked(ctx(scene({2: 5}, ready=(4,)))), j=tough_row)
    assert action == "emergency" and (place.slot, place.row, place.col) == (4, 2, 2)  # 3 columns ahead
    assert act(checked(ctx(scene({2: 2}, ready=(4,)))))[2] is None  # no room left to arm in time


def test_hard_rules_want_traps_well_ahead():
    sc = scene({2: 5}, sun=500)
    assert hard_rules.check(Placement(slot=4, row=2, col=2), sc, {}, set()) is None
    assert "3-6 columns to its right" in hard_rules.check(Placement(slot=4, row=2, col=5), sc, {}, set())
    assert "3-6 columns to its right" in hard_rules.check(Placement(slot=4, row=2, col=6), sc, {}, set())


def test_economy_first_plants_sunflowers_before_attackers():
    # Real run 2026-10-29: zombies always within column 5, so economy never ran and no Sunflower was placed.
    c = ctx(scene({2: 4}))
    assert act(c)[0] == "attack"
    early = POLICY.model_copy(update={"economy_first": 3})
    action, why, place = act(c, early)
    assert action == "economy" and place.slot == 1 and place.col == 0 and "economy_first 3" in why
    three = {(r, 0): ["sunflower"] for r in range(3)}
    assert act(ctx(scene({2: 4}), planted=three), early)[0] == "attack"  # floor reached: back to normal


def test_tactics_pass_over_what_the_hard_rules_reject():
    # 75 sun, no wall-nut card: snow pea (175) and peashooter (100) are unaffordable, so attack yields
    # to the next action instead of being proposed and rejected again; the Sunflower fits.
    c = checked(ctx(scene({2: 6}, ready=(0, 1, 5), sun=75), planted={(r, 0): ["sunflower"] for r in range(5)}))
    action, why, place = act(c, POLICY.model_copy(update={"economy_safe_col": 4}))
    assert action == "economy" and place.slot == 1
    # 120 sun: the preferred snow pea is too dear, the peashooter is taken instead of retrying.
    assert act(checked(ctx(scene({2: 6}, sun=120))))[2].slot == 0


def test_playbook_rejects_bad_where_settings():
    with pytest.raises(ValidationError):
        JevPolicy(priorities=["attack", "wait"])
    with pytest.raises(ValidationError):
        JevPolicy(tough_question="Is it tough? No placeholder here.")
    with pytest.raises(ValidationError):
        JevPolicy(emergency_offsets=[3])  # instant kills reach one column
    with pytest.raises(ValidationError):
        JevPolicy(wall_min_col=7, wall_max_col=4)


def test_hard_rules_reject_whatever_proposed_it():
    sc = scene({2: 2}, ready=(0, 1, 2, 3, 5), sun=120)
    assert "costs 150" in hard_rules.check(Placement(slot=2, row=2, col=2), sc, {}, set())
    rich = scene({2: 2}, sun=500)
    assert hard_rules.check(Placement(slot=2, row=2, col=2), rich, {}, set()) is None
    assert "no zombie within reach" in hard_rules.check(Placement(slot=2, row=2, col=6), rich, {}, set())
    assert "already has" in hard_rules.check(Placement(slot=0, row=1, col=1), rich, {(1, 1): ["sunflower"]}, set())
    assert hard_rules.check(Placement(slot=0, row=1, col=1), rich, {(1, 1): ["lily pad"]}, set()) is None
    assert "not usable" in hard_rules.check(Placement(slot=4, row=1, col=1), sc, {}, set())
    assert "already used" in hard_rules.check(Placement(slot=0, row=1, col=1), rich, {}, {0})
    assert "outside" in hard_rules.check(Placement(slot=0, row=5, col=1), rich, {}, set())
    assert "sun left is 50" in hard_rules.check(Placement(slot=0, row=0, col=2), rich, {}, set(), sun_spent=450)


def test_facts_have_no_rules_text():
    text = facts(scene({1: 4}, types={1: "conehead"}), {(1, 2): ["lily pad", "peashooter"]})
    assert "Row 1: zombies conehead, nearest at column 4; plants peashooter on lily pad at column 2." in text
    assert "danger" not in text and "close" not in text  # thresholds stay in code
