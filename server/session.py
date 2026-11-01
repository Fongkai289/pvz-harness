"""One connected game client: receives frames, runs the layers, sends clicks back.

Concurrent loops run per session:
  look / plants: screenshot -> scene and plant matrix (code + Qwen)
  fast  (every fast_interval s): sun clicks; Jev chooses plant/row/column under current guidance
  sun   (every 0.25 s): independently collect pixel-confirmed suns, serialized with planting
  plan  (every plan_interval s): planner updates objectives, priorities and budgets, never placements
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import WebSocket

from common.protocol import (
    Command,
    EpisodeEvent,
    ExecReport,
    ModelTiming,
    StateMsg,
    dumps,
    parse_client_message,
    unpack_frame,
)
from rules.playbook_rules import Playbook
from server.config import Settings, effective_settings
from rules.hard_rules import PLANT_COSTS
from server.action_selector import ActionSelector, DecisionContext, candidates, blocked_reasons
from server.judge import Judge
from server.perception import Observation, Perception, decode_jpeg
from server.planner import Placement, Planner, Plan, PlanResult
from server.policy import FastPolicy
from server.sun_collector import SunCollector, COLLECT_INTERVAL
from server.runlog import RunLog
from server.scene import QwenLooker, Scene
from server.threats import observed_threats, threat_changes

log = logging.getLogger(__name__)

STALE_FRAME_SECONDS = 2.0  # stop acting when no fresh frame arrived for this long


class Session:
    def __init__(self, ws: WebSocket, settings: Settings, planner: Planner, selector: ActionSelector,
                 playbook: Playbook, session_id: str, looker: QwenLooker | None = None) -> None:
        self.ws = ws
        self.settings = effective_settings(settings, playbook)
        self.planner = planner
        self.selector = selector
        self.looker = looker  # Jev always needs observed state, even with an image-based planner
        self.split_vision = looker is not None and hasattr(looker, "look_resources")
        self.latest_zombies = None
        self.zombies_received = 0.0
        self.playbook = playbook
        self.plan_note = ""
        self.directive: Plan | None = None
        self.directive_received = 0.0
        self.directive_spent = 0
        self.scene_received = 0.0
        self.episode_generation = 0
        self.episode_active = True
        self.latest_scene: Scene | None = None
        # The lawn as last seen (authoritative: plants get eaten without any signal), plus what the
        # agent placed after that snapshot was taken, so it does not plant twice before the next one.
        self.seen_plants: dict[tuple[int, int], list[str]] = {}
        self.seen_plants_frame = -1
        self.plants_received = 0.0
        self.placed_since: list[tuple[int, int, int, str]] = []  # (frame_id, row, col, plant)
        self.used_slots: set[int] = set()  # cards used since the latest scene was taken
        self.sun_spent = 0
        self.session_id = session_id
        self.perception = Perception(playbook.perception)
        self.policy = FastPolicy(playbook.fast)
        self.judge = Judge()  # sees ground truth; nothing on the player side reads from it
        self.runlog = RunLog(
            settings.runs_dir, session_id, self.settings.frame_log_interval, settings.max_run_bytes
        )
        self.latest_obs: Observation | None = None
        self.latest_jpeg: bytes | None = None
        self.latest_recv = 0.0
        self.paused = False
        self._cmd_id = 0
        self._send_lock = asyncio.Lock()
        self._action_lock = asyncio.Lock()
        self.sun_collector = SunCollector()
        self._sun_permit = None
        self._sun_processed_frame = -1
        self._sun_clicks_sent = 0
        self._selected_context: DecisionContext | None = None
        self.telemetry_enabled = False
        self._model_calls = {stage: 0 for stage in ("scene", "zombies", "plants", "plan", "jev")}
        self._model_idle = {}
        self._planner_latency = 0.0
        self._planner_started = None
        self._jev_blocked = {}
        self.completed_outcome = None
        self.threat_rows = []
        self._plan_requested = asyncio.Event()
        self._plan_reasons = {}
        self._active_plan_reasons = []
        self._resource_snapshot = None
        self._planning_context = None
        self.player_runtime = None
        self._code_epoch = None
        if playbook.player_code:
            from rsi.native_player import NativeCodeStore, PlayerRuntime
            self.player_runtime = PlayerRuntime(NativeCodeStore(settings.data_dir).source(playbook.player_code))

    # ---- inbound ----

    async def on_frame(self, data: bytes) -> None:
        header, jpeg = unpack_frame(data)
        obs = self.perception.process(header, decode_jpeg(jpeg))
        self.latest_obs, self.latest_jpeg, self.latest_recv = obs, jpeg, time.monotonic()
        self.sun_collector.observe(obs, self.latest_recv)
        self.runlog.maybe_save_frame(header.frame_id, jpeg)
        self.runlog.event(
            "frame", frame_id=header.frame_id, t_capture=header.t_capture,
            bytes=len(jpeg), suns=len(obs.suns),
        )

    async def on_text(self, text: str) -> None:
        msg = parse_client_message(text)
        if isinstance(msg, StateMsg):
            self.judge.on_state(msg)
            self.runlog.event("state", t=msg.t, frame_id=msg.frame_id, data=msg.data)
        elif isinstance(msg, ExecReport):
            self.runlog.event("exec", **msg.model_dump(exclude={"type"}))
        elif isinstance(msg, EpisodeEvent):
            if msg.event in {"start", "end"}:
                # Invalidate in-flight planner/Jev/look results and budgets across games.
                self.episode_generation += 1
                if self.player_runtime:
                    self.player_runtime.reset()
                    self._code_epoch = None
                self.episode_active = msg.event == "start"
                self.sun_collector = SunCollector()
                self._sun_processed_frame = -1
                self.directive = None
                self.latest_scene = None
                self.latest_zombies = None
                self.zombies_received = 0.0
                self.seen_plants.clear()
                self.seen_plants_frame = -1
                self.plants_received = 0.0
                self.placed_since.clear()
                self.used_slots.clear()
                self.sun_spent = self.directive_spent = 0
                self._jev_blocked = {}
                self.threat_rows = []
                self._plan_requested.clear()
                self._plan_reasons.clear()
                self._active_plan_reasons = []
                self._resource_snapshot = None
                self._planning_context = None
                self._planner_started = None
                if msg.event == "start":
                    self.latest_obs = None  # no screenshot from the previous episode may start a new plan
            summary = self.judge.on_episode(msg)
            if msg.event == "end" and summary:
                final = summary.get("final_state") or {}
                if (msg.info.get("reason") == "game_result" and final.get("game") == "real_pvz"
                        and final.get("outcome") in {"win", "lose"}
                        and final.get("outcome_source") == "goty-1096-readonly-result"):
                    self.completed_outcome = final["outcome"]
            self.runlog.event("episode", event=msg.event, level=msg.level, summary=summary,
                              playbook=self.playbook.version)
            if msg.event == "end" and self.latest_obs and self.latest_jpeg:
                self.runlog.maybe_save_frame(self.latest_obs.frame_id, self.latest_jpeg, force=True)
        else:
            log.warning("unexpected message after hello: %s", msg.type)

    # ---- outbound ----

    async def send(self, action: str, obs: Observation | None = None, meta: dict | None = None,
                   **fields) -> Command | None:
        async with self._send_lock:
            if (meta or {}).get("decision_source") == "sun_collector":
                # A label cannot authorize arbitrary coordinates: only the collector's
                # exact current target, while it owns the action lock, is permitted.
                permit = self._sun_permit
                if (permit is None or permit[0] is not obs or action != "click"
                        or fields != {"x": permit[1].x, "y": permit[1].y, "reason": "collect sun"}):
                    raise PermissionError("Automatic input requires an exact sun collection permit")
                if (self.paused or self._fresh_obs() is not obs or not any(
                        s.x == fields["x"] and s.y == fields["y"] for s in self.sun_collector.targets(obs))):
                    return None  # pause/new frame/end while waiting for the send lock
            elif self.settings.decider == "onejev" and (
                action != "click" or not meta or meta.get("decision_source") != "onejev"
                or meta.get("decision_id", 0) <= 0
                or meta.get("decision_id") != self._model_calls["jev"]
            ):
                raise PermissionError("Planting requires the current Jev decision; planner has no input authority")
            self._cmd_id += 1
            cmd = Command(cmd_id=self._cmd_id, frame_id=obs.frame_id if obs else None,
                          t_capture=obs.t_capture if obs else None, action=action, **fields)
            await self.ws.send_text(dumps(cmd))
        self.runlog.event("cmd", meta=meta or {}, **cmd.model_dump(exclude={"type"}))
        return cmd

    # ---- loops ----

    async def report_model_timing(self, stage: str, latency: float, error=None) -> None:
        if not self.telemetry_enabled or latency <= 0:
            return
        await self.report_model_state(stage, "failed" if error else "completed", latency,
                                      "调用失败或超时" if error else "")

    async def report_model_state(self, stage, status, latency=0.0, reason=""):
        if not self.telemetry_enabled:
            return
        if status == "idle" and self._model_idle.get(stage) == reason:
            return
        self._model_idle[stage] = reason if status == "idle" else None
        interval = {"scene": self.settings.look_interval, "plants": self.settings.plants_interval,
                    "zombies": self.settings.zombies_interval,
                    "plan": self.settings.plan_interval, "jev": self.settings.fast_interval}[stage]
        async with self._send_lock:
            await self.ws.send_text(dumps(ModelTiming(
                stage=stage, status=status, call_id=self._model_calls[stage], latency_ms=latency * 1000,
                error=status == "failed", reason=reason, interval_s=interval)))

    async def initialize_model_activity(self):
        for stage in self._model_calls:
            await self.report_model_state(stage, "idle", reason="等待画面或调用条件")

    async def _call_model(self, stage, call):
        self._model_calls[stage] += 1
        await self.report_model_state(stage, "running")
        started = time.monotonic()
        self.runlog.event("model_call", stage=stage, status="running", call_id=self._model_calls[stage])
        try:
            result = await call()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A failed call must not silently kill this layer's recurring loop.
            latency = time.monotonic() - started
            await self.report_model_state(stage, "failed", latency, type(e).__name__)
            self.runlog.event("model_call", stage=stage, status="failed", call_id=self._model_calls[stage],
                              latency=latency, error=type(e).__name__)
            log.exception("%s model call failed; retrying at the next interval", stage)
            return None
        await self.report_model_timing(stage, max(result.latency, 0.000001), result.error)
        return result

    def _fresh_obs(self) -> Observation | None:
        if not self.episode_active or self.latest_obs is None or time.monotonic() - self.latest_recv > STALE_FRAME_SECONDS:
            return None
        return self.latest_obs

    async def look_loop(self) -> None:
        if self.looker is None:
            return
        due = time.monotonic() + self.settings.look_interval
        while True:
            await asyncio.sleep(max(0, due - time.monotonic()))
            due = time.monotonic() + self.settings.look_interval
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                await self.report_model_state("scene", "idle", reason="等待新画面或暂停解除")
                continue
            generation = self.episode_generation
            frame_received = self.latest_recv
            reader = self.looker.look_resources if self.split_vision else self.looker.look
            look = await self._call_model("scene", lambda: reader(
                decode_jpeg(jpeg), obs.frame_id, self.settings.loadout, obs.t_capture))
            if look is None:
                continue
            # The look step's own record: the judge can check it against ground truth, field by field.
            self.runlog.event("scene", frame_id=obs.frame_id, latency=look.latency, error=look.error,
                              input_tokens=look.input_tokens, output_tokens=look.output_tokens,
                              scene=look.scene.model_dump() if look.scene else None)
            if look.scene is not None and generation == self.episode_generation:
                self._accept_scene(look.scene, frame_received)

    def _accept_scene(self, scene: Scene, frame_received: float) -> None:
        if self.split_vision and self.latest_zombies is not None:
            z = self.latest_zombies
            if (scene.lawn, scene.rows) == (z.lawn, z.rows):
                scene = scene.model_copy(update={"zombies": z.zombies, "nearest_col": z.nearest_col,
                                                  "positions": z.positions})
        self.latest_scene = scene
        self.scene_received = frame_received  # inference latency does not make an old image fresh
        # Do not erase commands issued while this screenshot was being analysed.
        newer = [p for p in self.placed_since if p[0] >= scene.frame_id]
        self.used_slots = {self.settings.loadout.index(p[3]) for p in newer
                           if p[3] in self.settings.loadout}
        self.sun_spent = sum(PLANT_COSTS.get(p[3], 0) for p in newer)
        snapshot = {"sun": scene.sun, "usable_and_affordable": [c.plant for c in scene.cards
                    if c.ready and scene.sun is not None and PLANT_COSTS.get(c.plant, 99999) <= scene.sun]}
        if self._resource_snapshot is not None and snapshot != self._resource_snapshot and self.directive is not None:
            self._request_replan({"resources": "阳光或可用卡片发生变化；请用最新资源重新判断旧预算建议"})
        self._resource_snapshot = snapshot
        self._update_threats()

    async def zombies_loop(self):
        if not self.split_vision:
            return
        due = time.monotonic() + self.settings.zombies_interval
        while True:
            await asyncio.sleep(max(0, due - time.monotonic()))
            due = time.monotonic() + self.settings.zombies_interval
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                await self.report_model_state("zombies", "idle", reason="等待新画面或暂停解除")
                continue
            generation, received = self.episode_generation, self.latest_recv
            result = await self._call_model("zombies", lambda: self.looker.look_zombies(
                decode_jpeg(jpeg), obs.frame_id, self.settings.loadout, obs.t_capture))
            if result is None:
                continue
            self.runlog.event("zombies", frame_id=obs.frame_id, latency=result.latency, error=result.error,
                              scene=result.scene.model_dump() if result.scene else None)
            if result.scene is not None and generation == self.episode_generation:
                self._accept_zombies(result.scene, received)

    def _accept_zombies(self, scene, received):
        self.latest_zombies, self.zombies_received = scene, received
        # Updating zombies must not erase the resource/card spending ledger.
        if self.latest_scene and (self.latest_scene.lawn, self.latest_scene.rows) == (scene.lawn, scene.rows):
            self.latest_scene = self.latest_scene.model_copy(update={"zombies": scene.zombies,
                "nearest_col": scene.nearest_col, "positions": scene.positions})
        self._update_threats()

    def _request_replan(self, reasons):
        if not reasons or not self.playbook.timing.danger_replan or self.paused or not self.episode_active:
            return
        changed = {k: v for k, v in reasons.items() if self._plan_reasons.get(k) != v}
        self._plan_reasons.update(reasons)
        self._plan_requested.set()
        if changed:
            self.runlog.event("replan_trigger", reasons=list(changed.values()))
            if self.latest_obs and self.latest_jpeg:
                self.runlog.maybe_save_frame(self.latest_obs.frame_id, self.latest_jpeg, force=True)

    def _update_threats(self):
        if self.latest_scene is None or self._zombie_block_reason():
            return
        rows = observed_threats(self.latest_scene, self.lawn(), self._layout_fresh())
        changes = threat_changes(self.threat_rows, rows)
        if rows != self.threat_rows:
            self.runlog.event("threats", rows=rows, frame_id=self.latest_scene.frame_id)
        self.threat_rows = rows
        self._request_replan(changes)

    def _zombie_block_reason(self):
        if not self.split_vision:
            return ""
        if self.latest_zombies is None:
            return "等待Qwen首次僵尸识别"
        if time.monotonic() - self.zombies_received > max(5.0, 2 * self.settings.zombies_interval):
            return "僵尸信息已过期，等待Qwen刷新"
        if self.latest_scene and (self.latest_scene.lawn, self.latest_scene.rows) != (self.latest_zombies.lawn, self.latest_zombies.rows):
            return "资源与僵尸识别的草坪类型不一致，等待刷新"
        return ""

    async def plants_loop(self) -> None:
        """Read plant layouts at the version's cadence; slow calls never overlap or queue catch-up work."""
        if self.looker is None:
            return
        due = time.monotonic() + self.settings.plants_interval
        while True:
            await asyncio.sleep(max(0, due - time.monotonic()))
            due = time.monotonic() + self.settings.plants_interval
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                await self.report_model_state("plants", "idle", reason="等待新画面或暂停解除")
                continue
            generation = self.episode_generation
            frame_received = self.latest_recv
            seen = await self._call_model("plants", lambda: self.looker.look_plants(decode_jpeg(jpeg), self.settings.loadout))
            if seen is None:
                continue
            self.runlog.event("plants", frame_id=obs.frame_id, latency=seen.latency, error=seen.error,
                              output_tokens=seen.output_tokens,
                              plants=None if seen.plants is None else
                              {f"{r},{c}": v for (r, c), v in sorted(seen.plants.items())})
            if seen.plants is not None and generation == self.episode_generation:
                self.seen_plants, self.seen_plants_frame = seen.plants, obs.frame_id
                self.plants_received = frame_received
                # Preserve the spend/card ledger until the faster scene read has caught up too.
                cutoff = min(obs.frame_id, self.latest_scene.frame_id) if self.latest_scene else -1
                self.placed_since = [p for p in self.placed_since if p[0] >= cutoff]
                self._update_threats()

    def lawn(self) -> dict[tuple[int, int], list[str]]:
        """The lawn as last seen, plus what the agent placed after that snapshot."""
        cells = {cell: list(plants) for cell, plants in self.seen_plants.items()}
        for frame_id, row, col, plant in self.placed_since:
            if frame_id < self.seen_plants_frame:
                continue
            cell = cells.setdefault((row, col), [])
            if plant not in cell:
                cell.append(plant)
        return cells

    async def fast_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.fast_interval)
            obs = self._fresh_obs()
            if obs is None or self.paused:
                continue
            # Keep the explicit offline rule baseline unchanged.
            if self.settings.decider == "rules":
                for click in self.policy.tick(obs):
                    await self.send("click", obs, meta={**click.meta, "decision_source": "rules"},
                                    x=click.x, y=click.y, reason=click.reason)
            place = await self._jev_step(obs)
            if place is not None:
                await self._execute_placement(place)

    async def _execute_placement(self, place: Placement) -> None:
        # A planner budget replacement cannot split the select-card/click-cell pair.
        async with self._action_lock:
            latest, selected = self._decision_context(), self._selected_context
            if (latest is None or selected is None or latest.directive is not selected.directive
                    or latest.scene is not selected.scene or latest.reject(place) is not None):
                return
            obs = self._fresh_obs()
            # Only compile the selected placement; never sneak sun clicks into this pair.
            for click in self.policy.tick(Observation(obs.frame_id, obs.t_capture), place=place):
                await self.send("click", obs, meta={**click.meta, **self._decision_authority()},
                                x=click.x, y=click.y, reason=click.reason)
            card = latest.scene.cards[place.slot].plant
            self.placed_since.append((obs.frame_id, place.row, place.col, card))
            self.used_slots.add(place.slot)
            self.sun_spent += PLANT_COSTS[card]
            self.directive_spent += PLANT_COSTS[card]

    def _decision_context(self) -> DecisionContext | None:
        if self._decision_block_reason():
            return None
        return DecisionContext(self.latest_scene, self.lawn(), set(self.used_slots), self.directive,
                               self.sun_spent, self.directive_spent,
                               layout_fresh=self._layout_fresh(), strategy_knowledge=self.playbook.planner.knowledge)

    def _layout_fresh(self):
        return self.looker is None or (self.seen_plants_frame >= 0 and
            time.monotonic() - self.plants_received <= max(12.0, 2 * self.settings.plants_interval))

    def _decision_authority(self):
        return {"decision_source": self.settings.decider, "decision_id": self._model_calls["jev"]}

    async def sun_loop(self):
        while True:
            await asyncio.sleep(COLLECT_INTERVAL)
            if self.settings.decider == "onejev":
                await self._collect_suns()

    async def _collect_suns(self):
        async with self._action_lock:
            obs = self._fresh_obs()
            if self.paused or obs is None or obs.frame_id <= self._sun_processed_frame:
                return
            self._sun_processed_frame = obs.frame_id
            confirmed = self.sun_collector.targets(obs)
            clicks = self.policy.sun_candidates(Observation(obs.frame_id, obs.t_capture, confirmed))
            sent = 0
            for click in clicks[:2]:  # bounded batch so planting can acquire the lock promptly
                self._sun_permit = (obs, click)
                try:
                    cmd = await self.send("click", obs, meta={**click.meta, "decision_source": "sun_collector",
                                          "rule": "two_frame_confirmation"},
                                          x=click.x, y=click.y, reason="collect sun")
                finally:
                    self._sun_permit = None
                if cmd is not None:
                    self.policy.mark_sun_clicked(click)
                    sent += 1
            self._sun_clicks_sent += sent
            self.runlog.event("sun_collection", frame_id=obs.frame_id, detected=len(obs.suns),
                              confirmed=len(confirmed), clicks_sent=sent, total_clicks=self._sun_clicks_sent,
                              reason="已下发拾取" if sent else "等待新画面或重复目标冷却" if confirmed
                              else "等待连续两帧确认" if obs.suns else "未发现阳光")

    def _decision_block_reason(self) -> str:
        now = time.monotonic()
        if self.paused or not self.episode_active:
            return "对局暂停或已结束"
        if self._fresh_obs() is None:
            return "等待新截图（画面已过期或未收到）"
        if self.latest_scene is None:
            return "等待Qwen首次资源识别"
        if reason := self._zombie_block_reason():
            return reason
        if self.directive is None:
            return "等待Planner首次规划"
        if now - self.directive_received >= self.directive.valid_for_seconds:
            return "Planner指令已过期，等待重新规划"
        if now - self.scene_received > max(5.0, 2 * self.settings.look_interval):
            return "识图结果已过期，等待Qwen刷新"
        return ""

    async def _jev_step(self, obs: Observation) -> Placement | None:
        self._selected_context = None
        ctx = self._decision_context()
        if ctx is None:
            self._jev_blocked = {self._decision_block_reason(): 1}
            await self.report_model_state("jev", "idle", reason=next(iter(self._jev_blocked)))
            return None  # planting waits; the independent sun loop remains available
        self.policy.set_rows(ctx.scene.rows or 5)
        if self.player_runtime:
            generation = self.episode_generation
            try:
                derived = await self._system0()
            except Exception as exc:
                self.runlog.event("system0_error",error=type(exc).__name__,code=self.playbook.player_code)
                return None
            if generation != self.episode_generation or self._decision_block_reason() or self.latest_scene is not ctx.scene:
                return None
            ctx.tool_context = derived["jev_context"]
            if derived["request_replan"]:
                self._plan_requested.set()
        available = candidates(ctx)
        self._jev_blocked = {} if available else blocked_reasons(ctx)
        decision_id = None
        if self.settings.decider == "onejev" and available:
            selected = await self._call_model("jev", lambda: self.selector.select(ctx, self.playbook.jev))
            if selected is None:
                return None
            decision_id = self._model_calls["jev"]
        else:
            reason = ("当前使用规则决策" if self.settings.decider != "onejev" else
                      "；".join(list(self._jev_blocked)[:3]))
            await self.report_model_state("jev", "idle", reason=reason)
            selected = await self.selector.select(ctx, self.playbook.jev)
        place = selected.placement
        latest = self._decision_context()
        rejected = None
        if selected.error or (self.settings.decider == "onejev" and selected.source != "onejev"):
            rejected = "no successful Jev authorization"
        elif latest is None or latest.directive is not ctx.directive or latest.scene is not ctx.scene:
            rejected = "decision context changed or expired during Jev call"
        elif place is not None:
            rejected = latest.reject(place)
        self.runlog.event("jev", frame_id=obs.frame_id, scene_frame=ctx.scene.frame_id,
                          decision_id=decision_id,
                          action="plant" if place else "wait", reason=selected.reason,
                          selector_source=selected.source, selector_latency=selected.latency,
                          selector_error=selected.error, scores=selected.scores,
                          blocked_reasons=self._jev_blocked,
                          guidance=ctx.directive.model_dump(), budget_spent=ctx.directive_spent,
                          decision_context={"scene": ctx.scene.model_dump(),
                              "plants": [{"row": r, "col": c, "plants": ps} for (r,c),ps in sorted(ctx.planted.items())],
                              "sun_spent_since_scene": ctx.sun_spent, "used_slots": sorted(ctx.used_slots),
                              "system0_derived_context":ctx.tool_context,
                              "layout_fresh": ctx.layout_fresh, "budget_advice": ctx.budget_message()},
                          placement=place.model_dump() if place else None, rejected=rejected)
        if not rejected:
            self._selected_context = ctx
        return None if rejected else place

    def _plan_delay(self) -> float:
        """Refresh before expiry, allowing for measured inference time; never extend a lease."""
        if self.directive is None:
            return min(1.0, self.settings.plan_interval)
        age = time.monotonic() - self.directive_received
        lead = max(1.0, self._planner_latency * 1.5)
        since_start = (time.monotonic() - self._planner_started
                       if self._planner_started is not None else age)
        return max(0.25, min(self.settings.plan_interval - since_start,
                             self.directive.valid_for_seconds - age - lead))

    async def plan_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._plan_requested.wait(), timeout=self._plan_delay())
            except TimeoutError:
                pass
            self._plan_requested.clear()
            if self._planner_started is not None:
                spacing = min(self.settings.plan_interval, self.playbook.timing.danger_min_seconds)
                await asyncio.sleep(max(0, spacing - (time.monotonic() - self._planner_started)))
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                await self.report_model_state("plan", "idle", reason="等待新画面或暂停解除")
                continue
            if self.looker is not None and self.latest_scene is None:
                await self.report_model_state("plan", "idle", reason="等待识图完成")
                continue  # split mode decides from a scene; none yet
            if reason := self._zombie_block_reason():
                await self.report_model_state("plan", "idle", reason=reason)
                continue
            generation = self.episode_generation
            self._active_plan_reasons = list(self._plan_reasons.values())
            self._plan_reasons.clear()
            try:
                started = time.monotonic()
                self._planner_started = started
                result = (await self._call_model("plan", lambda: self._plan(obs, jpeg))
                          if self.settings.planner != "mock" else await self._plan(obs, jpeg))
            finally:
                self._planner_latency = time.monotonic() - started
            if result is None:
                continue
            self.runlog.event(
                "plan", frame_id=obs.frame_id, mode=self.settings.planner_mode, latency=result.latency,
                error=result.error, request_id=result.request_id, input_tokens=result.input_tokens,
                output_tokens=result.output_tokens, plan=result.plan.model_dump() if result.plan else None,
                trigger_reasons=self._active_plan_reasons,
                observed_context=self._planning_context,
            )
            if result.plan is not None and generation == self.episode_generation:
                async with self._action_lock:
                    if generation == self.episode_generation:
                        self.directive = result.plan
                        self.directive_received = time.monotonic()
                        self.directive_spent = 0
                        self.plan_note = result.plan.note

    async def _plan(self, obs: Observation, jpeg: bytes) -> PlanResult:
        knowledge = self.playbook.planner.knowledge
        self._planning_context = None
        if self.settings.planner_mode == "end_to_end" or self.looker is None or self.latest_scene is None:
            return await self.planner.plan(jpeg, obs, knowledge)
        reference = jpeg if self.settings.planner_send_image else None
        context = {"plants": [{"row": r, "col": c, "plants": plants}
                              for (r, c), plants in sorted(self.lawn().items())],
                   "previous_guidance_for_reference_only": self.directive.model_dump() if self.directive else None,
                   "committed_spend_under_previous_guidance": self.directive_spent,
                   "plant_layout_fresh": self._layout_fresh(),
                   "observation_frames": {"resources": self.latest_scene.frame_id,
                                          "zombies": self.latest_zombies.frame_id if self.latest_zombies else None,
                                          "plants": self.seen_plants_frame},
                   "jev_blocked_reasons": self._jev_blocked}
        context["observed_threats"] = self.threat_rows
        context["replan_reasons"] = self._active_plan_reasons
        if self.player_runtime:
            derived = await self._system0()
            context["system0_derived_context"] = derived["planner_context"]
        self._planning_context = {"scene": self.latest_scene.model_dump(), "context": context}
        return await self.planner.decide(self.latest_scene, knowledge, reference, context=context)

    async def _system0(self):
        from rsi.native_player import player_input
        scene = self.latest_scene
        if self.paused or not self.episode_active or scene is None:
            raise RuntimeError("System0 cannot run while inactive")
        if self._code_epoch is None:
            self._code_epoch = scene.t_capture
        plants = [{"row":r,"col":c,"plants":ps} for (r,c),ps in self.lawn().items()]
        result = await self.player_runtime.step(player_input(scene,plants,self.directive,self.directive_spent,
                        max(0,int((scene.t_capture-self._code_epoch)*100)),self.latest_jpeg))
        self.runlog.event("system0",code=self.playbook.player_code,frame_id=scene.frame_id,result=result)
        return result

    def close(self) -> None:
        if self.player_runtime:
            self.player_runtime.close()
        self.runlog.event("session_closed")
        self.runlog.close()
