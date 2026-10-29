"""Play one playbook version on a fixed set of seeds and collect scores + diagnostics.

Runs fake-game episodes against the live server (one session per seed, in parallel),
then reads each session's run log: the judge's score and, for the coach, what every
sun click hit or missed.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from pathlib import Path
from typing import Any

from client.mock_client import play


def _quantiles(xs: list[float]) -> dict[str, float] | None:
    if not xs:
        return None
    xs = sorted(xs)
    pick = lambda q: xs[min(len(xs) - 1, int(q * len(xs)))]
    return {"min": xs[0], "p10": pick(0.1), "median": pick(0.5), "p90": pick(0.9), "max": xs[-1]}


def diagnose(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Join each sun click with its ground-truth outcome; summarise hue / area of hits vs misses."""
    clicks = {e["cmd_id"]: e for e in events if e["kind"] == "cmd" and e.get("reason") == "collect sun"}
    hits, misses, decoys = [], [], []
    for e in events:
        if e["kind"] != "exec" or e["cmd_id"] not in clicks or not e.get("result"):
            continue
        meta = clicks[e["cmd_id"]].get("meta", {})
        (hits if e["result"]["collected"] else decoys if e["result"].get("decoy") else misses).append(meta)
    summarize = lambda group: {
        "count": len(group),
        "hue": _quantiles([m["hue"] for m in group if "hue" in m]),
        "area": _quantiles([m["area"] for m in group if "area" in m]),
    }
    return {"hit_clicks": summarize(hits), "decoy_clicks": summarize(decoys),
            "other_missed_clicks": summarize(misses)}


def read_episode(runs_dir: Path, session_id: str) -> dict[str, Any]:
    events = [json.loads(line) for line in (runs_dir / session_id / "events.jsonl").read_text().splitlines()]
    end = next(e for e in reversed(events) if e["kind"] == "episode" and e["event"] == "end")
    return {"session": session_id, "playbook": end.get("playbook"), "score": end["summary"]["score"],
            "final_state": end["summary"]["final_state"], "diagnostics": diagnose(events)}


async def evaluate(version: str, seeds: list[int], *, url: str, token: str, runs_dir: Path,
                   seconds: float, concurrency: int = 6, game: str = "suns") -> dict[str, Any]:
    gate = asyncio.Semaphore(concurrency)

    async def one(seed: int) -> dict[str, Any]:
        async with gate:
            result = await play(url, token, seconds, seed, playbook=version, client_id=f"eval-s{seed}", game=game)
        await asyncio.sleep(0.5)  # let the server write the episode summary
        episode = read_episode(runs_dir, result["session"])
        if episode["playbook"] != version:
            raise RuntimeError(f"session ran {episode['playbook']}, expected {version}")
        return {"seed": seed, **episode}

    episodes = await asyncio.gather(*(one(s) for s in seeds))
    scores = {ep["seed"]: ep["score"] for ep in episodes}
    return {"version": version, "game": game, "seeds": seeds, "scores": scores,
            "mean": round(statistics.mean(scores.values()), 4), "episodes": episodes}


def aggregate_for_coach(report: dict[str, Any]) -> dict[str, Any]:
    """What the coach reads: totals and click diagnostics pooled over the dev episodes."""
    totals: dict[str, int] = {}
    for ep in report["episodes"]:
        for k, v in (ep["final_state"] or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                totals[k] = totals.get(k, 0) + v
            elif k == "outcome":
                totals[f"outcome_{v}"] = totals.get(f"outcome_{v}", 0) + 1
    pooled: dict[str, list] = {}
    for ep in report["episodes"]:
        for group, d in ep["diagnostics"].items():
            pooled.setdefault(group, []).append(d)
    return {
        "version": report["version"], "game": report.get("game", "suns"), "mean_score": report["mean"],
        "episodes": len(report["episodes"]),
        "totals": totals,
        "clicks": {group: {"count": sum(d["count"] for d in ds),
                           "per_episode_hue": [d["hue"] for d in ds if d["hue"]][:3],
                           "per_episode_area": [d["area"] for d in ds if d["area"]][:3]}
                   for group, ds in pooled.items()},
    }
