"""RSI loop: evaluate -> coach -> candidate -> same seeds -> promote or not -> repeat.

    uv run python -m rsi.loop --rounds 3

Run on the server while the pvz server is up. Per round:
  1. play the current playbook on the dev seeds (fresh each round, so both sides share conditions)
  2. the coach reads the dev results (with ground truth) and proposes one change
  3. the fixed rules validate it; it becomes an immutable new version
  4. the candidate plays the same dev seeds
  5. gate: promote only if the mean improves by >= MIN_GAIN and it wins on all but at most one seed
  6. rollback: if the current version scores below half of the best mean seen, return to the best
Held-out seeds are played before round 1 and every --heldout-every rounds (and after the last);
the coach never sees those results. Every step is appended to data/ledger.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from rsi.coach import diff, propose
from rsi.evaluate import aggregate_for_coach, evaluate
from server.config import Settings, load_settings
from server.playbook import PlaybookStore

log = logging.getLogger("pvz.rsi")

MIN_GAIN = 0.02  # mean score improvement required on the dev seeds
WIN_EPS = 0.0  # a seed counts as a win when the candidate scores strictly higher

Run = Callable[[str, list[int]], Awaitable[dict[str, Any]]]


def gate(base: dict, cand: dict) -> tuple[bool, str, int, float]:
    seeds = base["seeds"]
    wins = sum(cand["scores"][s] > base["scores"][s] + WIN_EPS for s in seeds)
    gain = round(cand["mean"] - base["mean"], 4)
    need = len(seeds) - 1
    if gain >= MIN_GAIN and wins >= need:
        return True, f"mean +{gain} and better on {wins}/{len(seeds)} seeds", wins, gain
    return False, f"mean {gain:+} and better on {wins}/{len(seeds)} seeds (need >= +{MIN_GAIN} and >= {need})", wins, gain


async def one_round(rnd: int, s: Settings, store: PlaybookStore, run: Run, dev: list[int],
                    best: dict[str, Any]) -> None:
    current = store.load()
    base = await run(current.version, dev)
    log.info("round %d: %s dev mean %.4f %s", rnd, current.version, base["mean"], base["scores"])
    if base["mean"] > best["mean"]:
        best.update(version=current.version, mean=base["mean"])
    elif base["mean"] < 0.5 * best["mean"]:
        store.set_current(best["version"])
        store.log(kind="rollback", round=rnd, from_version=current.version, to=best["version"],
                  mean=base["mean"], best_mean=best["mean"])
        log.info("round %d: rollback %s -> %s", rnd, current.version, best["version"])
        return

    proposal, error = await propose(current, aggregate_for_coach(base), store.ledger(), s)
    if proposal is None:
        store.log(kind="coach_failed", round=rnd, parent=current.version, error=error)
        log.info("round %d: no candidate (%s)", rnd, error)
        return
    changes = diff(current, proposal)
    if not changes:
        store.log(kind="coach_no_change", round=rnd, parent=current.version, rationale=proposal.rationale)
        log.info("round %d: coach proposed no change", rnd)
        return

    candidate = proposal.apply(current, store.next_version())
    store.write(candidate)
    cand = await run(candidate.version, dev)
    promoted, reason, wins, gain = gate(base, cand)
    if promoted:
        store.set_current(candidate.version)
        if cand["mean"] > best["mean"]:
            best.update(version=candidate.version, mean=cand["mean"])
    store.log(kind="candidate", round=rnd, parent=current.version, candidate=candidate.version,
              changes=changes, rationale=proposal.rationale, parent_mean=base["mean"],
              candidate_mean=cand["mean"], parent_scores=base["scores"], candidate_scores=cand["scores"],
              wins=wins, gain=gain, promoted=promoted, reason=reason)
    log.info("round %d: %s %s -> %s: %s | changes %s", rnd, "PROMOTED" if promoted else "rejected",
             current.version, candidate.version, reason, changes)


async def heldout_check(rnd: int, store: PlaybookStore, run: Run, seeds: list[int]) -> None:
    version = store.current()
    ho = await run(version, seeds)
    store.log(kind="heldout", round=rnd, version=version, mean=ho["mean"], scores=ho["scores"])
    log.info("round %d: held-out %s mean %.4f %s", rnd, version, ho["mean"], ho["scores"])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--dev-seeds", default="1,2,3,4,5,6")
    parser.add_argument("--heldout-seeds", default="101,102,103,104")
    parser.add_argument("--heldout-every", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--game", choices=["suns", "lawn"], default="suns")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "pvz.client", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    s = load_settings()
    store = PlaybookStore(s.data_dir)
    url = f"ws://127.0.0.1:{s.port}/ws"
    dev = [int(x) for x in args.dev_seeds.split(",")]
    heldout = [int(x) for x in args.heldout_seeds.split(",")]

    async def run(version: str, seeds: list[int]) -> dict[str, Any]:
        return await evaluate(version, seeds, url=url, token=s.token, runs_dir=s.runs_dir,
                              seconds=args.seconds, game=args.game)

    best: dict[str, Any] = {"version": store.current(), "mean": float("-inf")}
    await heldout_check(0, store, run, heldout)  # transfer baseline for the final held-out score
    for rnd in range(1, args.rounds + 1):
        await one_round(rnd, s, store, run, dev, best)
        if rnd % args.heldout_every == 0 or rnd == args.rounds:
            await heldout_check(rnd, store, run, heldout)
    log.info("done; current playbook %s", store.current())


if __name__ == "__main__":
    asyncio.run(main())
