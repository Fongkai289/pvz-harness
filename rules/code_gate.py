"""Fixed promotion rules for code evolution; never writable by the coach.

Development screens proposals. Fresh confirmation pairs test the selected proposal;
an independent audit checks outcome regressions. Seeds are paired within each stage.
These rules are intentionally independent of the coach's prompt and generated code.
"""

from __future__ import annotations

import math

MIN_GAIN = 0.02
MIN_PAIRS = {"development": 8, "confirmation": 12, "audit": 12}
CONFIRM_P = 0.05
GATE_VERSION = "paired-v1"


def compare(base: dict, candidate: dict, stage: str) -> dict:
    if stage not in MIN_PAIRS:
        raise ValueError("unknown promotion stage")
    left, right = base.get("episodes", []), candidate.get("episodes", [])
    reason = None
    if base.get("error") or candidate.get("error"):
        reason = "evaluation failed"
    elif len(left) < MIN_PAIRS[stage] or len(left) != len(right):
        reason = "insufficient paired episodes"
    elif base.get("environment") != candidate.get("environment"):
        reason = "different evaluation environments"
    elif len({r.get("seed") for r in left}) != len(left):
        reason = "duplicate seeds"
    elif [r.get("seed") for r in left] != [r.get("seed") for r in right]:
        reason = "unpaired seeds"
    elif any(r.get("error") or not isinstance(r.get("score"), (int, float))
             or not math.isfinite(r["score"]) or not 0 <= r["score"] <= 1
             for r in left + right):
        reason = "invalid episode score"
    if reason:
        return {"passed": False, "stage": stage, "reason": reason, "gate": GATE_VERSION}
    differences = [b["score"] - a["score"] for a, b in zip(left, right)]
    gain = sum(differences) / len(differences)
    positive = sum(d > 1e-9 for d in differences)
    negative = sum(d < -1e-9 for d in differences)
    n = positive + negative
    # Exact one-sided sign test, ignoring ties; no normal approximation for small samples.
    p = sum(math.comb(n, k) for k in range(positive, n + 1)) / 2**n if n else 1.0
    wins_before = sum(r.get("outcome") == "win" for r in left)
    wins_after = sum(r.get("outcome") == "win" for r in right)
    if wins_after < wins_before:
        reason = "fewer completed wins"
    elif stage == "audit":
        reason = "audit regression" if gain < -1e-9 else None
    elif gain < MIN_GAIN:
        reason = "gain below fixed minimum"
    elif stage == "development" and positive < math.ceil(len(left) * 0.6):
        reason = "gain not consistent across scenarios"
    elif stage == "confirmation" and p > CONFIRM_P:
        reason = "fresh confirmation is not convincing"
    return {"passed": reason is None, "stage": stage, "reason": reason or "passed",
            "gain": round(gain, 6), "positive": positive, "negative": negative,
            "sign_test_p": p, "wins_before": wins_before, "wins_after": wins_after,
            "gate": GATE_VERSION}
