"""One bounded Linux-server episode with the real Qwen/Jev application.

Starts an isolated loopback app and a pixel-only FakeLawn client, copies the
current live playbook, and saves evidence. Never changes/restarts the live app.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import replace
import json
import logging
from pathlib import Path
import re
import socket
import statistics
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def summarize(events: list[dict]) -> dict:
    groups = {kind: [e for e in events if e["kind"] == kind] for kind in ("scene", "plants", "plan", "jev", "exec", "episode")}
    def latency(rows, field):
        values = sorted(e[field] for e in rows if isinstance(e.get(field), (float, int)))
        return {"calls": len(values), "median_ms": round(statistics.median(values) * 1000, 1),
                "p95_ms": round(values[int((len(values)-1)*0.95)] * 1000, 1)} if values else None
    return {
        "counts": dict(Counter(e["kind"] for e in events)),
        "scenes_ok": sum(e.get("scene") is not None for e in groups["scene"]),
        "plant_reads_ok": sum(e.get("plants") is not None for e in groups["plants"]),
        "plans_ok": sum(e.get("plan") is not None for e in groups["plan"]),
        "jev_plant_choices": sum(e.get("placement") is not None and not e.get("rejected") for e in groups["jev"]),
        "jev_waits": sum(e.get("placement") is None for e in groups["jev"]),
        "jev_model_decisions": sum(e.get("selector_latency", 0) > 0 for e in groups["jev"]),
        "jev_no_call_waits": sum(e.get("selector_latency", 0) == 0 for e in groups["jev"]),
        "jev_errors": dict(Counter(e.get("selector_error") for e in groups["jev"] if e.get("selector_error"))),
        "rejected_choices": dict(Counter(e.get("rejected") for e in groups["jev"] if e.get("rejected"))),
        "model_errors": {kind: [e["error"] for e in groups[kind] if e.get("error")][:4]
                         for kind in ("scene", "plants", "plan")},
        "latency": {"look": latency(groups["scene"], "latency"), "plants": latency(groups["plants"], "latency"),
                    "planner": latency(groups["plan"], "latency"),
                    "jev": latency([e for e in groups["jev"] if e.get("selector_latency", 0) > 0], "selector_latency")},
        "example_plan": next((e["plan"] for e in groups["plan"] if e.get("plan")), None),
        "example_choices": [{k: e.get(k) for k in ("reason", "placement", "budget_spent", "rejected")}
                            for e in groups["jev"] if e.get("placement")][:6],
        "judge": next((e.get("summary") for e in reversed(groups["episode"]) if e.get("event") == "end"), None),
    }


async def run(args) -> Path:
    if sys.platform != "linux":
        raise RuntimeError("Real model tests must run on the configured Linux server")
    import httpx
    import uvicorn
    from client.fake_lawn import LOADOUT
    from client.mock_client import play
    from rules.playbook_rules import Playbook
    from server.app import create_app
    from server.config import load_settings, effective_settings, timing_values

    root = ROOT / "data/app_smoke" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    write(root.parent / "LATEST_RUN.json", {"path": str(root)})
    base = load_settings(args.settings_env)
    if base.planner != "qwen" or base.decider != "onejev":
        raise ValueError("Live settings must select actual qwen and onejev models, not mock/rules")
    version = (args.live_data / "CURRENT").read_text().strip()
    if not re.fullmatch(r"v\d{3,}", version):
        raise ValueError("invalid live playbook version")
    policy = Playbook.model_validate_json((args.live_data / "playbooks" / f"{version}.json").read_text())
    base = effective_settings(base, policy)
    (root / "data/playbooks").mkdir(parents=True)
    (root / "data/playbooks" / f"{version}.json").write_text(policy.model_dump_json(indent=2), encoding="utf-8")
    (root / "data/CURRENT").write_text(version + "\n")
    settings = replace(base, host="127.0.0.1", data_dir=root / "data", runs_dir=root / "runs",
                       loadout=list(LOADOUT), frame_log_interval=5, max_run_bytes=64 * 1024**2)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    metadata = {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "domain": "fake-lawn-live-models", "seed": args.seed, "wall_limit_seconds": args.seconds,
                "playbook": version, "planner": base.qwen_model, "jev": base.onejev_model,
                "jev_timeout": base.decider_timeout, "planner_interval": base.plan_interval,
                "look_interval": base.look_interval,
                "effective_timing": timing_values(base),
                "output": str(root)}
    write(root / "metadata.json", metadata)
    print(json.dumps(metadata), flush=True)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    client_task = None
    try:
        for _ in range(100):
            if task.done():
                await task
                raise RuntimeError("server exited before startup")
            if server.started:
                break
            await asyncio.sleep(0.1)
        else:
            raise TimeoutError("isolated server startup timed out")
        async with httpx.AsyncClient(timeout=5) as http:
            response = await http.get(f"http://127.0.0.1:{port}/health")
            response.raise_for_status()
            health = response.json()
        if health.get("control_contract") != "advisory-strategy-auto-suns-v4":
            raise RuntimeError("unexpected application contract")
        write(root / "health.json", health)
        client_task = asyncio.create_task(play(f"ws://127.0.0.1:{port}/ws", settings.token,
            args.seconds, args.seed, fps=20, client_id="live-model-smoke", game="lawn"))
        started = time.monotonic()
        while not client_task.done():
            await asyncio.wait({client_task}, timeout=10)
            paths = list((root / "runs").glob("*/events.jsonl"))
            stats = summarize(read_events(paths[0])) if paths else {}
            progress = {"elapsed_seconds": round(time.monotonic()-started),
                        **{k: stats.get(k) for k in ("scenes_ok", "plans_ok", "jev_plant_choices", "jev_waits", "jev_errors")}}
            write(root / "progress.json", progress)
            print(json.dumps(progress), flush=True)
        result = await client_task
        await asyncio.sleep(0.5)
        events = read_events(root / "runs" / result["session"] / "events.jsonl")
        report = {"metadata": metadata, "game": result, "application": summarize(events)}
        write(root / "report.json", report)
        print(json.dumps(report), flush=True)
    except BaseException as exc:
        write(root / "error.json", {"error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if client_task and not client_task.done():
            client_task.cancel()
            await asyncio.gather(client_task, return_exceptions=True)
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        listener.close()
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings-env", type=Path, required=True)
    parser.add_argument("--live-data", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=240)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    if not 30 <= args.seconds <= 600:
        parser.error("seconds must be 30..600")
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run(args))
