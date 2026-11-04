"""One connected game client: receives frames, runs the layers, sends clicks back.

Concurrent loops run per session:
  look / plants: screenshot -> scene and plant matrix (code + Qwen)
  fast  (every fast_interval s): Jev chooses plant/row/column, or a named decision from
        player_code's menu that its act() carries out, under current guidance
  sun   (every 0.25 s): player_code reflex() (sun pickups, optional gated planting) or, without
        one, the host's own pickup of pixel-confirmed suns; serialized with planting
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
from server.action_selector import (LESSONS_CONTRACT, ActionSelector, DecisionContext, candidates, blocked_reasons,
                                    reject_actions)
from server.action_gate import (MenuItem, PlantAction, ShovelAction, SunAction, parse_actions, parse_menu,
                                split_actions)
from server.judge import Judge
from server.perception import Blob, Observation, Perception, decode_jpeg
from server.planner import Placement, Planner, Plan, PlanResult
from server.policy import Click, FastPolicy
from server.sun_collector import SunCollector, COLLECT_INTERVAL
from server.runlog import RunLog
from server.scene import QwenLooker, Scene
from server.lessons import active_lessons
from server.seed_packets import UNKNOWN, CardMemory, known_plants
from server.shovel import find_shovel
from server.threats import observed_threats
from rules.replan_rules import event_spacing, plan_lifetime, refresh_seconds

log = logging.getLogger(__name__)

STALE_FRAME_SECONDS = 2.0  # stop acting when no fresh frame arrived for this long
REFLEX_TIMEOUT = 0.15  # seconds for a warm player_code reflex() call
REFLEX_MAX_TIMEOUTS = 5  # consecutive timeouts before reflexes are off for the rest of the episode


class Session:
    def __init__(self, ws: WebSocket, settings: Settings, planner: Planner, selector: ActionSelector,
                 playbook: Playbook, session_id: str, looker: QwenLooker | None = None) -> None:
        self.ws = ws
        self.base_settings = settings
        self.settings = effective_settings(settings, playbook)
        self._book_level = None  # Adventure level whose playbook this episode uses (None: as connected)
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
        self.placed_since: list[tuple[int, int, int, str | None]] = []  # (frame_id, row, col, plant or None = dug up)
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
        self._frame_arrived = asyncio.Event()  # replaced on every frame, so each waiter wakes once
        self.paused = False
        self._cmd_id = 0
        self._send_lock = asyncio.Lock()
        self._action_lock = asyncio.Lock()
        self.sun_collector = SunCollector()
        self._sun_permit = None
        self._reflex_permit = None
        self._reflex_timeouts = 0
        self._reflex_disabled = False
        self._last_reflex_plant = float("-inf")
        self._fallback = None  # {plan, reasons, since, plants}: System0 said this plan no longer fits
        self._fallback_permit = None
        self._lesson_errors = set()
        self.card_memory = CardMemory()  # the seed bank as recognized on screen, per level
        self._last_fallback_plant = float("-inf")
        self._menu: list[MenuItem] = []
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
        self.completed_mowers = None  # lawn mowers used in the completed game, from the same read-only result
        self.threat_rows = []
        self._plan_requested = asyncio.Event()
        self._plan_reasons = {}
        self._active_plan_reasons = []
        self._plan_id = 0
        self._plan_failures = 0  # consecutive Jev decisions under the current plan that could not be carried out
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
        self._frame_arrived.set()
        self._frame_arrived = asyncio.Event()
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
            level = (msg.data or {}).get("level")
            if type(level) is int and 1 <= level <= 50 and level != self._book_level:  # 0 while a level loads
                self._use_level_book(level)
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
                self._reflex_timeouts = 0
                self._reflex_disabled = False
                self._last_reflex_plant = float("-inf")
                self._fallback = None
                self._last_fallback_plant = float("-inf")
                self._lesson_errors = set()
                self.card_memory.reset()  # another level may have other cards
                self._menu = []
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
                self._plan_failures = 0
                self._planning_context = None
                self._planner_started = None
                if msg.event == "start":
                    self.latest_obs = None  # no screenshot from the previous episode may start a new plan
                    self._book_level = None  # training may have changed the level's playbook since
            summary = self.judge.on_episode(msg)
            if msg.event == "end" and summary:
                final = summary.get("final_state") or {}
                if (msg.info.get("reason") == "game_result" and final.get("game") == "real_pvz"
                        and final.get("outcome") in {"win", "lose"}
                        and final.get("outcome_source") == "goty-1096-readonly-result"):
                    self.completed_outcome = final["outcome"]
                    self.completed_mowers = final.get("mowers_used")
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
            source = (meta or {}).get("decision_source")
            if source == "system0_fallback":
                # Fallback planting: only the exact clicks compiled from a gated, hard-rule-checked placement.
                permit = self._fallback_permit
                if permit is None or permit[0] is not obs or action != "click" or fields != permit[1]:
                    raise PermissionError("Fallback planting requires an exact gated placement permit")
            elif source == "system0_reflex" and fields.get("reason") != "collect sun":
                # Reflex planting: only the exact clicks compiled from a gated, hard-rule-checked placement.
                permit = self._reflex_permit
                if permit is None or permit[0] is not obs or action != "click" or fields != permit[1]:
                    raise PermissionError("Reflex planting requires an exact gated placement permit")
            elif source in {"sun_collector", "system0_reflex"}:
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

    async def _next_frame(self, arrived: asyncio.Event, timeout: float) -> float:
        """A look skipped for want of a fresh frame retries when the next one lands, not at its next tick:
        when frames come slower than STALE_FRAME_SECONDS, fixed ticks keep falling between them."""
        try:
            await asyncio.wait_for(arrived.wait(), timeout)
        except TimeoutError:
            pass
        return time.monotonic()

    async def look_loop(self) -> None:
        if self.looker is None:
            return
        due = time.monotonic() + self.settings.look_interval
        while True:
            await asyncio.sleep(max(0, due - time.monotonic()))
            due = time.monotonic() + self.settings.look_interval
            arrived = self._frame_arrived
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                await self.report_model_state("scene", "idle", reason="等待新画面或暂停解除")
                due = await self._next_frame(arrived, self.settings.look_interval)
                continue
            generation = self.episode_generation
            frame_received = self.latest_recv
            reader = self.looker.look_resources if self.split_vision else self.looker.look
            image = decode_jpeg(jpeg)
            cards = self.card_memory.observe(image)  # which plant each seed slot shows, from the screenshot
            look = await self._call_model("scene", lambda: reader(image, obs.frame_id, cards, obs.t_capture))
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
        names = [card.plant for card in scene.cards]
        self.used_slots = {names.index(p[3]) for p in newer if p[3] in names}
        self.sun_spent = sum(PLANT_COSTS.get(p[3], 0) for p in newer)
        self._update_threats()

    async def zombies_loop(self):
        if not self.split_vision:
            return
        due = time.monotonic() + self.settings.zombies_interval
        while True:
            await asyncio.sleep(max(0, due - time.monotonic()))
            due = time.monotonic() + self.settings.zombies_interval
            arrived = self._frame_arrived
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                await self.report_model_state("zombies", "idle", reason="等待新画面或暂停解除")
                due = await self._next_frame(arrived, self.settings.zombies_interval)
                continue
            generation, received = self.episode_generation, self.latest_recv
            result = await self._call_model("zombies", lambda: self.looker.look_zombies(
                decode_jpeg(jpeg), obs.frame_id, list(self.card_memory.names), obs.t_capture))
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
        """Observed threat rows are Planner/Jev context; whether they need a new plan is System0's call."""
        if self.latest_scene is None or self._zombie_block_reason():
            return
        rows = observed_threats(self.latest_scene, self.lawn(), self._layout_fresh())
        if rows != self.threat_rows:
            self.runlog.event("threats", rows=rows, frame_id=self.latest_scene.frame_id)
        self.threat_rows = rows

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
            arrived = self._frame_arrived
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                await self.report_model_state("plants", "idle", reason="等待新画面或暂停解除")
                due = await self._next_frame(arrived, self.settings.plants_interval)
                continue
            generation = self.episode_generation
            frame_received = self.latest_recv
            # Lawn plants are named from the recognized cards (all known card art before the first look).
            names = [n for n in self.card_memory.names if n != UNKNOWN] or known_plants()
            seen = await self._call_model("plants", lambda: self.looker.look_plants(decode_jpeg(jpeg), names))
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
        """The lawn as last seen, plus what the agent placed (or dug up) after that snapshot, in order."""
        cells = {cell: list(plants) for cell, plants in self.seen_plants.items()}
        for frame_id, row, col, plant in self.placed_since:
            if frame_id < self.seen_plants_frame:
                continue
            if plant is None:  # shovel
                cells.pop((row, col), None)
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
            decision = await self._jev_step(obs)
            if decision is not None:
                await self._execute_placement(*decision)

    async def _execute_placement(self, place: Placement | None, shovel: ShovelAction | None = None) -> None:
        # A planner budget replacement cannot split the select-card/click-cell pair.
        async with self._action_lock:
            latest, selected = self._decision_context(), self._selected_context
            if (latest is None or selected is None or latest.directive is not selected.directive
                    or latest.scene is not selected.scene or reject_actions(latest, place, shovel) is not None):
                return
            obs = self._fresh_obs()
            if shovel is not None:
                clicks = self._shovel_clicks(shovel)
                if clicks is None:
                    return  # the shovel left its box since the look; neither half runs
                for click in clicks:
                    await self.send("click", obs, meta={**click.meta, **self._decision_authority()},
                                    x=click.x, y=click.y, reason=click.reason)
                self._record_shovel(obs, shovel)
            if place is None:
                return
            # Only compile the selected placement; never sneak sun clicks into this pair.
            for click in self.policy.tick(Observation(obs.frame_id, obs.t_capture), place=place):
                await self.send("click", obs, meta={**click.meta, **self._decision_authority()},
                                x=click.x, y=click.y, reason=click.reason)
            self._record_placement(obs, latest.scene, place)

    def _shovel_clicks(self, shovel: ShovelAction) -> list[Click] | None:
        """Pick up the shovel from where the latest frame shows it, then click the plant's cell."""
        x0 = find_shovel(decode_jpeg(self.latest_jpeg)) if self.latest_jpeg else None
        if x0 is None:
            self.runlog.event("shovel", row=shovel.row, col=shovel.col, rejected="no shovel in the latest frame")
            return None
        return self.policy.shovel_clicks(x0, shovel.row, shovel.col)

    def _record_shovel(self, obs: Observation, shovel: ShovelAction) -> None:
        self.placed_since.append((obs.frame_id, shovel.row, shovel.col, None))
        self.runlog.event("shovel", frame_id=obs.frame_id, row=shovel.row, col=shovel.col, rejected=None)

    def _record_placement(self, obs: Observation, scene: Scene, place: Placement) -> None:
        card = scene.cards[place.slot].plant
        self.placed_since.append((obs.frame_id, place.row, place.col, card))
        self.used_slots.add(place.slot)
        self.sun_spent += PLANT_COSTS[card]
        self.directive_spent += PLANT_COSTS[card]

    def _decision_context(self) -> DecisionContext | None:
        if self._decision_block_reason():
            return None
        planted = self.lawn()
        return DecisionContext(self.latest_scene, planted, set(self.used_slots), self.directive,
                               self.sun_spent, self.directive_spent,
                               layout_fresh=self._layout_fresh(), strategy_knowledge=self.playbook.planner.knowledge,
                               lessons=self._lessons(planted))

    def _lessons(self, planted):
        scene = self.latest_scene
        shown, errors = active_lessons(self.playbook.lessons, scene, planted,
                                       None if scene.sun is None else scene.sun - self.sun_spent)
        for lesson, error in errors.items():
            if lesson not in self._lesson_errors:  # once per lesson per episode
                self._lesson_errors.add(lesson)
                self.runlog.event("lesson_error", lesson=lesson, error=error)
        return shown

    def _layout_fresh(self):
        return self.looker is None or (self.seen_plants_frame >= 0 and
            time.monotonic() - self.plants_received <= max(12.0, 2 * self.settings.plants_interval))

    def _decision_authority(self):
        return {"decision_source": self.settings.decider, "decision_id": self._model_calls["jev"]}

    async def sun_loop(self):
        while True:
            await asyncio.sleep(COLLECT_INTERVAL)
            if self.settings.decider == "onejev":
                if self._reflex_active():
                    await self._reflex()
                else:
                    await self._collect_suns()

    async def _reflex(self):
        """player_code reflex(): choose sun pickups and, if the playbook allows, one gated plant.

        Any error, invalid output or timeout falls back to the host's own pickup this tick.
        """
        obs = self._fresh_obs()
        if self.paused or obs is None or obs.frame_id <= self._sun_processed_frame:
            return
        confirmed = self.sun_collector.targets(obs)
        clicks = self.policy.sun_candidates(Observation(obs.frame_id, obs.t_capture, confirmed))
        reflex = self.playbook.reflex
        ctx = self._decision_context() if reflex.allow_planting and self.settings.system0_actions else None
        may_plant = ctx is not None and time.monotonic() - self._last_reflex_plant >= reflex.min_plant_interval_s
        if not clicks and not may_plant:
            return
        if self.latest_scene is None:
            return await self._collect_suns()
        generation = self.episode_generation
        try:
            targets = [Blob(c.x, c.y, int(c.meta.get("area", 0)), float(c.meta.get("hue", 0.0))) for c in clicks]
            observation = self._system0_observation(ctx if may_plant else None, suns=targets)
            raw = await self._system0("reflex", observation=observation, warm_timeout=REFLEX_TIMEOUT)
            actions = parse_actions(raw, "reflex", suns=len(clicks), allow_planting=may_plant)
            self._reflex_timeouts = 0
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                self._reflex_timeouts += 1
                if self._reflex_timeouts >= REFLEX_MAX_TIMEOUTS:
                    self._reflex_disabled = True
            self.runlog.event("system0_error", entry="reflex", error=f"{type(exc).__name__}: {exc}"[:300],
                              code=self.playbook.player_code, reflex_disabled=self._reflex_disabled)
            return await self._collect_suns()
        if generation != self.episode_generation:
            return
        async with self._action_lock:
            if self.paused or self._fresh_obs() is not obs or obs.frame_id <= self._sun_processed_frame:
                return
            self._sun_processed_frame = obs.frame_id
            sent, plant, rejected = 0, None, None
            for action in actions:
                if isinstance(action, SunAction):
                    click = clicks[action.index]
                    self._sun_permit = (obs, click)
                    try:
                        cmd = await self.send("click", obs, meta={**click.meta, "decision_source": "system0_reflex",
                                              "rule": "two_frame_confirmation"},
                                              x=click.x, y=click.y, reason="collect sun")
                    finally:
                        self._sun_permit = None
                    if cmd is not None:
                        self.policy.mark_sun_clicked(click)
                        sent += 1
                elif isinstance(action, PlantAction):
                    plant = Placement(slot=action.slot, row=action.row, col=action.col)
                    latest = self._decision_context()
                    rejected = "decision context unavailable" if latest is None else latest.reject(plant)
                    if rejected is None:
                        for click in self.policy.tick(Observation(obs.frame_id, obs.t_capture), place=plant):
                            fields = {"x": click.x, "y": click.y, "reason": click.reason}
                            self._reflex_permit = (obs, fields)
                            try:
                                await self.send("click", obs, meta={**click.meta, "decision_source": "system0_reflex"},
                                                **fields)
                            finally:
                                self._reflex_permit = None
                        self._record_placement(obs, latest.scene, plant)
                        self._last_reflex_plant = time.monotonic()
            self._sun_clicks_sent += sent
            self.runlog.event("sun_collection", frame_id=obs.frame_id, detected=len(obs.suns),
                              confirmed=len(confirmed), clicks_sent=sent, total_clicks=self._sun_clicks_sent,
                              decision_source="system0_reflex",
                              reason="已下发拾取" if sent else "reflex 未选择阳光" if clicks else "未发现阳光")
            if plant is not None:
                self.runlog.event("reflex_plant", frame_id=obs.frame_id, placement=plant.model_dump(),
                                  rejected=rejected)

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
        if now - self.directive_received >= plan_lifetime(self.directive.valid_for_seconds):
            return "Planner指令已过期，等待重新规划"
        if now - self.scene_received > max(5.0, 2 * self.settings.look_interval):
            return "识图结果已过期，等待Qwen刷新"
        return ""

    async def _jev_step(self, obs: Observation) -> tuple[Placement | None, ShovelAction | None] | None:
        """The accepted decision: an optional dig, then an optional plant; None waits."""
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
                reasons = derived["planner_context"].get("replan_reasons")
                reasons = [str(r)[:200] for r in reasons][:6] if isinstance(reasons, list) and reasons else ["unspecified"]
                self._request_replan({"system0:" + r: r for r in reasons})
                if self._fallback_enabled() and (self._fallback or {}).get("plan") != self._plan_id:
                    self._fallback = {"plan": self._plan_id, "reasons": reasons, "since": time.monotonic(), "plants": []}
                    self.runlog.event("fallback_start", plan_id=self._plan_id, reasons=reasons)
            if self._fallback_window():
                if await self._fallback_step(ctx):
                    return None
                ctx.tool_context = {**ctx.tool_context, "plan_no_longer_fits": self._fallback["reasons"]}
        available = candidates(ctx)
        self._jev_blocked = {} if available else blocked_reasons(ctx)
        decision_id = None
        menu_log = None
        shovel = None
        if self._menu_active() and available:
            # Named decisions: player_code lists them, Jev picks one, player_code's act() places it.
            generation = self.episode_generation
            observation = self._system0_observation(ctx)
            try:
                menu = parse_menu(await self._system0("options", observation=observation), ctx.scene.rows or 5)
            except Exception as exc:
                self.runlog.event("system0_error", entry="options", error=f"{type(exc).__name__}: {exc}"[:300],
                                  code=self.playbook.player_code)
                return None
            if generation != self.episode_generation or self.latest_scene is not ctx.scene:
                return None
            self._menu = menu
            if not menu:
                await self.report_model_state("jev", "idle", reason="决策菜单为空")
                self.runlog.event("jev", frame_id=obs.frame_id, scene_frame=ctx.scene.frame_id, decision_id=None,
                                  action="wait", reason="empty decision menu", menu=[], guidance=ctx.directive.model_dump())
                return None
            selected = await self._call_model("jev", lambda: self.selector.select_menu(
                ctx, menu, ctx.directive.preferred_decisions))
            if selected is None:
                return None
            decision_id = self._model_calls["jev"]
            menu_log = {"menu": [item.public() for item in menu],
                        "decision": selected.decision.public() if selected.decision else None}
            if selected.decision is not None and not selected.error:
                try:
                    actions = parse_actions(await self._system0("act", observation=observation,
                                            decision=selected.decision.call_argument()), "act", suns=0)
                except Exception as exc:
                    actions = []
                    menu_log["act_error"] = f"{type(exc).__name__}: {exc}"[:300]
                menu_log["act"] = [vars(a) for a in actions]
                shovel, plant = split_actions(actions)
                if plant is not None:
                    selected.placement = Placement(slot=plant.slot, row=plant.row, col=plant.col)
                if not actions:
                    menu_log["decision_unexecutable"] = True
        elif self.settings.decider == "onejev" and available:
            if self.playbook.decision.jev_mode == "flat":
                selected = await self._call_model("jev", lambda: self.selector.select(ctx, self.playbook.jev, mode="flat"))
            else:
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
        elif place is not None or shovel is not None:
            rejected = reject_actions(latest, place, shovel)
        acting = place is not None or shovel is not None
        if (menu_log or {}).get("decision_unexecutable") or (acting and rejected and latest is not None
                and latest.directive is ctx.directive and latest.scene is ctx.scene):
            self._plan_failures += 1
        elif acting and not rejected:
            self._plan_failures = 0
        self.runlog.event("jev", frame_id=obs.frame_id, scene_frame=ctx.scene.frame_id,
                          decision_id=decision_id, **(menu_log or {}),
                          action=("shovel+plant" if shovel and place else "shovel" if shovel
                                  else "plant" if place else "wait"), reason=selected.reason,
                          selector_source=selected.source, selector_latency=selected.latency,
                          selector_error=selected.error, scores=selected.scores,
                          blocked_reasons=self._jev_blocked, lessons=[l["id"] for l in ctx.lessons],
                          guidance=ctx.directive.model_dump(), budget_spent=ctx.directive_spent,
                          decision_context={"scene": ctx.scene.model_dump(),
                              "plants": [{"row": r, "col": c, "plants": ps} for (r,c),ps in sorted(ctx.planted.items())],
                              "sun_spent_since_scene": ctx.sun_spent, "used_slots": sorted(ctx.used_slots),
                              "system0_derived_context":ctx.tool_context,
                              "layout_fresh": ctx.layout_fresh, "budget_advice": ctx.budget_message()},
                          placement=place.model_dump() if place else None,
                          shovel=vars(shovel) if shovel else None, rejected=rejected)
        if not rejected:
            self._selected_context = ctx
        return None if rejected or not acting else (place, shovel)

    def _plan_delay(self) -> float:
        """Refresh before expiry, allowing for measured inference time; never extend a lease."""
        if self.directive is None:
            return min(1.0, self.settings.plan_interval)
        age = time.monotonic() - self.directive_received
        lead = max(1.0, self._planner_latency * 1.5)
        since_start = (time.monotonic() - self._planner_started
                       if self._planner_started is not None else age)
        return max(0.25, min(refresh_seconds(self.settings.plan_interval, self.directive.valid_for_seconds) - since_start,
                             plan_lifetime(self.directive.valid_for_seconds) - age - lead))

    async def plan_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._plan_requested.wait(), timeout=self._plan_delay())
            except TimeoutError:
                pass
            self._plan_requested.clear()
            if self._planner_started is not None:
                spacing = event_spacing(self.playbook.timing.danger_min_seconds)
                await asyncio.sleep(max(0, spacing - (time.monotonic() - self._planner_started)))
            arrived = self._frame_arrived
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                await self.report_model_state("plan", "idle", reason="等待新画面或暂停解除")
                # Still due (an event or the refresh), so retry on the next frame rather than the next refresh.
                await self._next_frame(arrived, max(1.0, self._plan_delay()))
                self._plan_requested.set()
                continue
            if self.looker is not None and self.latest_scene is None:
                await self.report_model_state("plan", "idle", reason="等待识图完成")
                continue  # split mode decides from a scene; none yet
            if reason := self._zombie_block_reason():
                await self.report_model_state("plan", "idle", reason=reason)
                continue
            generation = self.episode_generation
            trigger = ("initial" if self.directive is None else "system0" if self._plan_reasons else "periodic")
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
                trigger=trigger, trigger_reasons=self._active_plan_reasons,
                observed_context=self._planning_context,
            )
            if result.plan is not None and generation == self.episode_generation:
                async with self._action_lock:
                    if generation == self.episode_generation:
                        self.directive = result.plan
                        self.directive_received = time.monotonic()
                        self.directive_spent = 0
                        self.plan_note = result.plan.note
                        self._end_fallback()
                        self._plan_id += 1
                        self._plan_failures = 0

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
        if lessons := self._lessons(self.lawn()):
            context["lessons_now"] = lessons
            context["lessons_contract"] = LESSONS_CONTRACT
        if self.player_runtime:
            derived = await self._system0()
            context["system0_derived_context"] = derived["planner_context"]
        if self._menu_active():
            context["decision_menu"] = [item.public() for item in self._menu]
            context["decision_menu_note"] = "Latest named decisions offered to Jev; it may change as the lawn changes."
        self._planning_context = {"scene": self.latest_scene.model_dump(), "context": context}
        return await self.planner.decide(self.latest_scene, knowledge, reference, context=context)

    def _menu_active(self) -> bool:
        return (self.player_runtime is not None and self.player_runtime.has_menu
                and self.settings.decider == "onejev" and self.playbook.decision.jev_mode == "auto")

    def _reflex_active(self) -> bool:
        return (self.player_runtime is not None and "reflex" in self.player_runtime.entries
                and not self._reflex_disabled)

    def _system0_observation(self, ctx: DecisionContext | None = None, suns=(), pixels=False, status=False):
        """Player-side input for every System0 entry; legality only from the hard rules."""
        from rsi.native_player import player_input
        scene = self.latest_scene
        if self.paused or not self.episode_active or scene is None:
            raise RuntimeError("System0 cannot run while inactive")
        if self._code_epoch is None:
            self._code_epoch = scene.t_capture
        plants = [{"row":r,"col":c,"plants":ps} for (r,c),ps in self.lawn().items()]
        legal = None if ctx is None else [(p.slot, p.row, p.col) for p in candidates(ctx)]
        return player_input(scene, plants, self.directive, self.directive_spent,
                            max(0,int((scene.t_capture-self._code_epoch)*100)),
                            self.latest_jpeg if pixels else None, legal=legal, suns=suns,
                            sun_available=None if scene.sun is None else scene.sun - self.sun_spent,
                            plan_status=self._plan_status() if status else None)

    def _use_level_book(self, level):
        """Play this level's own playbook (server/level_books.py), as training does; CURRENT if it has none.

        The level number is what the game shows the player; it only selects the playbook.
        """
        from rsi.native_player import NativeCodeStore, PlayerRuntime
        from server.level_books import LevelBooks
        from server.playbook import PlaybookStore
        self._book_level = level
        store = PlaybookStore(self.base_settings.data_dir)
        try:
            version = LevelBooks(self.base_settings.data_dir).version(store, level)
            if version == self.playbook.version:
                self.runlog.event("level_playbook", level=level, version=version, previous=version, switched=False)
                return
            book = store.load(version)
            source = NativeCodeStore(self.base_settings.data_dir).source(book.player_code) if book.player_code else None
        except Exception as exc:
            self.runlog.event("level_playbook_error", level=level, error=f"{type(exc).__name__}: {exc}"[:300])
            return
        previous, self.playbook = self.playbook, book
        self.settings = effective_settings(self.base_settings, book)
        if book.player_code != previous.player_code:
            if self.player_runtime:
                self.player_runtime.close()
            self.player_runtime = PlayerRuntime(source) if source else None
            self._reflex_disabled = False
            self._reflex_timeouts = 0
        self.runlog.event("level_playbook", level=level, version=book.version, previous=previous.version, switched=True)

    def _plan_status(self):
        if self.directive is None:
            return None
        return {"id": self._plan_id, "age_s": round(time.monotonic() - self.directive_received, 1),
                "failed_steps": self._plan_failures,
                "fallback_reasons": self._fallback["reasons"] if self._fallback_window() else []}

    def _fallback_enabled(self) -> bool:
        return (self.player_runtime is not None and "fallback" in self.player_runtime.entries
                and self.playbook.fallback.allow_planting and self.settings.system0_actions
                and self.playbook.timing.danger_replan)

    def _fallback_window(self) -> bool:
        return self._fallback is not None and self._fallback["plan"] == self._plan_id and self._fallback_enabled()

    def _end_fallback(self):
        if self._fallback is not None and self._fallback["plan"] == self._plan_id:
            self.runlog.event("fallback_end", plan_id=self._plan_id, reasons=self._fallback["reasons"],
                              duration_s=round(time.monotonic() - self._fallback["since"], 1),
                              plants=self._fallback["plants"])
        self._fallback = None

    async def _fallback_step(self, ctx: DecisionContext) -> bool:
        """Player code plants in Jev's place while the plan no longer fits; False hands this step to Jev."""
        if time.monotonic() - self._last_fallback_plant < self.playbook.fallback.min_plant_interval_s:
            return True
        generation = self.episode_generation
        try:
            observation = self._system0_observation(ctx, status=True)
            actions = parse_actions(await self._system0("fallback", observation=observation), "fallback", suns=0)
        except Exception as exc:
            self.runlog.event("system0_error", entry="fallback", error=f"{type(exc).__name__}: {exc}"[:300],
                              code=self.playbook.player_code)
            return False
        if generation != self.episode_generation or not self._fallback_window():
            return True
        await self.report_model_state("jev", "idle", reason="方针已失效，代码接管直到新方针")
        plant, rejected = None, None
        shovel, planted = split_actions(actions)
        async with self._action_lock:
            latest = self._decision_context()
            if planted is not None:
                plant = Placement(slot=planted.slot, row=planted.row, col=planted.col)
            if plant is not None or shovel is not None:
                if latest is None or latest.directive is not ctx.directive or latest.scene is not ctx.scene:
                    rejected = "decision context changed or expired during fallback call"
                else:
                    rejected = reject_actions(latest, plant, shovel)
            if (plant is not None or shovel is not None) and rejected is None:
                obs = self._fresh_obs()
                clicks = [] if shovel is None else self._shovel_clicks(shovel)
                if clicks is None:
                    rejected = "no shovel in the latest frame"
                else:
                    if plant is not None:
                        clicks += self.policy.tick(Observation(obs.frame_id, obs.t_capture), place=plant)
                    for click in clicks:
                        fields = {"x": click.x, "y": click.y, "reason": click.reason}
                        self._fallback_permit = (obs, fields)
                        try:
                            await self.send("click", obs, meta={**click.meta, "decision_source": "system0_fallback"},
                                            **fields)
                        finally:
                            self._fallback_permit = None
                    if shovel is not None:
                        self._record_shovel(obs, shovel)
                    if plant is not None:
                        self._record_placement(obs, latest.scene, plant)
                        self._fallback["plants"].append(plant.model_dump())
                    self._last_fallback_plant = time.monotonic()
        self.runlog.event("fallback", plan_id=self._plan_id, scene_frame=ctx.scene.frame_id,
                          reasons=self._fallback["reasons"],
                          action=("shovel+plant" if shovel and plant else "shovel" if shovel
                                  else "plant" if plant else "wait"),
                          placement=plant.model_dump() if plant else None,
                          shovel=vars(shovel) if shovel else None, rejected=rejected)
        return True

    async def _system0(self, entry="decide", *, observation=None, decision=None, warm_timeout=None):
        if observation is None:
            observation = self._system0_observation(pixels=True, status=entry == "decide")
        frame = self.latest_scene.frame_id if self.latest_scene else None
        result = await self.player_runtime.step(observation, entry, decision, warm_timeout)
        if entry != "reflex":  # reflex results are logged with what the gate did with them
            self.runlog.event("system0", entry=entry, code=self.playbook.player_code, frame_id=frame, result=result)
        return result

    def close(self) -> None:
        if self.player_runtime:
            self.player_runtime.close()
        self.runlog.event("session_closed")
        self.runlog.close()
