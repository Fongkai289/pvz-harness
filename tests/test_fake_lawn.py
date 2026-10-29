"""The fake lawn game's rules and drawing."""

import asyncio

from client.fake_lawn import FakeLawn, Zombie, ZOMBIES
from common.protocol import Command
from rules.hard_rules import PLANT_COSTS
from server import geometry
from server.judge import score
from server.perception import Perception, decode_jpeg
from server.scene import classify_lawn, read_cards
from rules.playbook_rules import Playbook


def click(game, x, y):
    return asyncio.run(game.execute(Command(cmd_id=1, action="click", x=x, y=y)))


def plant(game, slot, row, col):
    click(game, *geometry.seed_slot_center(slot))
    return click(game, *geometry.cell_center(row, col))


def run_steps(game, n):
    for _ in range(n):
        game.step()


def test_planting_costs_sun_and_starts_recharge():
    g = FakeLawn(seed=1, start_sun=200)
    assert plant(g, 1, 2, 0) == {"planted": "sunflower", "row": 2, "col": 0}
    assert g.sun == 150 and not g.card_usable(1)  # recharging
    assert plant(g, 1, 2, 1) == {"planted": None} or click(g, *geometry.seed_slot_center(1)) == {"selected": None}
    assert plant(g, 0, 2, 0)["planted"] is None  # cell taken


def test_peashooter_kills_and_zombie_eats_unprotected_plant():
    g = FakeLawn(seed=1, start_sun=1000)
    g.level.schedule = []
    plant(g, 0, 1, 0)  # peashooter
    g.zombies.append(Zombie("normal", 1, 700.0, *ZOMBIES["normal"]))
    run_steps(g, 900)
    assert g.counters["killed"] == 1 and g.outcome == "win"

    g2 = FakeLawn(seed=1, start_sun=1000)
    g2.level.schedule = []
    plant(g2, 1, 3, 4)  # a sunflower in the way, nothing shooting
    g2.zombies.append(Zombie("normal", 3, 500.0, *ZOMBIES["normal"]))
    run_steps(g2, 600)
    assert g2.counters["plants_eaten"] == 1 and (3, 4) not in g2.plants


def test_mower_once_then_lose():
    g = FakeLawn(seed=1)
    g.level.schedule = [(100_000, "normal", 4)]  # not over yet: one zombie still to come
    g.zombies.append(Zombie("normal", 0, 60.0, *ZOMBIES["normal"]))
    run_steps(g, 60)
    assert g.counters["mowers_used"] == 1 and g.outcome != "lose"
    g.zombies.append(Zombie("normal", 0, 60.0, *ZOMBIES["normal"]))
    run_steps(g, 60)
    assert g.outcome == "lose"


def test_cherry_bomb_and_potato_mine():
    g = FakeLawn(seed=1, start_sun=1000)
    g.level.schedule = []
    for r in (1, 2, 3):
        g.zombies.append(Zombie("buckethead", r, geometry.cell_center(r, 5)[0], *ZOMBIES["buckethead"]))
    plant(g, 2, 2, 5)  # cherry bomb, 3x3
    run_steps(g, 25)
    assert g.counters["killed"] == 3


def test_drawing_matches_what_perception_reads():
    g = FakeLawn(seed=1, start_sun=75)
    image = decode_jpeg(g.grab()[0])
    cards = read_cards(image, g.loadout)
    usable = [c.slot for c in cards if c.ready]
    assert usable == [s for s, p in enumerate(g.loadout) if PLANT_COSTS[p] <= 75]  # sunflower, potato mine
    assert classify_lawn(image) == "day"
    seed = Playbook.model_validate_json(open("playbook_seed/v001.json").read())
    g.suns = []
    assert Perception(seed.perception).process(_header(), image).suns == []  # bar icon is not a sun


def test_lawn_score():
    assert score({"game": "lawn", "killed": 10, "scheduled": 10, "outcome": "win", "mowers_used": 0}) == 1.0
    assert score({"game": "lawn", "killed": 4, "scheduled": 10, "outcome": "lose", "mowers_used": 2}) == 0.0


def _header():
    from common.protocol import FrameHeader
    return FrameHeader(frame_id=1, t_capture=0.0, width=800, height=600)
