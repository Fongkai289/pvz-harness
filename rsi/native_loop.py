"""Windows pvz_rl worker: durable paired evaluation -> server Coach -> fixed gates.

python -m rsi.native_loop --config data/native_rsi/config.json --preflight
python -m rsi.native_loop --config data/native_rsi/config.json --watch
PAUSE/STOP files in work_dir control this worker, independently of game-client buttons.
"""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from common.file_lock import file_lock
from rsi.code_store import write_json
from rsi.native_gate import episode_score, SCORE_VERSION
from rsi.native_jobs import digest, TERMINAL, source_digest
from rsi.native_runtime import NativeArena, player_state, raw_info, preflight, PLANTS
from rsi.native_schedule import DecisionClock, DecisionSchedule, timing_summary
from rsi.postgame import EpisodeRecorder
from rules.replan_rules import refresh_seconds
from rsi.native_transport import JsonRPC, ArenaProcess


class Stopped(RuntimeError):
    pass


def control(root):
    while (root / "PAUSE").exists():
        if (root / "STOP").exists():
            raise Stopped("worker explicitly stopped")
        time.sleep(.2)
    if (root / "STOP").exists():
        raise Stopped("worker explicitly stopped")


HEARTBEAT_S = 30  # well inside the server bridge's IDLE_EXIT_S


class SSHBridge:
    def __init__(self, config, root):
        ssh = config["ssh"]
        args = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                "-i", ssh["key"], "-p", str(ssh["port"])]
        # Re-evaluate Wi-Fi selection on every new connection; do not change OS routing.
        helper = Path(__file__).resolve().parents[1] / "scripts/connect_server.ps1"
        safe_path = str(helper).replace("'", "''")
        network = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                                  f". '{safe_path}'; Get-SshBindAddress"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if network.returncode:
            raise RuntimeError("cannot determine SSH bind address")
        if network.stdout.strip():
            import ipaddress
            address = str(ipaddress.IPv4Address(network.stdout.strip()))
            args += ["-b", address]
        module = {'pvz_portable': 'rsi.portable_bridge', 'codex_coach': 'rsi.codex_queue'}.get(
            config.get('backend'), 'rsi.native_bridge')
        args += [ssh["host"], "cd /srv/pvz-harness && .venv/bin/python -u -m "+module]
        self.log = (root / "ssh.log").open("a", encoding="utf-8")
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                        text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.root = root
        self.rpc = JsonRPC(self.process, lambda: control(self.root))
        # The research bridges and the Coach queue both run native_bridge.serve(), which takes heartbeats.
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _heartbeat(self):
        """One unanswered line every HEARTBEAT_S: a server bridge that stops hearing these has lost us."""
        while self.process.poll() is None and self.rpc.failure is None:
            time.sleep(HEARTBEAT_S)
            try:
                with self.rpc.writer:
                    self.process.stdin.write('{"op": "ping"}\n')
                    self.process.stdin.flush()
            except (OSError, ValueError):
                return

    def call(self, op, **payload):
        return self.rpc.call(op, **payload)

    def release_episode(self, task, episode_id):
        if not (self.root/'STOP').exists():
            self.call('release_episode',job_id=task['job_id'],request_digest=digest(task),episode_id=episode_id)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=10)
        self.rpc.fail(RuntimeError('SSH closed'))
        self.log.close()


KEY_FRAME_TICKS = 2000  # a live key frame at least every 20 game seconds, plus one per planting
MAX_KEY_FRAMES = 60
REFLEX_CALL_TICKS = 100  # research reflex() planting is asked at most once per game second


def run_episode(arena, task, spec, bridge, root, schedule=None, abort=None):
    episode_id = uuid.uuid4().hex
    try:
        return _run_episode(arena, task, spec, bridge, root, episode_id, schedule, abort)
    finally:
        if hasattr(bridge, 'release_episode'):
            bridge.release_episode(task, episode_id)


def _run_episode(arena, task, spec, bridge, root, episode_id, schedule, abort):
    control(root)
    reset_started = time.monotonic()
    seed_choice = None
    if hasattr(arena, "prepare"):
        # Like a player: at the seed chooser the server picks the loadout, then the level starts.
        offer, info = arena.prepare(spec["level"], spec["seed"])
        if offer is not None:
            seed_choice = bridge.call("choose_seeds", job_id=task["job_id"], request_digest=digest(task), **offer)
            info = arena.choose(seed_choice["plants"])
    else:
        info = arena.reset(spec["level"], spec["seed"])
    reset_seconds = time.monotonic()-reset_started
    if info.get("rng_seed_support") != 2 or not info.get("rng_seed_applied"):
        raise RuntimeError("native runtime did not apply the paired RNG seed")
    if info.get("level_id") != spec["level"] or info.get("playthrough") != 1:
        raise RuntimeError("native reset reached a different level/playthrough")
    started = time.monotonic()
    state = {"plan":None, "plan_tick":-100000, "spent":0}
    mowers, trace, milestones = {}, [], []
    initial_clock = int(info["game_clock"])
    public_loadout = None
    clock = DecisionClock(schedule)
    samples, reasons_count = defaultdict(list), Counter()
    samples['environment_reset_s'].append(reset_seconds)
    decisions, jev_requests, simulation_steps = 0, 0, 0
    choice = None
    key_frames = []  # live view only: player-side boards at each planting and every KEY_FRAME_TICKS
    recorder = EpisodeRecorder()  # coach-only engine-state record; never sent to the player
    reflex = (task.get("book") or {}).get("reflex") or {}
    reflex_interval = int(float(reflex.get("min_plant_interval_s", 4)) * 100)
    last_reflex_plant, last_reflex_call, reflex_rejected = -10**9, -10**9, 0
    while True:
        if abort is not None and abort.is_set():
            raise RuntimeError('another evaluation lane failed')
        control(root)
        if time.monotonic()-started > 3600:
            raise TimeoutError("episode wall-time cap exceeded")
        if info.get("observation_quality") != "fresh" or not info.get("replay_eligible"):
            raise RuntimeError("stale/partial native observation cannot be scored")
        raw = raw_info(info)
        tick = int(info["game_clock"]) - initial_clock
        if public_loadout is None:
            if raw.get('backend')=='pvz_portable':
                from rsi.portable_runtime import NAMES
                public_loadout = [NAMES[c['plant_type']] for c in raw['cards']]
            else:
                public_loadout = [PLANTS[c["plant_type"]] for c in sorted(raw["cards"], key=lambda c:c["slot"])]
        mask = list(info['action_mask'])
        public = player_state(raw, mask, tick) if task['mode']=='state' else None
        try:
            truth = public if public is not None else player_state(raw, mask, tick)
        except ValueError:
            truth = None  # the coach record is incomplete; the game itself continues
        recorder.observe(tick, truth)
        reasons = clock.due(tick, public)
        if (not reasons and state['plan'] is not None
                and tick-state['plan_tick'] >= refresh_seconds(clock.schedule.planner_seconds,
                                                               state['plan'].get('valid_for_seconds',60))*100):
            reasons = ['plan_expired']
        placement = shovel = None
        if reasons:
            payload = dict(job_id=task["job_id"], request_digest=digest(task), tick=tick, episode_id=episode_id,
                           decision_reasons=reasons, schedule=clock.schedule.model_dump(), **state)
            if public is not None:
                payload.update(public)
            else:
                payload.update(jpeg=base64.b64encode(arena.pixels()).decode(), loadout=public_loadout)
            call_start = time.monotonic()
            choice = bridge.call("decide", **payload)
            if hasattr(arena,'identity') and arena.identity.get('backend')=='pvz_portable':
                progress_root=root/task['job_id']/'live'
                progress_root.mkdir(parents=True,exist_ok=True)
                write_json(progress_root/(episode_id+'.json'),dict(level=spec['level'],tick=tick,
                    decisions=decisions+1,scene=choice['scene'],plants=choice['plants'],
                    placement=choice['placement'],plan=choice['plan'],timings=choice['timings']))
                if choice['placement'] or not key_frames or tick-key_frames[-1]['tick']>=KEY_FRAME_TICKS:
                    key_frames.append(dict(tick=tick,scene=choice['scene'],plants=choice['plants'],
                                           placement=choice['placement'],plan=(choice.get('plan') or {}).get('objective')))
                    del key_frames[:-MAX_KEY_FRAMES]
                    write_json(progress_root/(episode_id+'-frames.json'),dict(level=spec['level'],frames=key_frames))
            samples['rpc_s'].append(time.monotonic()-call_start)
            for key, value in choice['timings'].items():
                if key.endswith('_s'):
                    samples[key].append(value)
            jev_requests += choice['timings'].get('jev_requests', 0)
            decisions += 1
            reasons_count.update(reasons)
            clock.committed(tick, public, choice.get('schedule'))
            placement, shovel = choice['placement'], choice.get('shovel')
            recorder.observe(tick, truth, force=True)
            if choice['plan_tick'] != state['plan_tick']:
                recorder.plan(tick, choice.get('replan'), (choice.get('lessons') or {}).get('planner'))
            if choice.get('fallback'):
                recorder.fallback(tick, choice['fallback'])
            recorder.decision(tick, choice, placement, source='fallback' if choice.get('fallback') else 'jev')
            state = {key:choice[key] for key in state}
        if (placement is None and choice is not None and choice.get('reflex_planting')
                and tick - last_reflex_plant >= reflex_interval and tick - last_reflex_call >= REFLEX_CALL_TICKS):
            last_reflex_call = tick
            # player_code reflex() planting between decisions (research games collect suns themselves).
            payload = dict(job_id=task["job_id"], request_digest=digest(task), tick=tick, episode_id=episode_id,
                           plan=state['plan'], spent=state['spent'])
            if public is not None:
                payload.update(public)
            answer = bridge.call("reflex", **payload)
            proposed = answer.get('placement')
            if proposed is not None:
                if mask[1 + proposed['slot']*54 + proposed['row']*9 + proposed['col']]:
                    placement, last_reflex_plant, state['spent'] = proposed, tick, answer['spent']
                    recorder.decision(tick, {**choice, 'decision': None}, placement, source='reflex')
                else:
                    reflex_rejected += 1  # not legal in the engine now; never sent
        control(root)  # Pause/stop arriving during model inference must prevent a new action.
        if abort is not None and abort.is_set():
            raise RuntimeError('another evaluation lane failed')
        step_start = time.monotonic()
        if shovel is not None:
            arena.shovel(shovel)  # dig first, so a plant of the same decision can use the cell
        terminal, truncated, info = arena.step(placement)
        samples['environment_step_s'].append(time.monotonic()-step_start)
        simulation_steps += 1
        if truncated:
            raise RuntimeError("truncated native episode is not a full-game result")
        if info.get("observation_quality") != "fresh" or not info.get("replay_eligible"):
            raise RuntimeError("partial/terminal-fallback snapshot cannot pass a gate")
        if info.get("action_result") and not info["action_result"].get("success"):
            raise RuntimeError("native executor rejected a proposed action")
        elapsed = (int(info["game_clock"])-initial_clock)/100
        events = info["events"]
        rows = events.get("mower_triggered_rows", [])
        if int(events.get("mowers_used", 0)) != len(rows):
            raise RuntimeError("mower trigger timing is incomplete")
        for row in rows:
            if row in mowers:
                raise RuntimeError("duplicate mower event")
            recorder.mower(int(info["game_clock"])-initial_clock, row)
            precise=events.get('mower_trigger_ticks',{})
            mowers[row] = float(precise.get(str(row),precise.get(row,elapsed*100)))/100
        point = {"tick":tick, "scene":choice["scene"], "plants":choice["plants"],
                 "plan":choice["plan"], "placement":placement, "shovel":shovel,
                 "timings":choice["timings"] if reasons else {},
                 "mower_triggered_rows":rows, "system0":choice.get("system0"),
                 "decision_reasons":reasons, "decision_observation_tick":clock.last_tick}
        if public is not None:
            point.update(scene=public['scene'], plants=public['plants'])
        if tick < 500 or rows or (tick % 3000 == 0 and len(milestones) < 30):
            milestones.append(point)
        trace.append(point)
        trace = trace[-12:]
        if terminal:
            outcome = {"WON":"win", "LOST":"lose"}.get(info["result"])
            final = raw_info(info)["global_state"]
            total = int(final.get("total_waves", 0))
            if total <= 0:
                raise RuntimeError("native total wave count is missing")
            progress = min(1.0, max(0.0, int(final["current_wave"])/total))
            return {**spec, "outcome":outcome, "score":episode_score(outcome,progress,elapsed,list(mowers.values())),
                    "complete":True, "seed_applied":True, "mower_coverage":True,
                    "mower_times":list(mowers.values()), "elapsed_game_s":elapsed, "progress":progress,
                    "wall_s":time.monotonic()-started, "model_errors":0,"invalid_actions":0,
                    "throughput":dict(decisions=decisions,jev_requests=jev_requests,simulation_steps=simulation_steps,
                                      triggers=dict(reasons_count),timings=timing_summary(samples),
                                      reflex_rejected_by_engine=reflex_rejected),
                    "trace":trace,"milestones":milestones,"postgame":recorder.report(),
                    "seed_choice":seed_choice}


def evaluate_task(config, task, bridge, *, arena_factory=NativeArena):
    root = Path(config["work_dir"])
    workers = config.get('workers', 1)
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('native workers must be an integer from 1 to 4')
    schedule = DecisionSchedule.model_validate(config.get('decision_schedule', {})).model_dump()
    identity = preflight(config) if arena_factory is NativeArena else {"upstream":task["upstream"], "fixture":True}
    identity.update(worker_source=source_digest(), engine_digest=task["engine_digest"],
                    runner='game-clock-events-v2', workers=workers, initial_schedule=schedule)
    suffix = f"-engineer-{task['engineering_test']}" if task.get("engineering_test") else ""
    output = root / task["job_id"] / f"step-{task['step']}{suffix}"
    output.mkdir(parents=True, exist_ok=True)
    expected = digest({"request":task, "runtime":identity})
    saved = output / "identity.json"
    if saved.exists() and json.loads(saved.read_text())["digest"] != expected:
        raise RuntimeError("resume identity changed; do not mix evidence")
    write_json(saved, {"digest":expected})
    finished = output/'report.json'
    if finished.exists():
        return json.loads(finished.read_text(encoding='utf-8'))
    episodes, missing = [None]*len(task['episodes']), []
    for index, spec in enumerate(task['episodes']):
        path = output/f'episode-{index}.json'
        if path.exists():
            episode = json.loads(path.read_text(encoding='utf-8'))
            if (episode['seed'],episode['level']) != (spec['seed'],spec['level']):
                raise ValueError('cached episode mismatch')
            episodes[index] = episode
        else:
            missing.append((index,spec))
    started = time.monotonic()
    mutex, abort = threading.Lock(), threading.Event()
    def lane(worker_id, assignments):
        arena = None
        try:
            for index, spec in assignments:
                if abort.is_set():
                    return
                control(root)
                if arena is None:
                    factory = (ArenaProcess if workers>1 and arena_factory is NativeArena
                               and config.get('backend')!='pvz_portable' else arena_factory)
                    arena = factory({**config,'instance_id':worker_id},task['mode'])
                episode = run_episode(arena,task,spec,bridge,root,schedule,abort)
                if config.get('backend')=='pvz_portable':
                    (output/f'episode-{index}-final.jpg').write_bytes(arena.pixels())
                write_json(output/f'episode-{index}.json',episode)
                with mutex:
                    episodes[index] = episode
                    progress = dict(status='evaluating',job=task['job_id'],step=task['step'],mode=task['mode'],
                                    side=task['side'],completed=sum(e is not None for e in episodes),
                                    total=len(episodes),workers=workers)
                    write_json(root/'status.json',progress)
                    print(json.dumps(progress),flush=True)
        except BaseException:
            abort.set()
            raise
        finally:
            if arena is not None:
                arena.close()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(lane, i, missing[i::workers]) for i in range(workers)]
        failures = [f.exception() for f in futures if f.exception() is not None]
    # Lanes stopped by abort only echo the failure; report the one that actually went wrong.
    if failures:
        raise next((e for e in failures if str(e) != 'another evaluation lane failed'), failures[0])
    elapsed_wall = max(time.monotonic()-started, .000001)
    result = {"request_digest":digest(task), "version":task["version"],
            "environment":{**identity, "backend":config.get('backend','pvz_rl'), "mode":task["mode"], "score_version":SCORE_VERSION,
                           "clock":"turbo; game frozen during inference; no real-time latency claim", "step_ticks":25},
            "throughput":{"workers":workers,"new_episodes":len(missing),"resumed_episodes":len(episodes)-len(missing),
                          "batch_wall_s":elapsed_wall,"measured_new_episodes_per_hour":len(missing)*3600/elapsed_wall,
                          "decisions":sum(e.get('throughput',{}).get('decisions',0) for e in episodes),
                          "jev_requests":sum(e.get('throughput',{}).get('jev_requests',0) for e in episodes)},
            "episodes":episodes, "mean":sum(e["score"] for e in episodes)/len(episodes)}
    write_json(finished,result)
    return json.loads(finished.read_text(encoding='utf-8'))


def run(config, *, watch=False, session=None, retry=False, bridge_factory=SSHBridge, evaluator=evaluate_task):
    root = Path(config["work_dir"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(root / "WORKER.lock", blocking=False):
        bridge = bridge_factory(config, root)
        try:
            if session:
                bridge.call("enqueue", session=session, retry=retry)
            while True:
                control(root)
                job = bridge.call("next")
                if job is None:
                    if not watch:
                        return
                    time.sleep(5)
                    continue
                if job["status"] in TERMINAL:
                    if not watch:
                        return
                    continue
                if job.get("paused"):
                    time.sleep(1)
                    continue
                try:
                    preflight(config)
                except Exception as exc:
                    status = {"status":"waiting_runtime", "job":job["id"], "detail":str(exc)}
                    write_json(root / "status.json", status)
                    bridge.call("status", job_id=job["id"], status="waiting_runtime", detail=str(exc))
                    print(json.dumps(status, ensure_ascii=False), flush=True)
                    return  # no models/games/candidate generation while prerequisites are missing
                job = bridge.call("prepare", job_id=job["id"])
                if job["status"] in TERMINAL:
                    if not watch:
                        return
                    continue
                if job["step"] == 2 and not job.get("candidate") and not (job.get("engineering") or {}).get("pending"):
                    continue  # Next bounded Coach/Engineer turn; no candidate exists to evaluate yet.
                bridge.call("status", job_id=job["id"], status="evaluating", detail="Windows pvz_rl evaluation")
                task = bridge.call("request", job_id=job["id"])
                try:
                    report = evaluator(config, task, bridge)
                except Exception as exc:
                    if task.get("engineering_test") and not isinstance(exc, Stopped):
                        bridge.call("test_error",job_id=job["id"],request_digest=digest(task),error=f"{type(exc).__name__}: {exc}")
                        continue
                    if not isinstance(exc, Stopped):
                        try:
                            bridge.call("status", job_id=job["id"], status="blocked", detail=str(exc))
                        except Exception:
                            pass
                    raise
                result = bridge.call("report", job_id=job["id"], report=report)
                write_json(root / "status.json", result)
                if result["status"] in TERMINAL and not watch:
                    return
        finally:
            bridge.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--session")
    parser.add_argument("--retry",action="store_true",help="create fresh evidence after a stopped/stale/failed round; requires --session")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--control", choices=["pause","resume","stop"])
    args = parser.parse_args()
    if args.retry and not args.session:
        parser.error("--retry requires --session")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    root = Path(config["work_dir"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.control:
        if args.control == "resume":
            (root / "PAUSE").unlink(missing_ok=True)
        else:
            (root / ("PAUSE" if args.control == "pause" else "STOP")).touch()
        return
    try:
        if args.preflight:
            print(json.dumps(preflight(config), ensure_ascii=False))
        else:
            run(config, watch=args.watch, session=args.session,retry=args.retry)
    except Exception as exc:
        write_json(root / "status.json", {"status":"stopped" if isinstance(exc,Stopped) else "blocked", "detail":str(exc)})
        print(f"{type(exc).__name__}: {exc}", flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
