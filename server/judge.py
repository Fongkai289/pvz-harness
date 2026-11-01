"""Judge: the only server component that sees ground-truth state.

Scores episodes from the final ground-truth state. The player side (perception,
policy, planner, decider) must never import this module. Neither the coach nor
the player can change how scores are computed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from common.protocol import EpisodeEvent, StateMsg

WASTED_CLICK_PENALTY = 0.5  # one wasted click costs half a collected sun
MOWER_SCORE_VERSION = "mower-time-v1"


def mower_evaluation(final: dict | None) -> dict | None:
    """A separate criterion when a real-game win/kill score is unavailable.

    Each use costs .1 plus .1*exp(-game_seconds/60). Unknown times are not
    replaced with reconnect time; incomplete runs cannot be used for promotion.
    """
    if not final or "mowers_used" not in final:
        return None
    events = final.get("mower_events", [])
    used = final["mowers_used"]
    known = [e["time_s"] for e in events if isinstance(e.get("time_s"), (float, int))
             and not isinstance(e["time_s"], bool) and math.isfinite(e["time_s"]) and e["time_s"] >= 0]
    unknown = max(0, used - len(known))
    early = sum(.1 * math.exp(-t / 60) for t in known)
    complete = ("mower_events" in final and len(events) == used and not unknown
                and final.get("mower_timing_coverage") == "from_start")
    return dict(version=MOWER_SCORE_VERSION, used=used, known_trigger_times_s=known,
                first_known_trigger_s=min(known) if known else None, unknown_trigger_times=unknown,
                coverage=final.get("mower_timing_coverage", "legacy_no_timing"),
                usage_penalty=round(.1 * used, 6), early_penalty=round(early, 6),
                penalty=round(.1 * used + early, 6), comparable=complete)


def score_version(final):
    if final and final.get("game") == "lawn":
        return "lawn-mower-time-v2" if "mower_events" in final else "lawn-v1"
    return "suns-v1" if final and "spawned" in final else None


def score(final: dict[str, Any] | None) -> float | None:
    """Score from the final ground-truth state; None if the state can't score.

    Lawn game: kill share + win bonus minus mower count/time penalties.
    Sun game: (collected - 0.5 x wasted clicks) / spawned.
    """
    if final and final.get("game") == "lawn":
        killed_share = final["killed"] / max(final["scheduled"], 1)
        win = 0.5 if final.get("outcome") == "win" else 0.0
        mower = mower_evaluation(final)
        if "mower_events" in final and not mower["comparable"]:
            return None
        return round(0.5 * killed_share + win - mower["penalty"], 4)
    if not final or "spawned" not in final:
        return None  # the real game's score (win / waves) comes with state reading
    spawned = max(final["spawned"], 1)
    return round((final["collected"] - WASTED_CLICK_PENALTY * final["wasted_clicks"]) / spawned, 4)


@dataclass
class Judge:
    latest_state: StateMsg | None = None
    states_seen: int = 0
    episode: dict[str, Any] = field(default_factory=dict)

    def on_state(self, msg: StateMsg) -> None:
        self.latest_state = msg
        self.states_seen += 1

    def on_episode(self, msg: EpisodeEvent) -> dict[str, Any] | None:
        """Returns the episode summary when an episode ends."""
        if msg.event == "start":
            self.latest_state = None
            self.episode = {"level": msg.level, "states_at_start": self.states_seen}
            return None
        final = self.latest_state.data if self.latest_state else None
        summary = {
            **self.episode,
            "level": msg.level or self.episode.get("level"),
            "states_seen": self.states_seen - self.episode.get("states_at_start", 0),
            "final_state": final,
            "score": score(final),
            "outcome": final.get("outcome") if final else None,
            "score_version": score_version(final),
            "mower_evaluation": mower_evaluation(final),
            "client_info": msg.info,
        }
        self.episode = {}
        return summary
