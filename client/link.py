"""Client side of the protocol: streams frames up, executes commands coming down.

A concrete client supplies three things: grab() -> JPEG bytes, execute(cmd), and
optionally read_state() for the judge. See mock_client.py and windows_client.py.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from client.model_activity import ModelActivity

from common.protocol import (
    Command,
    EpisodeEvent,
    ExecReport,
    FrameHeader,
    Hello,
    ModelTiming,
    StateMsg,
    dumps,
    pack_frame,
)

log = logging.getLogger("pvz.client")

Grab = Callable[[], tuple[bytes, int, int]]  # -> (jpeg, width, height)
Execute = Callable[[Command], Awaitable[dict[str, Any] | None]]  # -> ground-truth outcome, if known
ReadState = Callable[[], dict[str, Any] | None]


class ServerLink:
    def __init__(
        self,
        url: str,
        token: str,
        client_id: str,
        grab: Grab,
        execute: Execute,
        read_state: ReadState | None = None,
        fps: float = 20.0,
        state_hz: float = 2.0,
        game_version: str | None = None,
        playbook: str | None = None,
    ) -> None:
        self.url, self.token, self.client_id = url, token, client_id
        self.grab, self.execute, self.read_state = grab, execute, read_state
        self.fps, self.state_hz, self.game_version = fps, state_hz, game_version
        self.playbook = playbook  # requested; replaced by the version the server loaded
        self.frame_id = 0
        self.latencies: list[float] = []  # capture -> command executed, client clock
        self.session_id: str | None = None
        self.bytes_sent = 0
        self.bytes_received = 0
        self.rtt_ms: float | None = None
        self.frame_times: collections.deque[float] = collections.deque(maxlen=200)
        self.model_timings: dict[str, dict] = {}
        self.model_activity = ModelActivity()
        self.telemetry_supported = False
        self.game_outcome = None
        self.terminal_state = None

    def check_terminal(self):
        data = self.read_state() if self.read_state else None
        if (data and data.get("game") == "real_pvz" and data.get("outcome") in {"win", "lose"}
                and data.get("outcome_source") == "goty-1096-readonly-result"):
            self.game_outcome = data["outcome"]
            self.terminal_state = data
        return self.game_outcome

    async def _send(self, ws, data: str | bytes) -> None:
        await ws.send(data)
        self.bytes_sent += len(data.encode("utf-8") if isinstance(data, str) else data)

    def _received(self, data: str | bytes) -> None:
        self.bytes_received += len(data.encode("utf-8") if isinstance(data, str) else data)

    async def _measure_rtt(self, ws) -> None:
        while True:
            pong = await ws.ping()
            try:
                self.rtt_ms = 1000 * await asyncio.wait_for(pong, timeout=5)
            except TimeoutError:
                self.rtt_ms = None
            await asyncio.sleep(2)

    async def run(self, duration: float | None = None, done: Callable[[], bool] | None = None) -> None:
        """Play until `duration` seconds pass or `done()` (e.g. the game was won or lost) is true."""
        # The tunnel can stall for seconds on a weak network; 10 s (the default) gave up on handshakes that arrived.
        async with websockets.connect(self.url, max_size=None, open_timeout=30) as ws:
            await self._send(ws, dumps(Hello(token=self.token, client_id=self.client_id,
                                      game_version=self.game_version, playbook=self.playbook, telemetry=True)))
            raw = await ws.recv()
            self._received(raw)
            welcome = json.loads(raw)
            self.telemetry_supported = welcome.get("telemetry", False)
            self.session_id, self.playbook = welcome["session_id"], welcome["playbook"]
            log.info("connected, session %s, playbook %s", self.session_id, self.playbook)
            await self._send(ws, dumps(EpisodeEvent(event="start")))
            tasks = [asyncio.create_task(self._send_frames(ws)),
                     asyncio.create_task(self._recv_commands(ws)),
                     asyncio.create_task(self._measure_rtt(ws))]
            if self.read_state:
                tasks.append(asyncio.create_task(self._send_state(ws)))
            try:
                if duration is None and done is None:
                    await asyncio.gather(*tasks)
                else:
                    end = time.monotonic() + (duration if duration is not None else float("inf"))
                    while time.monotonic() < end and not self.game_outcome and not (done and done()):
                        for task in tasks:
                            if task.done():
                                task.result()
                                return
                        await asyncio.sleep(0.25)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.model_activity.disconnect()
                if self.read_state and not self.game_outcome:
                    await asyncio.sleep(.1)  # allow the independent 20 Hz judge sampler to observe a terminal screen
                self.check_terminal()
                final = self.terminal_state or (self.read_state() if self.read_state else None)
                if final is not None:
                    await self._send(ws, dumps(StateMsg(t=time.time(), frame_id=self.frame_id, data=final)))
                await self._send(ws, dumps(EpisodeEvent(event="end", info={**self.latency_summary(),
                    "reason": "game_result" if self.game_outcome else "operator_or_disconnect",
                    "outcome": self.game_outcome})))

    async def _send_frames(self, ws) -> None:
        """At most one frame in flight: the next screenshot is taken only after the server has the previous one.

        The SSH tunnel buffers megabytes, so a link slower than fps x frame size otherwise queues stale frames
        for many seconds and every click (suns above all) lands where things used to be. A ping sent after a
        frame is answered only once the server has received that frame, so its pong is the acknowledgement.
        """
        interval = 1.0 / self.fps
        delivered = None
        while True:
            if self.check_terminal():
                return
            if delivered is not None:
                await delivered
            start = time.monotonic()
            self.frame_id += 1
            t_capture = time.time()
            jpeg, w, h = await asyncio.to_thread(self.grab)
            header = FrameHeader(frame_id=self.frame_id, t_capture=t_capture, width=w, height=h)
            await self._send(ws, pack_frame(header, jpeg))
            delivered = await ws.ping()
            self.frame_times.append(time.monotonic())
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - start)))

    def upload_fps(self, window_s: float = 5.0) -> float:
        """Frames the server actually took per second recently; below fps when the network is the limit."""
        now = time.monotonic()
        recent = [t for t in self.frame_times if now - t <= window_s]
        return len(recent) / window_s

    async def _send_state(self, ws) -> None:
        while True:
            data = await asyncio.to_thread(self.read_state)
            if data is not None:
                await self._send(ws, dumps(StateMsg(t=time.time(), frame_id=self.frame_id, data=data)))
            await asyncio.sleep(1.0 / self.state_hz)

    async def _recv_commands(self, ws) -> None:
        async for raw in ws:
            self._received(raw)
            if json.loads(raw).get("type") == "model_timing":
                timing = ModelTiming.model_validate_json(raw)
                self.model_timings[timing.stage] = timing.model_dump()
                self.model_activity.update(timing)
                continue
            cmd = Command.model_validate_json(raw)
            error, result = None, None
            try:
                if self.check_terminal():
                    raise RuntimeError("game finished; no more player input")
                result = await self.execute(cmd)
            except Exception as e:  # report every failure back; never kill the stream
                error = f"{type(e).__name__}: {e}"
                log.warning("command %s failed: %s", cmd.cmd_id, error)
            t_exec = time.time()
            if cmd.t_capture is not None and error is None and cmd.action == "click":
                self.latencies.append(t_exec - cmd.t_capture)
            await self._send(ws, dumps(ExecReport(cmd_id=cmd.cmd_id, frame_id=cmd.frame_id, t_exec=t_exec,
                                           ok=error is None, error=error, result=result)))

    def latency_summary(self) -> dict[str, Any]:
        if not self.latencies:
            return {"commands": 0}
        xs = sorted(self.latencies)
        return {
            "commands": len(xs),
            "latency_median_ms": round(statistics.median(xs) * 1000, 1),
            "latency_p95_ms": round(xs[math.ceil(0.95 * len(xs)) - 1] * 1000, 1),
            "latency_max_ms": round(xs[-1] * 1000, 1),
        }
