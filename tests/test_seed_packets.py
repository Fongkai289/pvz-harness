"""Live play names the seed-bank cards from the screenshot, not from a configured order."""
import numpy as np

from rules import hard_rules
from server import geometry, seed_packets
from server.planner import Placement
from server.scene import Card, Scene, read_cards
from server.seed_packets import EMPTY, UNKNOWN, CardMemory


def templates():
    data = np.load(seed_packets.PATH)
    return data["crops"], [str(label) for label in data["labels"]]


def example(plant):
    crops, labels = templates()
    return crops[labels.index(plant)]


def frame(slots):
    """An 800x600 screenshot with the given crops (or noise) in the seed bank, then an empty slot."""
    image = np.zeros((600, 800, 3), np.uint8)
    rng = np.random.default_rng(1)
    for slot, crop in enumerate(list(slots) + [example(EMPTY)]):
        x0, y0, x1, y1 = geometry.seed_card_rect(slot)
        image[y0:y1, x0:x1] = crop if crop is not None else rng.integers(0, 255, (y1 - y0, x1 - x0, 3))
    return image


def test_every_known_card_is_recognized_also_when_dimmed():
    crops, labels = templates()
    for crop, label in zip(crops, labels):
        assert seed_packets.identify(crop)[0] == label
        if label != EMPTY:  # an unaffordable card is the same art, darker
            dim = (crop.astype(np.float32) * .55).astype(np.uint8)
            assert seed_packets.identify(dim)[0] in (label, UNKNOWN)
    noise = np.random.default_rng(0).integers(0, 255, crops[0].shape, dtype=np.uint8)
    assert seed_packets.identify(noise)[0] == UNKNOWN
    assert set(seed_packets.known_plants()) >= {"peashooter", "sunflower", "cherry bomb", "wall-nut"}


def test_the_bank_is_read_left_to_right_whatever_order_the_cards_were_picked_in():
    picked = [example("wall-nut"), example("peashooter"), example("sunflower")]
    assert [name for name, _ in seed_packets.read_bank(frame(picked))] == ["wall-nut", "peashooter", "sunflower"]


def test_memory_keeps_a_slot_while_it_is_unsure_and_ignores_frames_without_a_bank():
    memory = CardMemory()
    assert memory.observe(frame([example("peashooter"), example("sunflower")])) == ["peashooter", "sunflower"]
    assert memory.observe(frame([example("peashooter"), None])) == ["peashooter", "sunflower"]  # recharging, say
    assert memory.observe(np.zeros((600, 800, 3), np.uint8)) == ["peashooter", "sunflower"]  # menu, no bank
    memory.reset()
    assert memory.observe(frame([None, example("sunflower")])) == [UNKNOWN, "sunflower"]


def test_an_unrecognized_card_is_never_playable():
    [unknown, known] = read_cards(np.full((600, 800, 3), 255, np.uint8), [UNKNOWN, "peashooter"])
    assert not unknown.ready and known.ready
    scene = Scene(frame_id=1, lawn="day", rows=5, sun=500, zombies=[],
                  cards=[Card(slot=0, plant=UNKNOWN, ready=True, brightness=200)])
    assert "not recognized" in hard_rules.check(Placement(slot=0, row=2, col=2), scene, {}, set())
