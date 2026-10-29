import asyncio
import json
from pathlib import Path

import cv2
import httpx
import numpy as np

from server import geometry
from server.planner import DECIDE_CONTRACT, QwenPlanner
from server.scene import Card, RowReport, Scene, Zombie, measure_zombies, place_zombies, read_cards
from rsi.vision_eval import score_plants, score_scene

FIXTURES = Path(__file__).parent / "fixtures"


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


def test_measure_zombies_on_real_day_frames():
    # Frames of the first real run (2026-10-29, level 1-6); positions checked by eye.
    frame = lambda n: cv2.imread(str(FIXTURES / f"day_1-6_f{n:04d}.jpg"))
    assert measure_zombies(frame(73), 5) == [(2, 3), (1, 7)]  # the model said rows 3 and 4
    assert measure_zombies(frame(405), 5) == [(2, 0), (1, 3), (4, 8)]
    assert measure_zombies(frame(1022), 5) == [(4, 2), (3, 6)]  # coneheads; wall-nut, mines ignored
    # A row-0 zombie's head is over the seed bar; only its lower part shows (second run, frame 2574).
    assert (0, 3) in measure_zombies(cv2.imread(str(FIXTURES / "day_1-6_run2_f2574.jpg")), 5)
    # The pause menu's newspaper zombie is not on the lawn.
    assert measure_zombies(cv2.imread(str(FIXTURES / "day_1-6_paused.jpg")), 5) == []


def test_place_zombies_takes_rows_from_code_and_types_from_the_model():
    rows = lambda *reports: [RowReport(zombies=z, nearest_col=c) for z, c in reports]
    empty = ([], None)
    # Off by one: the model's types move to the measured row.
    zs, nearest = place_zombies(rows(empty, (["conehead"], 3), empty, empty, empty), [(2, 3)])
    assert [(z.type, z.row, z.col) for z in zs] == [("conehead", 2, 3)] and nearest == {2: 3}
    # Off by more (f66): two zombies measured, two reported far away; no phantom row is left.
    zs, nearest = place_zombies(rows(empty, empty, empty, (["normal"], 3), (["normal"], 7)), [(2, 3), (1, 7)])
    assert sorted((z.row, z.col) for z in zs) == [(1, 7), (2, 3)] and nearest == {1: 7, 2: 3}
    # Code cannot see one still entering: the model's extra zombie is kept where it said.
    zs, nearest = place_zombies(rows(empty, (["normal"], 0), empty, empty, (["conehead"], 9)), [(1, 0)])
    assert nearest == {1: 0, 4: 9} and {z.type for z in zs} == {"normal", "conehead"}
    # Invented on an empty lawn (second run, frame 164: five "zombonis" at column 0): dropped.
    zs, nearest = place_zombies(rows(*[(["zomboni"], 0)] * 5), [])
    assert zs == [] and nearest == {}


def test_look_plants_reads_a_code_matrix_from_the_cell_mosaic():
    import base64

    from server.scene import QwenLooker, plant_mosaic

    seen = {}
    matrix = [[0] * 9 for _ in range(5)]
    matrix[0][0], matrix[1][2], matrix[4][8] = 3, 1, 4  # sorted names: peashooter, potato mine, sunflower, other

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"rows": matrix})}}],
                                         "usage": {"completion_tokens": 159}})

    looker = QwenLooker("http://qwen", "Qwen3.8-27B", timeout=5,
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    image = cv2.imread(str(FIXTURES / "day_1-6_f0073.jpg"))
    result = asyncio.run(looker.look_plants(image, ["sunflower", "peashooter", "potato mine"]))
    assert result.plants == {(0, 0): ["sunflower"], (1, 2): ["peashooter"], (4, 8): ["other plant"]}
    content = seen["messages"][0]["content"]
    sent = cv2.imdecode(np.frombuffer(base64.b64decode(content[0]["image_url"]["url"].split(",")[1]), np.uint8), 1)
    assert sent.shape == plant_mosaic(image, 5).shape  # the tiles, not the screenshot
    assert "1 = peashooter, 2 = potato mine, 3 = sunflower, 4 = other plant" in content[1]["text"]


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
            {"objective": "Defend threatened rows", "priority_rows": [1], "note": "defend row 1"})}}]})

    planner = QwenPlanner("http://qwen", "Qwen3.8-27B", send_image=False, include_summary=True, timeout=5,
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = asyncio.run(planner.decide(SCENE, "Buy Sunflowers first."))
    assert result.plan.priority_rows == [1]
    assert seen["messages"][0]["content"].startswith(DECIDE_CONTRACT)
    content = seen["messages"][1]["content"]
    assert [p["type"] for p in content] == ["text"] and "row 1: conehead@col8" in content[0]["text"]
