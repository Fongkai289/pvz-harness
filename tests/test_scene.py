import asyncio
import json

import httpx
import numpy as np

from server import geometry
from server.planner import DECIDE_CONTRACT, QwenPlanner
from server.scene import Card, Scene, Zombie, read_cards
from rsi.vision_eval import score_plants, score_scene


def test_score_plants():
    seen = {(0, 1): ["sunflower"], (1, 1): ["peashooter"], (4, 8): ["sunflower"]}
    label = {"0,1": "sunflower", "1,1": "sunflower", "2,2": "wall-nut"}
    assert score_plants(seen, label) == {"cells_found": 2, "types_right": 1, "cells_total": 3, "invented_cells": 1}

SCENE = Scene(frame_id=5, lawn="pool", rows=6, sun=75,
              cards=[Card(slot=0, plant="sunflower", ready=True, brightness=200),
                     Card(slot=1, plant="lily pad", ready=False, brightness=90)],
              zombies=[Zombie(type="conehead", row=1, col=8), Zombie(type="snorkel", row=2, col=None)])


def test_read_cards_uses_brightness():
    image = np.zeros((600, 800, 3), np.uint8)
    for slot, value in ((0, 210), (1, 80)):
        x0, y0, x1, y1 = geometry.seed_card_rect(slot)
        image[y0:y1, x0:x1] = value
    cards = read_cards(image, ["peashooter", "sunflower"])
    assert [c.ready for c in cards] == [True, False]


def test_classify_lawn_from_colour():
    from server.scene import classify_lawn

    day = np.zeros((600, 800, 3), np.uint8); day[:] = (40, 160, 60)  # bright green, BGR
    night = np.zeros((600, 800, 3), np.uint8); night[:] = (60, 40, 30)  # dark blue-grey
    pool = day.copy(); pool[250:380] = (240, 200, 60)  # bright blue water band
    assert [classify_lawn(i) for i in (day, night, pool)] == ["day", "night", "pool"]


def test_scene_describe_is_compact_and_complete():
    text = SCENE.describe()
    assert "pool (6 rows)" in text and "Sun: 75" in text
    assert "0 sunflower (usable)" in text and "1 lily pad (not usable)" in text
    assert "row 1: conehead@col8" in text and "row 2: snorkel" in text


def test_six_row_lawn_geometry():
    assert geometry.cell_center(5, 0, rows=6)[1] > geometry.cell_center(4, 0, rows=5)[1] - 100
    try:
        geometry.cell_center(5, 0)  # five-row lawn has no row 5
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_score_scene_counts_found_missed_invented():
    label = {"sun": 75, "lawn": "pool", "greyed": [1], "occluded": [],
             "zombie_types": ["conehead", "buckethead", "snorkel"]}
    sc = score_scene(SCENE, label)
    assert sc["sun_ok"] and sc["lawn_ok"] and sc["cards_ok"] == 2
    assert sc["types_found"] == 2 and sc["types_missed"] == ["buckethead"] and sc["types_invented"] == []


def test_decide_reads_the_scene_not_the_picture():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"placements": [{"slot": 0, "row": 1, "col": 2}], "note": "defend row 1"})}}]})

    planner = QwenPlanner("http://qwen", "Qwen3.8-27B", send_image=False, include_summary=True, timeout=5,
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = asyncio.run(planner.decide(SCENE, "Buy Sunflowers first."))
    assert result.plan.placements[0].row == 1
    assert seen["messages"][0]["content"].startswith(DECIDE_CONTRACT)
    content = seen["messages"][1]["content"]
    assert [p["type"] for p in content] == ["text"] and "row 1: conehead@col8" in content[0]["text"]
