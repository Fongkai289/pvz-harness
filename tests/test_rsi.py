import json

import pytest
from pydantic import ValidationError

from client.mock_client import FakeGame
from common.protocol import FrameHeader
from rsi.evaluate import diagnose
from rsi.loop import gate
from rules.playbook_rules import Playbook
from server.judge import score
from server.perception import Perception, decode_jpeg
from server.playbook import PlaybookStore


def test_rules_reject_out_of_range_and_unknown_fields():
    base = json.loads(open("playbook_seed/v001.json").read())
    for bad in (
        {**base, "perception": {**base["perception"], "sun_hsv_hi": [200, 255, 255]}},
        {**base, "perception": {**base["perception"], "sun_min_area": 6000, "sun_max_area": 5000}},
        {**base, "fast": {**base["fast"], "sun_click_cooldown": 0}},
        {**base, "judge": {"penalty": 0}},  # the coach may not smuggle in new sections
    ):
        with pytest.raises(ValidationError):
            Playbook.model_validate(bad)


def test_store_seeds_versions_and_refuses_overwrite(tmp_path):
    store = PlaybookStore(tmp_path)
    assert store.current() == "v001" and store.versions() == ["v001"]
    v2 = store.load().model_copy(update={"version": store.next_version(), "parent": "v001"})
    store.write(v2)
    with pytest.raises(FileExistsError):
        store.write(v2)
    store.set_current("v002")
    assert store.current() == "v002"
    with pytest.raises(FileNotFoundError):
        store.set_current("v009")


def test_judge_score():
    assert score({"spawned": 10, "collected": 8, "wasted_clicks": 2}) == 0.7
    assert score({"sun": 50}) is None


def test_seed_playbook_falls_for_decoys_and_a_tighter_one_does_not():
    """The fake game's traps are real: the seed hue range sees the decoys, a tighter one doesn't."""
    game = FakeGame(seed=1)
    game.suns = []  # decoys only
    header = FrameHeader(frame_id=1, t_capture=0.0, width=800, height=600)
    image = decode_jpeg(game.grab()[0])
    seed = Playbook.model_validate_json(open("playbook_seed/v001.json").read())
    assert len(Perception(seed.perception).process(header, image).suns) == 3
    tight = seed.perception.model_copy(update={"sun_hsv_hi": (30, 255, 255)})
    assert Perception(tight).process(header, image).suns == []


def test_diagnose_joins_clicks_with_outcomes():
    events = [
        {"kind": "cmd", "cmd_id": 1, "reason": "collect sun", "meta": {"hue": 25.0, "area": 1200}},
        {"kind": "cmd", "cmd_id": 2, "reason": "collect sun", "meta": {"hue": 33.0, "area": 1000}},
        {"kind": "exec", "cmd_id": 1, "result": {"collected": 1, "decoy": False}},
        {"kind": "exec", "cmd_id": 2, "result": {"collected": 0, "decoy": True}},
    ]
    d = diagnose(events)
    assert d["hit_clicks"]["count"] == 1 and d["decoy_clicks"]["count"] == 1
    assert d["decoy_clicks"]["hue"]["median"] == 33.0


def test_decider_option_keys_are_fixed_and_old_versions_load_with_defaults():
    base = json.loads(open("playbook_seed/v001.json").read())
    assert "decider" not in base and "planner" not in base
    pb = Playbook.model_validate(base)  # written before those sections existed
    assert set(pb.decider.criteria) == {"place", "wait"} and pb.planner.knowledge
    assert sorted(pb.jev.priorities) == sorted(["follow_plan", "emergency", "attack", "reinforce", "economy", "wait"])
    with pytest.raises(ValidationError):  # the coach can reorder actions, not rename or drop them
        Playbook.model_validate({**base, "jev": {"priorities": ["attack", "wait"]}})
    with pytest.raises(ValidationError):
        Playbook.model_validate({**base, "decider": {"instructions": "Place it now or not?",
                                                     "criteria": {"yes": "place it", "no": "do not"}}})


def test_strategy_knowledge_block_fits_the_rules():
    from rsi.human_edit import planner_block
    from rules.playbook_rules import PlannerKnowledge

    text = planner_block(__import__("pathlib").Path("knowledge/pvz_strategy.md"))
    assert text.startswith("Economy") and "Sunflower" in text
    assert PlannerKnowledge(knowledge=text).knowledge == text  # within the length limit
    assert "Sources" not in text  # notes for people stay out of the planner's prompt


def test_coach_diff_names_section_and_field():
    from rsi.coach import Proposal, diff

    current = Playbook.model_validate_json(open("playbook_seed/v001.json").read())
    proposal = Proposal(planner={"knowledge": "Buy Sunflowers first."}, rationale="test")
    changes = diff(current, proposal)
    assert list(changes) == ["planner.knowledge"] and changes["planner.knowledge"][1] == "Buy Sunflowers first."
    candidate = proposal.apply(current, "v002")
    assert candidate.parent == "v001" and candidate.planner.knowledge == "Buy Sunflowers first."
    assert candidate.perception == current.perception  # omitted sections are kept byte for byte


def test_gate_needs_gain_and_consistency():
    base = {"seeds": [1, 2, 3], "scores": {1: 0.5, 2: 0.5, 3: 0.5}, "mean": 0.5}
    better = {"scores": {1: 0.6, 2: 0.6, 3: 0.45}, "mean": 0.55}
    lucky = {"scores": {1: 0.9, 2: 0.4, 3: 0.4}, "mean": 0.5667}
    assert gate(base, better)[0] is True
    assert gate(base, lucky)[0] is False  # big mean gain from one seed is not enough
