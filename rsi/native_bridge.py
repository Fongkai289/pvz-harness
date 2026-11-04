"""JSON-lines research RPC over the existing authenticated SSH connection.

Runs on Linux; never controls a game. State inputs are confined to this research
process and are not passed to live Windows sessions. Credentials stay on server.
"""
import asyncio
import base64
import collections
import json
import logging
import os
import sys
import time

from common.file_lock import file_lock
from rsi.coach import propose, diff
from rsi.code_store import write_json
from rsi.native_jobs import NativeJobs, TERMINAL, digest
from rsi.review_live import build_evidence, read_events, select_session
from rules.hard_rules import card_cost
from server.action_gate import PlantAction, parse_actions, parse_menu, split_actions
from server.action_selector import (LESSONS_CONTRACT, DecisionContext, OneJevSelector, Selection, candidates,
                                    reject_actions)
from server.config import load_settings
from server.planner import Placement, Plan, QwenPlanner
from server.scene import Scene, QwenLooker
from server.lessons import active_lessons
from server.threats import observed_threats
from rsi.native_engineer import NativeEngineer
from rsi.native_player import PlayerRuntime, player_input
from rsi.native_schedule import DecisionSchedule
from rules.playbook_rules import Playbook
from rules.replan_rules import event_spacing, refresh_seconds


class Bridge:
    def __init__(self, settings):
        self.settings = settings
        self.jobs = NativeJobs(settings)
        self.planner = QwenPlanner(settings.qwen_url, settings.qwen_model, False, True, settings.planner_timeout)
        self.selector = OneJevSelector(settings.onejev_url, settings.onejev_model, settings.decider_timeout)
        self.looker = QwenLooker(settings.qwen_url, settings.qwen_model, settings.planner_timeout)
        self.players = {}
        self.observed = {}  # (task digest, episode) -> last pixel-condition look, for reflexes
        self.menus = {}  # (task digest, episode) -> latest decision menu, shown to the next Planner call
        self.replans = {}  # (task digest, episode) -> {failed: consecutive unexecutable decisions, reasons: pending System0 request}

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
            self.observed.pop(key, None)
            self.menus.pop(key, None)
            self.replans.pop(key, None)
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
        if op == "reflex":
            return await self.reflex(job, request)
        if op == "choose_seeds":
            return await self.choose_seeds(job, request)
        raise ValueError("unknown operation")

    async def choose_seeds(self, job, request):
        """Training's seed chooser: Qwen picks from the level's public offer as text (live reads the screen).

        A failed call keeps the old fixed order (the first plants offered) and says so in the record."""
        from server.seed_choice import SEED_TYPES, choose_text
        task = self.jobs.request(job)
        if request["request_digest"] != digest(task):
            raise ValueError("stale seed choice request")
        book = Playbook.model_validate(task["book"])
        level, slots = request["level"], request["slots"]
        available, preview = request["available"], request["preview"]
        if (type(level) is not int or type(slots) is not int or not 0 < slots <= 10 or not available
                or len(set(available)) != len(available) or not set(available) <= set(SEED_TYPES)
                or not all(isinstance(z, str) and len(z) <= 40 for z in preview) or len(preview) > 30):
            raise ValueError("invalid seed offer")
        try:
            return await choose_text(self.looker, level, preview, available, slots, book.planner.knowledge)
        except Exception as exc:
            return {"plants": available[:min(slots, len(available))], "zombies": preview, "reason": "",
                    "error": f"{type(exc).__name__}: {exc}"[:300]}

    async def coach(self, job):
        book = self.jobs.store.load(job["parent"])
        base = json.loads(self.jobs.report_path(job, 0).read_text())
        vision = json.loads(self.jobs.report_path(job, 1).read_text())
        evidence = build_evidence(read_events(select_session(self.settings.runs_dir, job["source_session"])))
        def bounded(report):
            return {"environment":report["environment"], "mean":report["mean"],
                    "throughput":report.get("throughput", {}),
                    "episodes":[{k:v for k,v in ep.items() if k not in {"seed","trace","milestones","postgame"}} for ep in report["episodes"]],
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
        if self.jobs.backend != "pvz_portable" and scene.shovel:
            scene = scene.model_copy(update={"shovel": False})  # the pvz_rl executor has no shovel action
        planted = {(p["row"], p["col"]): p["plants"] for p in plants}
        plan = Plan.model_validate(request["plan"]) if request.get("plan") else None
        plan_tick = request.get("plan_tick", -100000)
        spent = request.get("spent", 0)
        derived = {"planner_context":{},"jev_context":{},"request_replan":False}
        key = None
        replan = {"failed": 0, "reasons": [], "fallback": None, "fallback_tick": -100000}
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
            replan = self.replans.setdefault(key, replan)
            status = None if plan is None else {"id": plan_tick, "age_s": round((tick - plan_tick) / 100, 1),
                                                "failed_steps": replan["failed"],
                                                "fallback_reasons": replan["fallback"] or []}
            code_started = time.monotonic()
            derived = await self.players[key].step(player_input(scene,plants,plan,spent,tick,jpeg,plan_status=status))
            timings['system0_s'] = time.monotonic()-code_started
        schedule = DecisionSchedule.model_validate(derived['jev_context'].get('runtime_schedule', request.get('schedule', {})))
        if derived["request_replan"] and book.timing.danger_replan and not replan["reasons"]:
            reasons = derived["planner_context"].get("replan_reasons")
            replan["reasons"] = [str(r)[:200] for r in reasons][:6] if isinstance(reasons, list) and reasons else ["unspecified"]
            if plan is not None and fallback_enabled(book, self.players.get(key)):
                replan["fallback"] = replan["reasons"]
        plan_interval = book.timing.planner_seconds or schedule.planner_seconds
        # Only System0's request or the routine refresh call the Planner, both inside the fixed bounds.
        trigger = ("initial" if plan is None else
                   "system0" if replan["reasons"] and tick - plan_tick >= event_spacing(book.timing.danger_min_seconds) * 100 else
                   "periodic" if tick - plan_tick >= refresh_seconds(plan_interval, plan.valid_for_seconds) * 100 else None)
        replanned = None
        lessons, lesson_errors = active_lessons(book.lessons, scene, planted, scene.sun)
        shown = {"planner": None, "jev": [l["id"] for l in lessons], "errors": lesson_errors}
        if trigger:
            result = await self.planner.decide(scene, book.planner.knowledge, context={
                "plants": plants, "previous_guidance_for_reference_only": plan.model_dump() if plan else None,
                "committed_spend_under_previous_guidance": spent,
                "observed_threats": observed_threats(scene, planted, True),
                "evaluation_input": task["mode"], "plant_layout_fresh": True,
                "target_plan_interval_game_s":plan_interval,
                "replan_reasons": replan["reasons"],
                "system0_derived_context":derived["planner_context"],
                **({"lessons_now": lessons, "lessons_contract": LESSONS_CONTRACT} if lessons else {}),
                **({"decision_menu": self.menus[key]} if key in self.menus else {})})
            shown["planner"] = shown["jev"]
            if result.plan is None:
                raise RuntimeError("Planner failed: " + str(result.error))
            plan, plan_tick, spent = result.plan, tick, 0
            timings["planner_s"] = result.latency
            replanned = {"trigger": trigger, "reasons": replan["reasons"]}
            replan["reasons"], replan["failed"], replan["fallback"] = [], 0, None
        ctx = DecisionContext(scene, planted, set(), plan, directive_spent=spent,
                              strategy_knowledge=book.planner.knowledge, allowed_positions=allowed,
                              tool_context=derived["jev_context"], lessons=lessons)
        player = self.players.get(key) if book.player_code else None
        decision = choice = fallback = None
        if replan["fallback"] and fallback_enabled(book, player):
            choice, fallback = await self.fallback_choice(ctx, player, plan_tick, spent, tick, replan, book, timings)
            if choice is None:  # fallback() failed: Jev takes this step, told the plan no longer fits
                ctx.tool_context = {**ctx.tool_context, "plan_no_longer_fits": replan["fallback"]}
        if choice is None and player is not None and player.has_menu and book.decision.jev_mode == "auto":
            choice, decision = await self.menu_choice(ctx, player, plan, spent, tick, timings)
            self.menus[key] = decision["menu"]
            if decision.get("decision_unexecutable") or decision.get("rejected"):
                replan["failed"] += 1
            elif choice.placement is not None or choice.shovel is not None:
                replan["failed"] = 0
        elif choice is None:
            mode = book.decision.jev_mode if book.decision.jev_mode != "auto" else schedule.selection
            choice = await self.selector.select(ctx, book.jev, mode=mode)
        if choice.error:
            raise RuntimeError("Jev failed: " + str(choice.error))
        timings["jev_s"] = choice.latency
        timings["jev_requests"] = len(choice.scores)
        placement = choice.placement
        if placement is not None:
            spent += card_cost(scene.cards[placement.slot])
        if task["mode"] != "state" and key is not None:
            self.observed[key] = (scene, plants)  # reflexes between pixel decisions reuse this look
        return {"placement": placement.model_dump() if placement else None,
                "shovel": vars(choice.shovel) if choice.shovel else None, "plan": plan.model_dump(),
                "plan_tick": plan_tick, "spent": spent, "timings": timings, "replan": replanned, "fallback": fallback,
                "lessons": shown,
                "scene": scene.model_dump(), "plants": plants, "system0":derived, "decision": decision,
                "reflex_planting": bool(player is not None and "reflex" in player.entries and book.reflex.allow_planting),
                "schedule":{**schedule.model_dump(), 'planner_seconds':plan_interval}}

    async def fallback_choice(self, ctx, player, plan_tick, spent, tick, replan, book, timings):
        """Player code plants in Jev's place while the plan no longer fits; (None, record) hands the step to Jev."""
        record = {"window": plan_tick, "reasons": replan["fallback"], "action": "wait", "placement": None,
                  "rejected": None}
        if tick - replan["fallback_tick"] < book.fallback.min_plant_interval_s * 100:
            return Selection(source="system0_fallback", reason="fallback plant interval"), record
        status = {"id": plan_tick, "age_s": round((tick - plan_tick) / 100, 1), "failed_steps": replan["failed"],
                  "fallback_reasons": replan["fallback"]}
        observation = player_input(ctx.scene, [{"row":r,"col":c,"plants":ps} for (r,c),ps in ctx.planted.items()],
                                   ctx.directive, spent, tick, legal=[(p.slot, p.row, p.col) for p in candidates(ctx)],
                                   sun_available=ctx.scene.sun, plan_status=status)
        started = time.monotonic()
        try:
            actions = parse_actions(await player.step(observation, "fallback"), "fallback", suns=0)
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"[:300]
            return None, record
        finally:
            timings["system0_fallback_s"] = time.monotonic() - started
        choice = Selection(source="system0_fallback", reason="plan no longer fits; player code holds")
        shovel, plant = split_actions(actions)
        if shovel is not None or plant is not None:
            place = None if plant is None else Placement(slot=plant.slot, row=plant.row, col=plant.col)
            record["placement"], record["rejected"] = place and place.model_dump(), reject_actions(ctx, place, shovel)
            record["shovel"] = vars(shovel) if shovel else None
            if record["rejected"] is None:
                choice.placement, choice.shovel = place, shovel
                record["action"] = "shovel+plant" if shovel and place else "shovel" if shovel else "plant"
                replan["fallback_tick"] = tick
        return choice, record

    async def menu_choice(self, ctx, player, plan, spent, tick, timings):
        """Named decision menu: player_code lists, Jev picks, player_code's act() places it."""
        legal = [(p.slot, p.row, p.col) for p in candidates(ctx)]
        if not legal:
            return Selection(reason="no admissible placement; wait"), {"menu": [], "decision": None}
        observation = player_input(ctx.scene, [{"row":r,"col":c,"plants":ps} for (r,c),ps in ctx.planted.items()],
                                   plan, spent, tick, legal=legal, sun_available=ctx.scene.sun)
        started = time.monotonic()
        menu = parse_menu(await player.step(observation, "options"), ctx.scene.rows or 5)
        record = {"menu": [item.public() for item in menu], "decision": None}
        choice = (await self.selector.select_menu(ctx, menu, plan.preferred_decisions) if menu
                  else Selection(reason="empty decision menu; wait"))
        if choice.decision is not None and not choice.error:
            record["decision"] = choice.decision.public()
            actions = parse_actions(await player.step(observation, "act", choice.decision.call_argument()), "act", suns=0)
            record["act"] = [vars(a) for a in actions]
            if not actions:
                record["decision_unexecutable"] = True
            else:
                shovel, plant = split_actions(actions)
                place = None if plant is None else Placement(slot=plant.slot, row=plant.row, col=plant.col)
                record["rejected"] = reject_actions(ctx, place, shovel)
                if record["rejected"] is None:
                    choice.placement, choice.shovel = place, shovel
        timings["system0_menu_s"] = time.monotonic() - started - choice.latency
        return choice, record

    async def reflex(self, job, request):
        """player_code reflex() planting between decisions; research games collect suns themselves."""
        task = self.jobs.request(job)
        if request["request_digest"] != digest(task):
            raise ValueError("stale reflex request")
        book = Playbook.model_validate(task["book"])
        key = (digest(task), request.get("episode_id"))
        player = self.players.get(key)
        if player is None or "reflex" not in player.entries or not book.reflex.allow_planting or not request.get("plan"):
            return {"placement": None, "spent": request.get("spent", 0)}
        if task["mode"] == "state":
            if any(k in request for k in ("jpeg", "raw_observation")):
                raise ValueError("unexpected reflex payload")
            scene, plants = Scene.model_validate(request["scene"]), request["plants"]
            allowed = {tuple(p) for p in request["allowed_positions"]}
        else:
            if any(k in request for k in ("scene", "plants", "allowed_positions", "raw_observation")):
                raise ValueError("state leaked into the pixel arm")
            if key not in self.observed:
                return {"placement": None, "spent": request.get("spent", 0)}
            scene, plants = self.observed[key]
            allowed = None
        plan, spent, tick = Plan.model_validate(request["plan"]), request.get("spent", 0), request["tick"]
        ctx = DecisionContext(scene, {(p["row"], p["col"]): p["plants"] for p in plants}, set(), plan,
                              directive_spent=spent, allowed_positions=allowed)
        observation = player_input(scene, plants, plan, spent, tick,
                                   legal=[(p.slot, p.row, p.col) for p in candidates(ctx)], sun_available=scene.sun)
        actions = parse_actions(await player.step(observation, "reflex"), "reflex", suns=0, allow_planting=True)
        place = next((Placement(slot=a.slot, row=a.row, col=a.col) for a in actions if isinstance(a, PlantAction)), None)
        rejected = None if place is None else ctx.reject(place)
        if place is None or rejected:
            return {"placement": None, "spent": spent, "rejected": rejected}
        return {"placement": place.model_dump(), "spent": spent + card_cost(scene.cards[place.slot])}


def fallback_enabled(book, player):
    return (player is not None and "fallback" in player.entries and book.fallback.allow_planting
            and book.timing.danger_replan)


async def main():
    settings = load_settings()
    if settings.planner != "qwen" or settings.decider != "onejev":
        raise RuntimeError("Native research requires the production Qwen/OneJev roles")
    bridge = Bridge(settings)
    try:
        # A previous bridge whose SSH link died exits within IDLE_EXIT_S; wait for it rather than fail.
        with file_lock(bridge.jobs.root / "WORKER.lock", timeout=IDLE_EXIT_S + 60):
            await serve(bridge)
    finally:
        await bridge.close()


IDLE_EXIT_S = 120  # client silent this long while nothing is running: its SSH link is gone


async def serve(bridge, idle_exit=IDLE_EXIT_S):
    """One authenticated owner; independent episode decisions may overlap.

    The Windows client sends {"op": "ping"} every 30 s (no reply). If the SSH link dies without sshd
    noticing, stdin never closes; the watchdog then exits so a reconnect can take WORKER.lock.
    """
    pending = set()
    activity = {"seen": time.monotonic(), "busy": 0}
    async def reply(request):
        activity["busy"] += 1
        try:
            result = await bridge.handle(request)
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:1000]}
        finally:
            activity["busy"] -= 1
            activity["seen"] = time.monotonic()
        response['rpc_id'] = request.get('rpc_id')
        print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)
    async def watchdog():
        while True:
            await asyncio.sleep(min(5, idle_exit))
            if not activity["busy"] and time.monotonic() - activity["seen"] > idle_exit:
                logging.getLogger(__name__).warning("client silent for %ss; exiting to free WORKER.lock", idle_exit)
                os._exit(0)  # the stdin reader thread would keep a normal shutdown waiting forever
    # Different jobs proceed side by side (parallel Coach attempts); within one job, mutating operations
    # run one at a time, in arrival order, and never overlap that job's in-flight decisions.
    decides = collections.defaultdict(set)
    order = collections.defaultdict(asyncio.Lock)
    async def ordered(request, job):
        async with order[job]:
            if decides[job]:
                await asyncio.gather(*decides[job])
            await reply(request)
    def track(task, group):
        group.add(task)
        task.add_done_callback(group.discard)
    guard = asyncio.create_task(watchdog())
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            activity["seen"] = time.monotonic()
            if len(line) > 4_000_000:
                raise ValueError('oversized research request')
            request = json.loads(line)
            if request.get('op') == 'ping':
                continue
            job = request.get('job_id') or ''
            if request.get('op') == 'decide':
                running = [t for group in decides.values() for t in group]
                if len(running) >= 8:
                    await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                task = asyncio.create_task(reply(request))
                track(task, decides[job])
            else:
                task = asyncio.create_task(ordered(request, job))
            track(task, pending)
    finally:
        guard.cancel()
        if pending:
            await asyncio.gather(*pending)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    asyncio.run(main())
