"""Client side of the protocol: streams frames up, executes commands coming down.

A concrete client supplies three things: grab() -> JPEG bytes, execute(cmd), and
optionally read_state() for the judge. See mock_client.py and windows_client.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from common.protocol import (
    Command,
    EpisodeEvent,
    ExecReport,
    FrameHeader,
    Hello,
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

    async def run(self, duration: float | None = None, done: Callable[[], bool] | None = None) -> None:
        """Play until `duration` seconds pass or `done()` (e.g. the game was won or lost) is true."""
        async with websockets.connect(self.url, max_size=None) as ws:
            await ws.send(dumps(Hello(token=self.token, client_id=self.client_id,
                                      game_version=self.game_version, playbook=self.playbook)))
            welcome = json.loads(await ws.recv())
            self.session_id, self.playbook = welcome["session_id"], welcome["playbook"]
            log.info("connected, session %s, playbook %s", self.session_id, self.playbook)
            await ws.send(dumps(EpisodeEvent(event="start")))
            tasks = [asyncio.create_task(self._send_frames(ws)),
                     asyncio.create_task(self._recv_commands(ws))]
            if self.read_state:
                tasks.append(asyncio.create_task(self._send_state(ws)))
            try:
                if duration is None and done is None:
                    await asyncio.gather(*tasks)
                else:
                    end = time.monotonic() + (duration if duration is not None else float("inf"))
                    while time.monotonic() < end and not (done and done()):
                        await asyncio.sleep(0.25)
            finally:
                for t in tasks:
                    t.cancel()
                if self.read_state and (final := self.read_state()) is not None:
                    await ws.send(dumps(StateMsg(t=time.time(), frame_id=self.frame_id, data=final)))
                await ws.send(dumps(EpisodeEvent(event="end", info=self.latency_summary())))

    async def _send_frames(self, ws) -> None:
        interval = 1.0 / self.fps
        while True:
            start = time.monotonic()
            self.frame_id += 1
            t_capture = time.time()
            jpeg, w, h = await asyncio.to_thread(self.grab)
            header = FrameHeader(frame_id=self.frame_id, t_capture=t_capture, width=w, height=h)
            await ws.send(pack_frame(header, jpeg))
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - start)))

    async def _send_state(self, ws) -> None:
        while True:
            data = await asyncio.to_thread(self.read_state)
            if data is not None:
                await ws.send(dumps(StateMsg(t=time.time(), frame_id=self.frame_id, data=data)))
            await asyncio.sleep(1.0 / self.state_hz)

    async def _recv_commands(self, ws) -> None:
        async for raw in ws:
            cmd = Command.model_validate_json(raw)
            error, result = None, None
            try:
                result = await self.execute(cmd)
            except Exception as e:  # report every failure back; never kill the stream
                error = f"{type(e).__name__}: {e}"
                log.warning("command %s failed: %s", cmd.cmd_id, error)
            t_exec = time.time()
            if cmd.t_capture is not None:
                self.latencies.append(t_exec - cmd.t_capture)
            await ws.send(dumps(ExecReport(cmd_id=cmd.cmd_id, frame_id=cmd.frame_id, t_exec=t_exec,
                                           ok=error is None, error=error, result=result)))

    def latency_summary(self) -> dict[str, Any]:
        if not self.latencies:
            return {"commands": 0}
        xs = sorted(self.latencies)
        return {
            "commands": len(xs),
            "latency_median_ms": round(statistics.median(xs) * 1000, 1),
            "latency_p95_ms": round(xs[int(0.95 * (len(xs) - 1))] * 1000, 1),
            "latency_max_ms": round(xs[-1] * 1000, 1),
        }
