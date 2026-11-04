"""Fixed bounds on how often the Planner is called; the coach tunes only inside them.

System0 decides when a plan no longer fits (plan.replan_when, checked in player code); these
limits stop any condition, playbook timing or Planner-chosen validity from shortening the cycle.
"""

MIN_PLAN_SPACING_S = 8.0  # no Planner call starts sooner than this after the previous one
# Routine refreshes come no sooner than this (as in the HoK planner's 30 s goals); System0-reported
# changes still replan after MIN_PLAN_SPACING_S. A 6 s cycle kept a slow shared model busy for no gain.
MIN_PERIODIC_REFRESH_S = 25.0
# A plan outlives its routine refresh plus a slow answer, so Jev is never left without one.
MIN_PLAN_LIFETIME_S = 40.0


def plan_lifetime(valid_for_seconds: float) -> float:
    return max(MIN_PLAN_LIFETIME_S, float(valid_for_seconds))


def refresh_seconds(configured: float, valid_for_seconds: float) -> float:
    """Periodic refresh: the configured cycle, before the plan expires, never under the routine floor."""
    return max(MIN_PERIODIC_REFRESH_S, min(float(configured), plan_lifetime(valid_for_seconds)))


def event_spacing(danger_min_seconds: float) -> float:
    """Minimum gap before a System0-requested replan."""
    return max(MIN_PLAN_SPACING_S, float(danger_min_seconds))
