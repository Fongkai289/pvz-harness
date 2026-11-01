"""Fixed, paired admission rules for pvz_rl. Never editable by Coach."""
import math

COUNTS = {"development": 8, "confirmation": 12, "audit": 12}
SCORE_VERSION = "native-win-progress-mowers-v1"


def episode_score(outcome, progress, elapsed_s, mower_times):
    if outcome not in {"win", "lose"}:
        raise ValueError("A truncated/unfinished game is not a completed evaluation")
    if not 0 <= progress <= 1 or not math.isfinite(elapsed_s) or elapsed_s < 0:
        raise ValueError("invalid progress/time")
    if any(not math.isfinite(t) or not 0 <= t <= elapsed_s for t in mower_times):
        raise ValueError("invalid mower timing")
    base = 1.0 if outcome == "win" else .2 * progress
    return max(0.0, base - sum(.1 + .1 * math.exp(-t / 60) for t in mower_times))


def compare(base, candidate, stage):
    from rules.code_gate import compare as code_compare
    if stage not in COUNTS:
        raise ValueError("unknown stage")
    left, right = base.get("episodes", []), candidate.get("episodes", [])
    reason = None
    if len(left) != COUNTS[stage] or len(right) != COUNTS[stage]:
        reason = "incomplete episode count"
    elif base.get("environment") != candidate.get("environment"):
        reason = "environment mismatch"
    elif base.get("environment", {}).get("score_version") != SCORE_VERSION:
        reason = "unknown scoring contract"
    elif any(e.get("outcome") not in {"win", "lose"} or e.get("error") or
             not e.get("seed_applied") or not e.get("complete") or
             not e.get("mower_coverage") or e.get("model_errors", 0) or e.get("invalid_actions", 0)
             for e in left + right):
        reason = "incomplete, unseeded or failed episode"
    elif [(e.get("level"), e.get("seed")) for e in left] != [(e.get("level"), e.get("seed")) for e in right]:
        reason = "unpaired levels/seeds"
    if reason:
        return {"passed": False, "stage": stage, "reason": reason}
    try:
        for episode in left + right:
            expected = episode_score(episode["outcome"], episode["progress"],
                                     episode["elapsed_game_s"], episode["mower_times"])
            if not math.isclose(expected, episode["score"], abs_tol=1e-9):
                raise ValueError("score mismatch")
    except (KeyError, TypeError, ValueError):
        return {"passed": False, "stage": stage, "reason": "score does not match game facts"}
    # Existing development/confirmation/audit statistical rules; scores are in [0,1].
    return code_compare(base, candidate, stage)
