"""Autonomous code research on PvZ-Emulator, independent of the real-game service.

Server: python -m rsi.code_loop --rounds 3
Local engine check: python -m rsi.code_loop --baseline-only
PAUSE/STOP files inside the printed run directory explicitly control this loop.
Only simulation CURRENT is changed. Real-game promotion requires real-screen tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
import uuid
from pathlib import Path

from rsi.code_coach import CodeProposal, propose_code
from rsi.code_evaluate import Interrupted, control, evaluate
from rsi.code_store import CodeStore, write_json
from rules.code_gate import MIN_PAIRS, compare

ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger("pvz.code_rsi")


def stage_seeds(rng: random.Random, used: set[int], count: int) -> list[int]:
    result = []
    while len(result) < count:
        value = rng.randrange(1, 2**30)
        if value not in used:
            used.add(value)
            result.append(value)
    return result


async def run(args) -> None:
    from rsi.code_runtime import validate_source
    from rsi.pixel_tools import HELPERS, HELP_TEXT
    from sim.render import CONTRACT

    store = CodeStore(args.data_dir)
    lock = store.root / "RUNNING.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f"another loop may be running; inspect {lock} before removing a stale lock")
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    current_run = store.root / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    try:
        current_run.mkdir(parents=True, exist_ok=False)
    except BaseException:
        lock.unlink(missing_ok=True)
        raise
    status = current_run / "status.json"
    counts = {"coach_responses": 0, "evaluated_candidates": 0, "promotions": 0, "failed_rounds": 0}
    LOG.info("run directory: %s", current_run)

    def update(**values):
        write_json(status, {"run": str(current_run), "updated": time.time(), **counts, **values})

    def play(version: str, seeds: list[int], label: str) -> dict:
        control(current_run)
        store.source(version)  # verify content address before executing
        update(state="evaluating", stage=label, version=version, episodes=len(seeds))
        LOG.info("%s: %s on %d episodes", label, version[:12], len(seeds))
        report = evaluate(store.candidates / f"{version}.py", seeds, current_run / f"{label}.json", controls=current_run)
        LOG.info("%s: mean %.4f, wins %d/%d", label, report["mean"],
                 sum(e["outcome"] == "win" for e in report["episodes"]), len(seeds))
        return report

    try:
        write_json(store.root / "LATEST_RUN.json", {"path": str(current_run), "pid": os.getpid()})
        seed_source = (ROOT / "playbook_seed" / "sim_player.py").read_text(encoding="utf-8")
        validate_source(seed_source, helper_names=HELPERS)
        initial = store.initialize(seed_source)
        # The random seed is persisted for reproducibility, never passed to coach or player.
        experiment_seed = random.SystemRandom().randrange(2**63)
        rng = random.Random(experiment_seed)
        used = set()
        write_json(current_run / "experiment.json", {"seed": experiment_seed, "initial": initial,
                   "domain": "pvzemu-symbolic-v1", "baseline_only": args.baseline_only,
                   "coach_mode": "external" if getattr(args, "candidate_json", None) else "internal_qwen"})
        if args.baseline_only:
            play(initial, stage_seeds(rng, used, MIN_PAIRS["development"]), "baseline")
            update(state="baseline_complete", current=initial, learning_started=False)
            return
        supplied = getattr(args, "candidate_json", None)
        if supplied and args.rounds != 1:
            raise ValueError("a supplied candidate is evaluated once; use --rounds 1")
        if not supplied and sys.platform != "linux":
            raise RuntimeError("Run learning on the configured Linux server; --baseline-only is supported on Windows")
        settings = None
        if not supplied:
            from server.config import load_settings
            settings = load_settings(getattr(args, "settings_env", None))
        for rnd in range(1, args.rounds + 1):
            control(current_run)
            current = store.current()
            seeds = stage_seeds(rng, used, MIN_PAIRS["development"])
            base = play(current, seeds, f"round{rnd}-development-base")
            update(state="coaching", round=rnd, current=current)
            phase = "development"
            try:
                if supplied:
                    proposal = CodeProposal.model_validate_json(supplied.read_text(encoding="utf-8"))
                    model = {"source": "supplied_candidate", "path": str(supplied.resolve()),
                             "origin": args.candidate_origin, "model_calls": 0}
                else:
                    proposal, model = await propose_code(store.source(current), base, store.coach_history(), settings,
                        controls=current_run, renderer_contract=CONTRACT, pixel_helpers=HELP_TEXT)
                    counts["coach_responses"] += 1
                write_json(current_run / f"round{rnd}-proposal.json", {**proposal.model_dump(), "model": model})
                validate_source(proposal.source, helper_names=HELPERS)
                version = store.add(proposal.source, parent=current, rationale=proposal.rationale,
                                    origin=args.candidate_origin if supplied else "qwen")
                store.log(kind="proposal", round=rnd, version=version, parent=current,
                          diagnosis=proposal.diagnosis, rationale=proposal.rationale, model=model)
                if version == current:
                    counts["failed_rounds"] += 1
                    store.log(kind="invalid", round=rnd, version=version, error="no code change")
                    continue
                candidate = play(version, seeds, f"round{rnd}-development-candidate")
                counts["evaluated_candidates"] += 1
                verdicts = [compare(base, candidate, "development")]
                store.log(kind="development", round=rnd, version=version, development=verdicts[-1])
                for stage in ("confirmation", "audit"):
                    if not verdicts[-1]["passed"]:
                        break
                    phase = stage
                    fresh = stage_seeds(rng, used, MIN_PAIRS[stage])
                    old = play(current, fresh, f"round{rnd}-{stage}-base")
                    new = play(version, fresh, f"round{rnd}-{stage}-candidate")
                    verdicts.append(compare(old, new, stage))
                    store.log(kind=stage, round=rnd, version=version, verdict=verdicts[-1])
                if len(verdicts) == 3 and all(v["passed"] for v in verdicts):
                    control(current_run)
                    store.promote(version, current, verdicts)
                    counts["promotions"] += 1
                    LOG.info("PROMOTED %s -> %s (simulation only)", current[:12], version[:12])
                else:
                    store.log(kind="rejected", round=rnd, version=version, verdicts=verdicts)
                    LOG.info("rejected %s: %s", version[:12], verdicts[-1]["reason"])
            except Interrupted:
                raise
            except Exception as exc:
                counts["failed_rounds"] += 1
                # Even exception messages may contain unseen scene data. Keep hidden
                # evaluation failures out of future coach prompts, not just scores.
                store.log(kind="invalid" if phase == "development" else "heldout_error",
                          round=rnd, phase=phase, error=f"{type(exc).__name__}: {exc}")
                LOG.exception("round %d failed; incumbent retained", rnd)
        final_state = "complete"
        if counts["failed_rounds"]:
            final_state = "complete_with_errors" if counts["evaluated_candidates"] else "failed"
        update(state=final_state, current=store.current(), initial=initial, rounds=args.rounds,
               autonomous=not bool(supplied))
    except Interrupted as exc:
        update(state="stopped", reason=str(exc), current=store.current())
        LOG.info("%s", exc)
    except BaseException as exc:
        update(state="failed", reason=f"{type(exc).__name__}: {exc}", current=store.current())
        raise
    finally:
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "code_rsi")
    parser.add_argument("--settings-env", type=Path, help="Existing server .env; never copied to a candidate worker")
    parser.add_argument("--candidate-json", type=Path, help="Evaluate a recorded external coach proposal without model calls")
    parser.add_argument("--candidate-origin", default="external", help="Provenance of an explicitly supplied candidate")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 20:
        parser.error("rounds must be 1..20 (bounded model budget)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
