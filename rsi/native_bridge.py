"""JSON-lines research RPC over the existing authenticated SSH connection.

Runs on Linux; never controls a game. State inputs are confined to this research
process and are not passed to live Windows sessions. Credentials stay on server.
"""
import asyncio
import base64
import json
import logging
import sys
import time

from common.file_lock import file_lock
from rsi.coach import propose, diff
from rsi.code_store import write_json
from rsi.native_jobs import NativeJobs, TERMINAL, digest
from rsi.review_live import build_evidence, read_events, select_session
from server.action_selector import DecisionContext, OneJevSelector, candidates
from server.config import load_settings
from server.planner import Plan, QwenPlanner
from server.scene import Scene, QwenLooker
from server.threats import observed_threats
from rsi.native_engineer import NativeEngineer
from rsi.native_player import PlayerRuntime, player_input
from rsi.native_schedule import DecisionSchedule
from rules.playbook_rules import Playbook


class Bridge:
    def __init__(self, settings):
        self.settings = settings
        self.jobs = NativeJobs(settings)
        self.planner = QwenPlanner(settings.qwen_url, settings.qwen_model, False, True, settings.planner_timeout)
        self.selector = OneJevSelector(settings.onejev_url, settings.onejev_model, settings.decider_timeout)
        self.looker = QwenLooker(settings.qwen_url, settings.qwen_model, settings.planner_timeout)
        self.players = {}

    async def close(self):
        for player in self.players.values():
            player.close()
        self.players.clear()
        for model in (self.planner, self.selector, self.looker):
            await model.client.aclose()

    async def handle(self, request):
        op = request["op"]
        if op == "enqueue":
            source = select_session(self.settings.runs_dir, request["session"])
            return self.jobs.enqueue(source.parent.name,retry=bool(request.get("retry")))
        if op == "next":
            job = self.jobs.next_job()
            if job is None:
                return None
            self.jobs.verify_versions(job)
            return job
        job = self.jobs.load(request["job_id"])
        if op == "release_episode":
            key = (request["request_digest"], request["episode_id"])
            player = self.players.pop(key, None)
            if player:
                player.close()
            return None
        if op == "control":
            action = request["action"]
            if action == "stop":
                job.update(status="stopped", reason="operator stopped this round")
            elif action == "pause":
                if job["status"] not in TERMINAL:
                    job.update(paused=True)
            elif action == "resume":
                job.update(paused=False)
            else:
                raise ValueError("unknown control")
            self.jobs.save(job)
            return job
        if job["status"] in TERMINAL:
            return job
        self.jobs.verify_versions(job)
        if job.get("paused"):
            raise RuntimeError("learning explicitly paused")
        if op == "prepare":
            if job["step"] == 2 and job["candidate"] is None:
                if job.get("evolution") == "code":
                    evidence = build_evidence(read_events(select_session(self.settings.runs_dir, job["source_session"]))) if not job.get("engineering") else None
                    await NativeEngineer(self.jobs).advance(job,evidence)
                else:
                    await self.coach(job)
            return job
        if op == "test_error":
            if request["request_digest"] != digest(self.jobs.request(job)):
                raise ValueError("stale Engineer test failure")
            return NativeEngineer(self.jobs).feedback(job,error=request["error"])
        if op == "request":
            return self.jobs.request(job)
        if op == "report":
            return self.jobs.accept(job, request["report"])
        if op == "status":
            if request["status"] not in {"waiting_runtime", "evaluating", "blocked"}:
                raise ValueError("invalid worker status")
            job.update(status=request["status"], detail=str(request.get("detail", ""))[:500])
            self.jobs.save(job)
            return job
        if op == "decide":
            return await self.decide(job, request)
        raise ValueError("unknown operation")

    async def coach(self, job):
        book = self.jobs.store.load(job["parent"])
        base = json.loads(self.jobs.report_path(job, 0).read_text())
        vision = json.loads(self.jobs.report_path(job, 1).read_text())
        evidence = build_evidence(read_events(select_session(self.settings.runs_dir, job["source_session"])))
        def bounded(report):
            return {"environment":report["environment"], "mean":report["mean"],
                    "throughput":report.get("throughput", {}),
                    "episodes":[{k:v for k,v in ep.items() if k not in {"seed","trace","milestones"}} for ep in report["episodes"]],
                    "sample_traces":[ep.get("milestones",[])[:2] +
                                     [p for p in ep.get("milestones",[]) if p.get("mower_triggered_rows")][:2] +
                                     ep["trace"][-2:] for ep in report["episodes"][::2]]}
        development = {"game": "native_pvz", "real_session": evidence,
                       "state_development": bounded(base), "pixels_development": bounded(vision)}
        history = []
        for path in sorted(self.jobs.root.glob("*/job.json")):
            prior = self.jobs.load(path.parent.name)
            if prior["id"] != job["id"] and prior.get("candidate"):
                history.append({"rationale":prior.get("rationale"), "changes":prior.get("changes"),
                                "development_verdict":[v for v in prior.get("verdicts",[]) if v["stage"] == "development"]})
        development["prior_native_candidates_development_only"] = history[-8:]
        proposal, error = await propose(book, development, self.jobs.store.ledger(), self.settings, strategy_only=True)
        if proposal is None or not diff(book, proposal):
            job.update(status="failed" if error else "rejected", reason=error or "Coach made no change")
            self.jobs.save(job)
            return
        self.jobs.verify_versions(job)
        with file_lock(self.jobs.store.current_file.with_suffix(".lock")):
            if self.jobs.store.current() != job["parent"]:
                raise ValueError("CURRENT changed during coaching")
            candidate = proposal.apply(book, self.jobs.store.next_version())
            self.jobs.store.write(candidate)
        write_json(self.jobs.folder(job["id"]) / "proposal.json", proposal.model_dump())
        job.update(candidate=candidate.version, candidate_digest=digest(candidate.model_dump()),
                   status="evaluating", changes=diff(book, proposal), rationale=proposal.rationale)
        self.jobs.save(job)

    async def decide(self, job, request):
        task = self.jobs.request(job)
        if request["request_digest"] != digest(task):
            raise ValueError("stale decision request")
        book = Playbook.model_validate(task["book"])
        tick = request["tick"]
        if type(tick) is not int or not 0 <= tick <= 120000:
            raise ValueError("invalid training clock")
        timings = {}
        jpeg = None
        if task["mode"] == "state":
            scene = Scene.model_validate(request["scene"])
            plants = request["plants"]
            allowed = {tuple(p) for p in request["allowed_positions"]}
        else:
            # Pixel condition never receives state fields or native legality masks.
            if any(k in request for k in ("scene", "plants", "allowed_positions", "raw_observation")):
                raise ValueError("state leaked into the pixel arm")
            import cv2
            import numpy as np
            jpeg = base64.b64decode(request["jpeg"], validate=True)
            if len(jpeg) > 2_000_000:
                raise ValueError("oversized image")
            image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape[:2] != (600, 800):
                raise ValueError("pixel test requires calibrated 800x600 capture")
            loadout = request["loadout"]  # public pre-game seed choice, not dynamic card state
            resource = await self.looker.look_resources(image, tick, loadout, tick/100)
            zombies = await self.looker.look_zombies(image, tick, loadout, tick/100)
            layout = await self.looker.look_plants(image, loadout)
            if resource.scene is None or zombies.scene is None or layout.plants is None:
                raise RuntimeError("pixel observation failed")
            scene = resource.scene.model_copy(update={"zombies": zombies.scene.zombies, "nearest_col": zombies.scene.nearest_col})
            plants = [{"row":r,"col":c,"plants":ps} for (r,c),ps in layout.plants.items()]
            allowed = None
            timings["vision_s"] = resource.latency + zombies.latency + layout.latency
        planted = {(p["row"], p["col"]): p["plants"] for p in plants}
        plan = Plan.model_validate(request["plan"]) if request.get("plan") else None
        plan_tick = request.get("plan_tick", -100000)
        spent = request.get("spent", 0)
        derived = {"planner_context":{},"jev_context":{},"request_replan":False}
        if book.player_code:
            # A new opaque episode ID is generated by Windows even when reconnecting.
            episode = request.get("episode_id")
            if not isinstance(episode,str) or len(episode)>80:
                raise ValueError("missing episode identity for code memory")
            key = (digest(task),episode)
            if key not in self.players:
                if len(self.players) >= 16:
                    raise RuntimeError("episode runtime limit reached; release finished episodes")
                self.players[key] = PlayerRuntime(self.jobs.code.source(book.player_code))
            code_started = time.monotonic()
            derived = await self.players[key].step(player_input(scene,plants,plan,spent,tick,jpeg))
            timings['system0_s'] = time.monotonic()-code_started
        schedule = DecisionSchedule.model_validate(derived['jev_context'].get('runtime_schedule', request.get('schedule', {})))
        events = request.get('decision_reasons', []) + request.get('pending_replan_events', [])
        event_replan = (book.timing.danger_replan and any(e in {'new_threat','threat_advance','plant_lost'} for e in events)
                        and tick-plan_tick >= book.timing.danger_min_seconds*100)
        plan_interval = book.timing.planner_seconds or schedule.planner_seconds
        if plan is None or event_replan or derived["request_replan"] or tick-plan_tick >= min(plan_interval, plan.valid_for_seconds) * 100:
            result = await self.planner.decide(scene, book.planner.knowledge, context={
                "plants": plants, "previous_guidance_for_reference_only": plan.model_dump() if plan else None,
                "committed_spend_under_previous_guidance": spent,
                "observed_threats": observed_threats(scene, planted, True),
                "evaluation_input": task["mode"], "plant_layout_fresh": True,
                "target_plan_interval_game_s":plan_interval,
                "system0_derived_context":derived["planner_context"]})
            if result.plan is None:
                raise RuntimeError("Planner failed: " + str(result.error))
            plan, plan_tick, spent = result.plan, tick, 0
            timings["planner_s"] = result.latency
        ctx = DecisionContext(scene, planted, set(), plan, directive_spent=spent,
                              strategy_knowledge=book.planner.knowledge, allowed_positions=allowed,
                              tool_context=derived["jev_context"])
        choice = await self.selector.select(ctx, book.jev, mode=schedule.selection)
        if choice.error:
            raise RuntimeError("Jev failed: " + str(choice.error))
        timings["jev_s"] = choice.latency
        timings["jev_requests"] = len(choice.scores)
        placement = choice.placement
        if placement is not None:
            from rules.hard_rules import card_cost
            spent += card_cost(scene.cards[placement.slot])
        return {"placement": placement.model_dump() if placement else None, "plan": plan.model_dump(),
                "plan_tick": plan_tick, "spent": spent, "timings": timings,
                "scene": scene.model_dump(), "plants": plants, "system0":derived,
                "schedule":{**schedule.model_dump(), 'planner_seconds':plan_interval}}


async def main():
    settings = load_settings()
    if settings.planner != "qwen" or settings.decider != "onejev":
        raise RuntimeError("Native research requires the production Qwen/OneJev roles")
    bridge = Bridge(settings)
    try:
        with file_lock(bridge.jobs.root / "WORKER.lock", blocking=False):
            await serve(bridge)
    finally:
        await bridge.close()


async def serve(bridge):
    """One authenticated owner; independent episode decisions may overlap."""
    pending = set()
    async def reply(request):
        try:
            result = await bridge.handle(request)
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:1000]}
        response['rpc_id'] = request.get('rpc_id')
        print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            if len(line) > 4_000_000:
                raise ValueError('oversized research request')
            request = json.loads(line)
            if request.get('op') == 'decide':
                if len(pending) >= 8:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                pending.add(asyncio.create_task(reply(request)))
            else:
                # Mutating queue/gate operations never overlap in-flight decisions.
                if pending:
                    await asyncio.gather(*pending)
                    pending.clear()
                await reply(request)
    finally:
        if pending:
            await asyncio.gather(*pending)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    asyncio.run(main())
