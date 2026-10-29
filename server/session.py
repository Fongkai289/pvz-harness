"""One connected game client: receives frames, runs the layers, sends clicks back.

Concurrent loops run per session:
  look / plants: screenshot -> scene and plant matrix (code + Qwen)
  fast  (every fast_interval s): sun clicks; Jev chooses plant/row/column under current guidance
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
    StateMsg,
    dumps,
    parse_client_message,
    unpack_frame,
)
from rules.playbook_rules import Playbook
from server.config import Settings
from rules.hard_rules import PLANT_COSTS
from server.action_selector import ActionSelector, DecisionContext
from server.judge import Judge
from server.perception import Observation, Perception, decode_jpeg
from server.planner import Placement, Planner, Plan, PlanResult
from server.policy import FastPolicy
from server.runlog import RunLog
from server.scene import QwenLooker, Scene

log = logging.getLogger(__name__)

STALE_FRAME_SECONDS = 2.0  # stop acting when no fresh frame arrived for this long


class Session:
    def __init__(self, ws: WebSocket, settings: Settings, planner: Planner, selector: ActionSelector,
                 playbook: Playbook, session_id: str, looker: QwenLooker | None = None) -> None:
        self.ws = ws
        self.settings = settings
        self.planner = planner
        self.selector = selector
        self.looker = looker  # Jev always needs observed state, even with an image-based planner
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
        self.placed_since: list[tuple[int, int, int, str]] = []  # (frame_id, row, col, plant)
        self.used_slots: set[int] = set()  # cards used since the latest scene was taken
        self.sun_spent = 0
        self.session_id = session_id
        self.perception = Perception(playbook.perception)
        self.policy = FastPolicy(playbook.fast)
        self.judge = Judge()  # sees ground truth; nothing on the player side reads from it
        self.runlog = RunLog(
            settings.runs_dir, session_id, settings.frame_log_interval, settings.max_run_bytes
        )
        self.latest_obs: Observation | None = None
        self.latest_jpeg: bytes | None = None
        self.latest_recv = 0.0
        self.paused = False
        self._cmd_id = 0
        self._send_lock = asyncio.Lock()
        self._action_lock = asyncio.Lock()
        self._selected_context: DecisionContext | None = None

    # ---- inbound ----

    async def on_frame(self, data: bytes) -> None:
        header, jpeg = unpack_frame(data)
        obs = self.perception.process(header, decode_jpeg(jpeg))
        self.latest_obs, self.latest_jpeg, self.latest_recv = obs, jpeg, time.monotonic()
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
                self.episode_active = msg.event == "start"
                self.directive = None
                self.latest_scene = None
                self.seen_plants.clear()
                self.seen_plants_frame = -1
                self.placed_since.clear()
                self.used_slots.clear()
                self.sun_spent = self.directive_spent = 0
                if msg.event == "start":
                    self.latest_obs = None  # no screenshot from the previous episode may start a new plan
            summary = self.judge.on_episode(msg)
            self.runlog.event("episode", event=msg.event, level=msg.level, summary=summary,
                              playbook=self.playbook.version)
            if msg.event == "end" and self.latest_obs and self.latest_jpeg:
                self.runlog.maybe_save_frame(self.latest_obs.frame_id, self.latest_jpeg, force=True)
        else:
            log.warning("unexpected message after hello: %s", msg.type)

    # ---- outbound ----

    async def send(self, action: str, obs: Observation | None = None, meta: dict | None = None,
                   **fields) -> Command:
        self._cmd_id += 1
        cmd = Command(
            cmd_id=self._cmd_id,
            frame_id=obs.frame_id if obs else None,
            t_capture=obs.t_capture if obs else None,
            action=action,
            **fields,
        )
        async with self._send_lock:
            await self.ws.send_text(dumps(cmd))
        self.runlog.event("cmd", meta=meta or {}, **cmd.model_dump(exclude={"type"}))
        return cmd

    # ---- loops ----

    def _fresh_obs(self) -> Observation | None:
        if not self.episode_active or self.latest_obs is None or time.monotonic() - self.latest_recv > STALE_FRAME_SECONDS:
            return None
        return self.latest_obs

    async def look_loop(self) -> None:
        if self.looker is None:
            return
        while True:
            await asyncio.sleep(self.settings.look_interval)
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                continue
            generation = self.episode_generation
            frame_received = self.latest_recv
            look = await self.looker.look(decode_jpeg(jpeg), obs.frame_id, self.settings.loadout, obs.t_capture)
            # The look step's own record: the judge can check it against ground truth, field by field.
            self.runlog.event("scene", frame_id=obs.frame_id, latency=look.latency, error=look.error,
                              input_tokens=look.input_tokens, output_tokens=look.output_tokens,
                              scene=look.scene.model_dump() if look.scene else None)
            if look.scene is not None and generation == self.episode_generation:
                self._accept_scene(look.scene, frame_received)

    def _accept_scene(self, scene: Scene, frame_received: float) -> None:
        self.latest_scene = scene
        self.scene_received = frame_received  # inference latency does not make an old image fresh
        # Do not erase commands issued while this screenshot was being analysed.
        newer = [p for p in self.placed_since if p[0] >= scene.frame_id]
        self.used_slots = {self.settings.loadout.index(p[3]) for p in newer
                           if p[3] in self.settings.loadout}
        self.sun_spent = sum(PLANT_COSTS.get(p[3], 0) for p in newer)

    async def plants_loop(self) -> None:
        """Read the plants on the lawn, slower than zombies: plants change less often and the read is slower."""
        if self.looker is None:
            return
        while True:
            await asyncio.sleep(self.settings.plants_interval)
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                continue
            generation = self.episode_generation
            seen = await self.looker.look_plants(decode_jpeg(jpeg), self.settings.loadout)
            self.runlog.event("plants", frame_id=obs.frame_id, latency=seen.latency, error=seen.error,
                              output_tokens=seen.output_tokens,
                              plants=None if seen.plants is None else
                              {f"{r},{c}": v for (r, c), v in sorted(seen.plants.items())})
            if seen.plants is not None and generation == self.episode_generation:
                self.seen_plants, self.seen_plants_frame = seen.plants, obs.frame_id
                # Preserve the spend/card ledger until the faster scene read has caught up too.
                cutoff = min(obs.frame_id, self.latest_scene.frame_id) if self.latest_scene else -1
                self.placed_since = [p for p in self.placed_since if p[0] >= cutoff]

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
            # Suns first: they must never wait behind a model call. On 2026-10-29 OneJev (a shared pool)
            # took up to its 1 s timeout and capture-to-click p95 went from 346 ms to 1256 ms.
            for click in self.policy.tick(obs):
                await self.send("click", obs, meta=click.meta, x=click.x, y=click.y, reason=click.reason)
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
            for click in self.policy.tick(obs, place=place):
                await self.send("click", obs, meta=click.meta, x=click.x, y=click.y, reason=click.reason)
            card = latest.scene.cards[place.slot].plant
            self.placed_since.append((obs.frame_id, place.row, place.col, card))
            self.used_slots.add(place.slot)
            self.sun_spent += PLANT_COSTS[card]
            self.directive_spent += PLANT_COSTS[card]

    def _decision_context(self) -> DecisionContext | None:
        now = time.monotonic()
        if self.paused or self._fresh_obs() is None or self.latest_scene is None or self.directive is None:
            return None
        if now - self.directive_received >= self.directive.valid_for_seconds:
            return None
        if now - self.scene_received > max(5.0, 2 * self.settings.look_interval):
            return None
        return DecisionContext(self.latest_scene, self.lawn(), set(self.used_slots), self.directive,
                               self.sun_spent, self.directive_spent)

    async def _jev_step(self, obs: Observation) -> Placement | None:
        self._selected_context = None
        ctx = self._decision_context()
        if ctx is None:
            return None  # no fresh scene/directive: collect suns only, no planner-placement bypass
        self.policy.set_rows(ctx.scene.rows or 5)
        selected = await self.selector.select(ctx, self.playbook.jev)
        place = selected.placement
        latest = self._decision_context()
        rejected = None
        if latest is None or latest.directive is not ctx.directive or latest.scene is not ctx.scene:
            rejected = "decision context changed or expired during Jev call"
        elif place is not None:
            rejected = latest.reject(place)
        self.runlog.event("jev", frame_id=obs.frame_id, scene_frame=ctx.scene.frame_id,
                          action="plant" if place else "wait", reason=selected.reason,
                          selector_source=selected.source, selector_latency=selected.latency,
                          selector_error=selected.error, scores=selected.scores,
                          guidance=ctx.directive.model_dump(), budget_spent=self.directive_spent,
                          placement=place.model_dump() if place else None, rejected=rejected)
        if not rejected:
            self._selected_context = ctx
        return None if rejected else place

    async def plan_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.plan_interval if self.directive else min(1.0, self.settings.plan_interval))
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                continue
            if self.looker is not None and self.latest_scene is None:
                continue  # split mode decides from a scene; none yet
            pause = self.settings.pause_for_planner
            generation = self.episode_generation
            if pause:
                async with self._action_lock:
                    self.paused = True
                    await self.send("pause", obs, reason="planner thinking")
            try:
                result = await self._plan(obs, jpeg)
            finally:
                if pause:
                    await self.send("resume", obs, reason="planner done")
                    self.paused = False
            self.runlog.event(
                "plan", frame_id=obs.frame_id, mode=self.settings.planner_mode, latency=result.latency,
                error=result.error, request_id=result.request_id, input_tokens=result.input_tokens,
                output_tokens=result.output_tokens, plan=result.plan.model_dump() if result.plan else None,
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
        if self.settings.planner_mode == "end_to_end" or self.looker is None or self.latest_scene is None:
            return await self.planner.plan(jpeg, obs, knowledge)
        reference = jpeg if self.settings.planner_send_image else None
        context = {"plants": [{"row": r, "col": c, "plants": plants}
                              for (r, c), plants in sorted(self.lawn().items())],
                   "current_guidance": self.directive.model_dump() if self.directive else None,
                   "spent_under_guidance": self.directive_spent}
        return await self.planner.decide(self.latest_scene, knowledge, reference, context=context)

    def close(self) -> None:
        self.runlog.event("session_closed")
        self.runlog.close()
