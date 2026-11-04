"""Training rounds aimed at the level that went wrong: its own games, plus a regression audit."""
import pytest

from rsi.native_gate import TARGETED_COUNTS, compare
from rsi.native_jobs import NativeJobs, STEPS, check_target
from rules.code_gate import compare as code_compare
from tests.test_native_rsi import attach_candidate, report
from tests.test_server import settings

TARGET = {"level": 5, "reason": "mowers", "mowers": 5, "regression_levels": [2, 3, 4]}


def test_target_is_checked_and_explained():
    target = check_target(TARGET)
    assert "1-5" in target["goal"] and "5 lawn mower" in target["goal"] and "conveyor-belt" in target["goal"]
    assert "conveyor" not in check_target({**TARGET, "level": 6})["goal"]
    for bad in ({**TARGET, "level": 11}, {**TARGET, "regression_levels": [5]}, {**TARGET, "reason": "bored"},
                {**TARGET, "mowers": 9}, "1-5"):
        with pytest.raises(ValueError):
            check_target(bad)


def test_targeted_round_plays_the_target_and_audits_won_levels(settings):
    job = NativeJobs(settings).enqueue("t-round", target=TARGET)
    suites = job["suites"]
    assert job["gate"] == "targeted-v1" and job["target"]["level"] == 5
    assert [len(suites[s]) for s in ("development", "confirmation", "audit")] == [6, 6, 6]
    assert {e["level"] for e in suites["development"] + suites["confirmation"]} == {5}
    assert [e["level"] for e in suites["audit"]] == [2, 3, 4, 2, 3, 4]
    seeds = [e["seed"] for s in suites.values() for e in s]
    assert len(seeds) == len(set(seeds))  # every stage on fresh seeds
    first = NativeJobs(settings).enqueue("first-level", target={**TARGET, "level": 2, "regression_levels": []})
    assert {e["level"] for e in first["suites"]["audit"]} == {2}  # nothing won yet: audit the target itself


def test_targeted_round_goes_through_the_same_rules_and_promotes(settings):
    jobs = NativeJobs(settings)
    job = jobs.enqueue("t-promote", target=TARGET)
    for index, (_, mode, side) in enumerate(STEPS):
        if index == 2:
            attach_candidate(jobs, job)
        job = jobs.accept(job, report(jobs, job, .18 if side == "candidate" else .1))
    assert job["promoted"] and all(v["gate"] == "targeted-v1" for v in job["verdicts"])


def test_targeted_counts_are_fixed_and_the_default_gate_is_unchanged():
    def run(n, score):
        return {"environment": {}, "episodes": [{"seed": i, "level": 5, "score": score, "outcome": "win"} for i in range(n)]}
    assert code_compare(run(6, .5), run(6, .9), "confirmation", "targeted-v1")["passed"]
    assert code_compare(run(5, .5), run(5, .9), "confirmation", "targeted-v1")["reason"] == "insufficient paired episodes"
    assert code_compare(run(6, .5), run(6, .9), "confirmation")["reason"] == "insufficient paired episodes"  # paired-v1 needs 12
    assert TARGETED_COUNTS == {"development": 6, "confirmation": 6, "audit": 6}
    with pytest.raises(ValueError):
        compare(run(6, .5), run(6, .9), "confirmation", "made-up-gate")
