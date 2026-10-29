"""Evaluate generated perception + action code against real pvzemu physics.

The trusted runner owns the engine, scoring and transcripts. Candidate functions
receive pixels, their own observations and JSON memory, never an engine reference.
Each evaluation runs in a fresh subprocess with an external timeout. No model calls.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from rsi.code_store import DOMAIN, digest, write_json

ROOT = Path(__file__).resolve().parent.parent
EPISODE_TICKS = 18000
STEP_TICKS = 100


class Interrupted(RuntimeError):
    pass


def control(root: Path | None) -> None:
    """Explicit pause is never auto-recovered. STOP also interrupts a paused run."""
    if root is None:
        return
    while (root / "PAUSE").exists():
        if (root / "STOP").exists():
            raise Interrupted("stopped by user")
        time.sleep(0.1)
    if (root / "STOP").exists():
        raise Interrupted("stopped by user")


def episode_score(truth: dict) -> float:
    # Horizon survival is a training objective, not an Adventure-level win.
    # A win is clearing all scheduled zombies within the fixed horizon.
    scheduled = max(1, truth["scheduled"])
    kills = min(1.0, max(0.0, truth["killed"] / scheduled))
    survival = min(1.0, max(0.0, truth["tick"] / EPISODE_TICKS))
    return 1.0 if truth["outcome"] == "win" else round(0.2 * kills + 0.1 * survival, 6)


def run_episodes(source: str, seeds: list[int], controls: Path | None = None) -> dict:
    import numpy as np

    from rsi.code_runtime import CandidateProgram
    from rsi.pixel_tools import HELPERS
    from sim.emulator import EmulatorEpisode

    episodes = []
    environment = None
    for seed in seeds:
        control(controls)
        # New globals and memory per episode: no information from earlier test seeds.
        player = CandidateProgram(source, helpers=HELPERS)
        game = EmulatorEpisode(seed=seed, ticks=EPISODE_TICKS)
        memory = {}
        trace, durations = [], []
        started = time.monotonic()
        steps = 0
        while not game.done:
            control(controls)
            image = game.frame()
            before = time.perf_counter()
            observation = player.perceive(image)
            action = player.decide(observation, memory)
            durations.append(time.perf_counter() - before)
            if action is not None:
                if not isinstance(action, dict) or set(action) != {"plant", "row", "col"}:
                    raise ValueError("action must be null or {plant, row, col}")
                if (not isinstance(action["plant"], str) or type(action["row"]) is not int
                        or type(action["col"]) is not int or not 0 <= action["row"] < 5
                        or not 0 <= action["col"] < 9):
                    raise ValueError("invalid action coordinates/type")
            result = game.step(action, ticks=STEP_TICKS)
            truth = game.truth()  # Privileged; never passed to either player function.
            if steps % 10 == 0 or action is not None or game.done:
                trace.append({"tick": truth["tick"], "observation": observation, "action": action,
                              "execution": result, "truth": truth})
            steps += 1
        truth = game.truth()
        environment = {"domain": DOMAIN, "scenario": truth.get("environment"), "engine": truth["engine"],
                       "episode_ticks": EPISODE_TICKS, "step_ticks": STEP_TICKS, "score": "win-or-progress-v1"}
        episodes.append({"seed": seed, "score": episode_score(truth), "outcome": truth["outcome"],
                         "truth": truth, "steps": steps, "wall_seconds": time.monotonic() - started,
                         "player_p50_ms": float(np.percentile(durations, 50) * 1000),
                         "player_p95_ms": float(np.percentile(durations, 95) * 1000),
                         "trace": trace[-40:]})
    return {"environment": environment, "source_sha256": digest(source), "episodes": episodes,
            "mean": sum(row["score"] for row in episodes) / len(episodes)}


def evaluate(source_path: Path, seeds: list[int], output: Path, *, controls: Path | None = None,
             timeout: float = 240) -> dict:
    """External wall-time limit also covers top-level code and infinite loops.

The timer excludes explicit user pauses. The subprocess itself checks PAUSE/STOP
between decisions; on STOP the parent terminates it, including a stuck candidate.
"""
    control(controls)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError("evaluation output already exists; use a fresh run path")
    request = output.with_suffix(".request.json")
    write_json(request, {"source": str(source_path.resolve()), "seeds": seeds,
                         "output": str(output.resolve()), "controls": str(controls.resolve()) if controls else None})
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "LD_LIBRARY_PATH"}}
    env.update(PYTHONUTF8="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
    # Keep stdout/stderr on disk: generated programs cannot fill a pipe and deadlock.
    with output.with_suffix(".worker.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "rsi.code_evaluate", "--worker", str(request)],
                                   cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        active = 0.0
        last = time.monotonic()
        try:
            while process.poll() is None:
                time.sleep(0.1)
                now = time.monotonic()
                if controls and (controls / "STOP").exists():
                    raise Interrupted("stopped by user")
                if not controls or not (controls / "PAUSE").exists():
                    active += now - last
                last = now
                if active > timeout:
                    raise TimeoutError("candidate evaluation exceeded wall-time limit")
            control(controls)
            if process.returncode != 0 or not output.exists():
                raise RuntimeError(f"evaluation worker failed (exit {process.returncode}); see {output.with_suffix('.worker.log')}")
            report = json.loads(output.read_text(encoding="utf-8"))
            if report.get("error"):
                raise RuntimeError(report["error"])
            if report.get("source_sha256") != digest(source_path.read_text(encoding="utf-8")):
                raise RuntimeError("worker evaluated a different source version")
            if [e.get("seed") for e in report.get("episodes", [])] != seeds:
                raise RuntimeError("worker returned different evaluation seeds")
            return report
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def worker(request_path: Path) -> None:
    if sys.platform == "linux":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (240, 240))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    try:
        result = run_episodes(Path(request["source"]).read_text(encoding="utf-8"), request["seeds"],
                              Path(request["controls"]) if request["controls"] else None)
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    write_json(Path(request["output"]), result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    worker(parser.parse_args().worker)
