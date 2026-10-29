"""One connected game client: receives frames, runs the layers, sends clicks back.

Three loops run per session:
  look  (split mode, every look_interval s): screenshot -> scene (code + Qwen)
  fast  (every fast_interval s): sun clicks; Jev picks a named action from the latest scene; code acts
  plan  (every plan_interval s): the planner decides from the scene (split) or the screenshot (end_to_end)
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
from rules import hard_rules
from rules.hard_rules import PLANT_COSTS
from server.decider import Judge, JevContext, choose
from server.judge import Judge
from server.perception import Observation, Perception, decode_jpeg
from server.planner import Placement, Planner, PlanResult
from server.policy import FastPolicy
from server.runlog import RunLog
from server.scene import QwenLooker, Scene

log = logging.getLogger(__name__)

STALE_FRAME_SECONDS = 2.0  # stop acting when no fresh frame arrived for this long


class Session:
    def __init__(self, ws: WebSocket, settings: Settings, planner: Planner, judge: Judge,
                 playbook: Playbook, session_id: str, looker: QwenLooker | None = None) -> None:
        self.ws = ws
        self.settings = settings
        self.planner = planner
        self.jev_judge = judge  # Jev's model layer (yes/no judgments), not the ground-truth Judge below
        self.looker = looker  # split mode only
        self.playbook = playbook
        self.plan_note = ""
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
        if self.latest_obs is None or time.monotonic() - self.latest_recv > STALE_FRAME_SECONDS:
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
            look = await self.looker.look(decode_jpeg(jpeg), obs.frame_id, self.settings.loadout, obs.t_capture)
            # The look step's own record: the judge can check it against ground truth, field by field.
            self.runlog.event("scene", frame_id=obs.frame_id, latency=look.latency, error=look.error,
                              input_tokens=look.input_tokens, output_tokens=look.output_tokens,
                              scene=look.scene.model_dump() if look.scene else None)
            if look.scene is not None:
                self.latest_scene = look.scene
                self.used_slots.clear()  # the new scene shows those cards' real state
                self.sun_spent = 0  # and the real sun count

    async def plants_loop(self) -> None:
        """Read the plants on the lawn, slower than zombies: plants change less often and the read is slower."""
        if self.looker is None:
            return
        while True:
            await asyncio.sleep(self.settings.plants_interval)
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None or self.paused:
                continue
            seen = await self.looker.look_plants(decode_jpeg(jpeg), self.settings.loadout)
            self.runlog.event("plants", frame_id=obs.frame_id, latency=seen.latency, error=seen.error,
                              output_tokens=seen.output_tokens,
                              plants=None if seen.plants is None else
                              {f"{r},{c}": v for (r, c), v in sorted(seen.plants.items())})
            if seen.plants is not None:
                self.seen_plants, self.seen_plants_frame = seen.plants, obs.frame_id
                self.placed_since = [p for p in self.placed_since if p[0] > obs.frame_id]

    def lawn(self) -> dict[tuple[int, int], list[str]]:
        """The lawn as last seen, plus what the agent placed after that snapshot."""
        cells = {cell: list(plants) for cell, plants in self.seen_plants.items()}
        for _, row, col, plant in self.placed_since:
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
            place = await self._jev_step(obs)
            for click in self.policy.tick(obs, place=place):
                await self.send("click", obs, meta=click.meta, x=click.x, y=click.y, reason=click.reason)
            if place is not None:
                card = self.settings.loadout[place.slot] if place.slot < len(self.settings.loadout) else "?"
                self.placed_since.append((obs.frame_id, place.row, place.col, card))
                self.used_slots.add(place.slot)
                self.sun_spent += PLANT_COSTS.get(card, 0)

    async def _jev_step(self, obs: Observation) -> Placement | None:
        scene = self.latest_scene
        lawn = self.lawn()
        if scene is None:  # end_to_end mode: no scene, so follow the plan; only the cell rule applies
            place = self.policy.next_placement()
            self.policy.drop_next()
            return place if place is None or (place.row, place.col) not in lawn else None
        self.policy.set_rows(scene.rows or 5)
        ctx = JevContext(scene=scene, plan_next=self.policy.next_placement(),
                         planted=lawn, used_slots=self.used_slots)
        judgments = await self.jev_judge.judge(ctx, self.playbook.jev)  # layer 1: model, yes/no only
        action, reason, place = choose(ctx, self.playbook.jev, judgments)  # layer 2: playbook tactics
        if action == "follow_plan":
            self.policy.drop_next()  # used or rejected, it is not retried
        rejected = None
        if place is not None:  # layer 3: hard rules, whoever proposed the placement
            rejected = hard_rules.check(place, scene, lawn, self.used_slots, self.sun_spent)
        self.runlog.event("jev", frame_id=obs.frame_id, scene_frame=scene.frame_id, action=action,
                          reason=reason, tough=judgments.tough, judge_source=judgments.source,
                          judge_latency=judgments.latency, judge_error=judgments.error,
                          placement=place.model_dump() if place else None, rejected=rejected)
        return None if rejected else place

    async def plan_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.plan_interval)
            obs, jpeg = self._fresh_obs(), self.latest_jpeg
            if obs is None or jpeg is None:
                continue
            if self.looker is not None and self.latest_scene is None:
                continue  # split mode decides from a scene; none yet
            pause = self.settings.pause_for_planner
            if pause:
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
            if result.plan is not None:
                rows = self.latest_scene.rows if self.latest_scene and self.latest_scene.rows else 5
                self.policy.set_plan(result.plan, rows=rows)
                self.plan_note = result.plan.note

    async def _plan(self, obs: Observation, jpeg: bytes) -> PlanResult:
        knowledge = self.playbook.planner.knowledge
        if self.looker is None or self.latest_scene is None:
            return await self.planner.plan(jpeg, obs, knowledge)
        reference = jpeg if self.settings.planner_send_image else None
        return await self.planner.decide(self.latest_scene, knowledge, reference)

    def close(self) -> None:
        self.runlog.event("session_closed")
        self.runlog.close()
