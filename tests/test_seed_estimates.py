"""Seed System0 turns static numbers into per-row times so Jev and Planner compare instead of compute."""
from rsi.code_runtime import CandidateProgram
from rsi.native_player import SEED, player_input, run_entry
from server.mechanics import player_reference
from server.scene import Card, Scene, Zombie

CARDS = [Card(slot=0, plant="sunflower", ready=True, brightness=1, cost=50),
         Card(slot=1, plant="peashooter", ready=True, brightness=1),
         Card(slot=2, plant="wall-nut", ready=True, brightness=1)]
ROW2 = [{"row": 2, "col": 1, "plants": ["peashooter"]}, {"row": 2, "col": 0, "plants": ["sunflower"]}]


def observe(zombies, plants=ROW2, sun=150):
    scene = Scene(frame_id=1, lawn="day", rows=5, sun=sun, cards=CARDS,
                  zombies=[Zombie(type=t, row=r, col=c) for t, r, c in zombies])
    taken = {(p["row"], p["col"]) for p in plants}
    legal = [[s, r, c] for s in range(3) for r in range(5) for c in range(9) if (r, c) not in taken]
    return player_input(scene, plants, None, 0, 0, legal=legal, sun_available=sun)


def entry(name, observation):
    return run_entry(CandidateProgram(SEED), name, observation, {})


def test_reference_is_static_full_health_and_null_when_unverified():
    scene = Scene(frame_id=1, lawn="day", rows=5, sun=0, cards=CARDS,
                  zombies=[Zombie(type="pole vaulting", row=0, col=8), Zombie(type="unknown", row=1, col=None)])
    ref = player_reference(scene, [{"row": 0, "col": 0, "plants": ["repeater", "threepeater"]}])
    assert ref["plants"]["peashooter"] == {"cost": 100, "hp": 300, "dps": 14.04, "sun_per_cycle": None, "cycle_s": None}
    assert ref["plants"]["sunflower"]["sun_per_cycle"] == 25 and ref["plants"]["sunflower"]["cycle_s"] == 24
    assert ref["plants"]["repeater"]["dps"] == 28.07  # two 20-damage peas every 1.425 s
    assert ref["plants"]["threepeater"]["dps"] is None  # cost only in the reference table
    assert ref["zombies"]["pole vaulting"]["s_per_tile"] == 2.5  # fastest listed phase
    assert ref["zombies"]["unknown"] == {"hp": None, "chew_dps": None, "s_per_tile": None}


def test_row_estimates_match_hand_arithmetic():
    context = entry("decide", observe([("buckethead", 2, 6), ("normal", 2, 8)]))["planner_context"]
    [row] = context["row_estimates"]
    # Buckethead at col 6, front plant at col 1: 5 tiles x 4.7 s; house: 7 tiles.
    # 1370 + 270 HP against one peashooter (20 / 1.425 s).
    assert row == {"row": 2, "zombies": 2, "first": "buckethead", "col": 6, "hp": 1640, "front_s": 24,
                   "house_s": 33, "dps": 14.0, "unrated": 0, "kill_s": 117, "holds": False}
    assert "row_estimates_note" in context
    assert "row_estimates" not in entry("decide", observe([]))["planner_context"]


def test_menu_lines_carry_the_comparisons_within_the_length_limit():
    menu = {m["id"]: m["text"] for m in entry("options", observe([("buckethead", 2, 6), ("normal", 2, 8)]))}
    assert menu["defend_row_2"] == ("Row 2: add a peashooter (2 zombies; buckethead col 6 reaches front plant ~24s; "
                                    "row kills all ~117s, too slow, with it ~58s, too slow; sun after 50)")
    assert "holds one ~40s" in menu["wall_row_2"] and menu["wall_row_2"].endswith("sun after 100)")
    assert menu["economy"] == "Economy: plant a sunflower for sun income (+25 sun every ~24s; sun after 100)"
    assert menu["defend_row_1"] == "Row 1: add a peashooter (no zombie seen; sun after 50)"
    assert all(len(text) <= 160 for text in menu.values())


def test_unknown_numbers_give_no_estimate_instead_of_a_guess():
    plants = [{"row": 3, "col": 1, "plants": ["threepeater"]}]
    rows = entry("decide", observe([("unknown", 1, 5), ("normal", 3, 7)], plants=plants))["planner_context"]["row_estimates"]
    unknown, unrated = rows
    assert unknown["hp"] is None and unknown["house_s"] is None and unknown["kill_s"] is None
    assert unrated["unrated"] == 1 and unrated["kill_s"] is None and unrated["holds"] is None
    menu = {m["id"]: m["text"] for m in entry("options", observe([("unknown", 1, 5)], plants=plants, sun=None))}
    assert menu["defend_row_1"] == "Row 1: add a peashooter (1 zombie)"
