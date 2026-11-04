"""A research bridge whose SSH link died frees WORKER.lock by itself; the next one waits for it."""
import asyncio
import io
import json
import threading
import time

import pytest

from common.file_lock import file_lock
from rsi import native_bridge, native_loop


class Stdin:
    """Lines first, then blocks like a dead SSH link until the bridge 'exits' (or a time limit)."""
    def __init__(self, lines, limit):
        self.lines, self.limit, self.closed = list(lines), limit, threading.Event()

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        self.closed.wait(self.limit)
        return ""


def run_serve(monkeypatch, lines, handle, idle_exit, limit=3.0):
    stdin, exits, out = Stdin(lines, limit), [], io.StringIO()
    def fake_exit(code):
        exits.append(time.monotonic())
        stdin.closed.set()  # the real os._exit ends the process here
    monkeypatch.setattr(native_bridge.sys, "stdin", stdin)
    monkeypatch.setattr(native_bridge.sys, "stdout", out)
    monkeypatch.setattr(native_bridge.os, "_exit", fake_exit)
    class Bridge:
        async def handle(self, request):
            return await handle(request)
    asyncio.run(native_bridge.serve(Bridge(), idle_exit=idle_exit))
    return exits, [json.loads(line) for line in out.getvalue().splitlines()]


def test_heartbeats_get_no_reply_and_a_silent_idle_bridge_exits(monkeypatch):
    async def handle(request):
        return "pong"
    started = time.monotonic()
    exits, replies = run_serve(monkeypatch, ['{"op": "ping"}\n', '{"op": "next", "rpc_id": "a"}\n'], handle, .3)
    assert replies == [{"ok": True, "result": "pong", "rpc_id": "a"}]  # the ping was not answered
    assert exits and exits[0] - started >= .3


def test_a_long_request_is_not_idleness(monkeypatch):
    finished = []
    async def handle(request):
        await asyncio.sleep(1.0)  # a Coach step: no client lines meanwhile
        finished.append(time.monotonic())
        return None
    exits, replies = run_serve(monkeypatch, ['{"op": "prepare", "rpc_id": "b"}\n'], handle, .3)
    assert replies[0]["rpc_id"] == "b" and exits and exits[0] >= finished[0]


def test_lock_timeout_waits_for_the_old_holder_then_gives_up(tmp_path):
    lock = tmp_path / "WORKER.lock"
    released = threading.Event()
    def hold(seconds):
        with file_lock(lock, blocking=False):
            released.wait(seconds)
    holder = threading.Thread(target=hold, args=(1.5,))
    holder.start()
    time.sleep(.2)
    with pytest.raises(OSError):
        with file_lock(lock, timeout=.5):
            pass
    started = time.monotonic()
    with file_lock(lock, timeout=10):
        assert time.monotonic() - started >= .8  # got it once the holder let go
    holder.join()


def test_ssh_bridge_heartbeat_writes_unanswered_pings(monkeypatch):
    class Process:
        def __init__(self):
            self.stdin, self.alive = io.StringIO(), True
        def poll(self):
            return None if self.alive else 0
    class RPC:
        writer, failure = threading.Lock(), None
    bridge = native_loop.SSHBridge.__new__(native_loop.SSHBridge)
    bridge.process, bridge.rpc = Process(), RPC()
    monkeypatch.setattr(native_loop, "HEARTBEAT_S", .05)
    beat = threading.Thread(target=bridge._heartbeat, daemon=True)
    beat.start()
    time.sleep(.3)
    bridge.process.alive = False
    beat.join(1)
    lines = bridge.process.stdin.getvalue().splitlines()
    assert len(lines) >= 2 and all(json.loads(line) == {"op": "ping"} for line in lines)


def test_different_jobs_run_side_by_side_but_one_job_stays_in_order(monkeypatch):
    spans = {}
    async def handle(request):
        start = time.monotonic()
        await asyncio.sleep(.4)  # a Coach step waiting on the model
        spans[request["rpc_id"]] = (start, time.monotonic())
        return request["rpc_id"]
    lines = ['{"op": "prepare", "job_id": "a", "rpc_id": "a1"}\n', '{"op": "prepare", "job_id": "b", "rpc_id": "b1"}\n',
             '{"op": "report", "job_id": "a", "rpc_id": "a2"}\n']
    exits, replies = run_serve(monkeypatch, lines, handle, idle_exit=.3, limit=1.5)
    assert sorted(r["rpc_id"] for r in replies) == ["a1", "a2", "b1"]
    assert spans["b1"][0] < spans["a1"][1]  # job b did not wait for job a
    assert spans["a2"][0] >= spans["a1"][1]  # job a's second request waited for its first
