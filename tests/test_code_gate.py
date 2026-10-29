import pytest

from rsi.code_store import CodeStore
from rules.code_gate import compare


def report(scores, *, start=0, outcome="lose", environment="test"):
    return {"environment": environment, "episodes": [
        {"seed": start + i, "score": value, "outcome": outcome}
        for i, value in enumerate(scores)]}


def test_identical_program_never_promotes_and_single_lucky_win_is_rejected():
    base = report([0.2] * 12)
    assert not compare(base, base, "confirmation")["passed"]
    lucky = report([0.8] + [0.2] * 11)
    verdict = compare(base, lucky, "confirmation")
    assert not verdict["passed"] and verdict["sign_test_p"] == 0.5


def test_consistent_fresh_improvement_passes_but_win_regression_does_not():
    base = report([0.2] * 12)
    better = report([0.3] * 12)
    for stage in ("development", "confirmation", "audit"):
        assert compare(base, better, stage)["passed"]
    base["episodes"][0]["outcome"] = "win"
    assert compare(base, better, "confirmation")["reason"] == "fewer completed wins"


@pytest.mark.parametrize("kind", ["seed", "duplicate", "nan", "error", "environment", "size"])
def test_invalid_or_mismatched_evaluations_fail_closed(kind):
    base, candidate = report([0.2] * 12), report([0.4] * 12)
    if kind == "seed":
        candidate["episodes"][0]["seed"] = 999
    elif kind == "duplicate":
        base["episodes"][1]["seed"] = base["episodes"][0]["seed"]
    elif kind == "nan":
        candidate["episodes"][0]["score"] = float("nan")
    elif kind == "error":
        candidate["error"] = "worker timeout"
    elif kind == "environment":
        candidate["environment"] = "different"
    else:
        candidate["episodes"].pop()
    assert not compare(base, candidate, "confirmation")["passed"]


def test_store_requires_all_gates_and_preserves_live_playbook(tmp_path):
    live = tmp_path / "CURRENT"
    live.write_text("v007\n")
    store = CodeStore(tmp_path / "code_rsi")
    parent = store.initialize("old source")
    version = store.add("new source", parent=parent, rationale="test", origin="coach")
    evidence = [{"stage": stage, "passed": True} for stage in ("development", "confirmation", "audit")]
    with pytest.raises(ValueError):
        store.promote(version, parent, evidence[:1])
    assert store.current() == parent
    store.promote(version, parent, evidence)
    assert store.current() == version and live.read_text() == "v007\n"
    with pytest.raises(RuntimeError):
        store.promote(parent, parent, evidence)
    (store.candidates / f"{version}.py").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="modified"):
        store.source(version)


def test_coach_history_never_contains_heldout_scores(tmp_path):
    store = CodeStore(tmp_path)
    store.log(kind="development", version="a", development={"gain": 0.1}, secret_audit=999)
    store.log(kind="confirmation", version="a", gain=0.2)
    store.log(kind="promoted", version="a", evidence=[{"stage": "audit", "score": 999}])
    assert store.coach_history() == [{"version": "a", "development": {"gain": 0.1}}]
