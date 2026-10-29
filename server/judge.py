"""Judge: the only server component that sees ground-truth state.

Scores episodes from the final ground-truth state. The player side (perception,
policy, planner, decider) must never import this module. Neither the coach nor
the player can change how scores are computed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from common.protocol import EpisodeEvent, StateMsg

WASTED_CLICK_PENALTY = 0.5  # one wasted click costs half a collected sun


def score(final: dict[str, Any] | None) -> float | None:
    """Score from the final ground-truth state; None if the state can't score.

    Lawn game: 0.5 x share of zombies killed, + 0.5 for a win, - 0.1 per lawnmower used.
    Sun game: (collected - 0.5 x wasted clicks) / spawned.
    """
    if final and final.get("game") == "lawn":
        killed_share = final["killed"] / max(final["scheduled"], 1)
        win = 0.5 if final.get("outcome") == "win" else 0.0
        return round(0.5 * killed_share + win - 0.1 * final["mowers_used"], 4)
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
            self.episode = {"level": msg.level, "states_at_start": self.states_seen}
            return None
        final = self.latest_state.data if self.latest_state else None
        summary = {
            **self.episode,
            "level": msg.level or self.episode.get("level"),
            "states_seen": self.states_seen - self.episode.get("states_at_start", 0),
            "final_state": final,
            "score": score(final),
            "client_info": msg.info,
        }
        self.episode = {}
        return summary
