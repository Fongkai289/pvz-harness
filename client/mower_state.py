"""Judge-only mower telemetry. ReadProcessMemory only; no injection or writes.

Only the verified English GOTY 1.2.0.1096 image is supported. See docs/mower-evaluation.md.
Game ticks are simulation time (100 Hz), independent of wall time and pauses.
"""
from __future__ import annotations

import copy
import ctypes
from ctypes import wintypes
import logging
import struct
import threading

log = logging.getLogger(__name__)


class ReadOnlyGame:
    def __init__(self, pid):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.api.ReadProcessMemory.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.OpenProcess(0x0010 | 0x1000, False, pid)  # VM_READ | QUERY_LIMITED_INFORMATION
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if self.read(0x400000, 2) != b"MZ":
                raise ValueError("unsupported game image")
            pe = self.u32(0x40003C)
            if not 64 <= pe <= 4096 or self.read(0x400000 + pe, 4) != b"PE\0\0":
                raise ValueError("unsupported PE header")
            if self.u32(0x400000 + pe + 8) != 0x4D02B058:
                raise ValueError("mower telemetry requires English GOTY 1.2.0.1096")
        except Exception:
            self.close()
            raise

    def read(self, address, size):
        if not 0x10000 <= address < 0x80000000 or not 0 < size <= 4096:
            raise ValueError("invalid bounded game read")
        buf = ctypes.create_string_buffer(size)
        done = ctypes.c_size_t()
        if not self.api.ReadProcessMemory(self.handle, address, buf, size, ctypes.byref(done)) or done.value != size:
            raise ctypes.WinError(ctypes.get_last_error())
        return buf.raw

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def sample(self):
        app = self.u32(0x731C50)
        game_ui, game_mode = self.u32(app + 0x91C), self.u32(app + 0x918)
        board = self.u32(app + 0x868)
        if not board:
            return None
        tick = self.u32(board + 0x5580)
        scene = self.u32(board + 0x5564)
        count = self.u32(board + 0x11C)
        if tick > 100 * 86400 or scene > 5 or count > 32:
            raise ValueError("unexpected board layout")
        base = self.u32(board + 0x118)
        mowers = []
        if count:
            data = self.read(base, count * 0x48)
            for i in range(count):
                off = i * 0x48
                row = struct.unpack_from("<i", data, off + 0x14)[0]
                state = struct.unpack_from("<i", data, off + 0x2C)[0]
                dead = bool(data[off + 0x30])
                if not 0 <= row < (6 if scene in (2, 3) else 5) or state not in (0, 1, 2, 3):
                    raise ValueError("unexpected mower layout")
                mowers.append(dict(row=row, state=state, dead=dead))
        if self.u32(app + 0x868) != board or self.u32(board + 0x5580) < tick:
            return None  # board changed while reading
        award = self.read(board + 0x5624, 1)[0]
        if award not in (0, 1) or game_ui > 7:
            raise ValueError("unexpected outcome layout")
        outcome = ("lose" if game_ui == 4 else "win" if game_ui == 5 or (game_ui == 3 and award) else None)
        # Only the verified Adventure layout/semantics; other modes can have intermediate awards.
        if game_mode != 0:
            outcome = None
        return dict(board=board, tick=tick, mowers=mowers, game_ui=game_ui, outcome=outcome,
                    level=self.u32(board + 0x5568), paused=bool(self.read(board + 0x17C, 1)[0]))

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class MowerTracker:
    def __init__(self):
        self.board = None
        self.tick = 0
        self.rows = {}
        self.events = {}
        self.first_tick = 0
        self.generation = 0
        self.outcome = None

    def update(self, sample):
        tick = sample["tick"]
        if sample["board"] != self.board or tick < self.tick:
            self.board, self.first_tick = sample["board"], tick
            self.rows, self.events = {}, {}
            self.generation += 1
            self.outcome = None
        if sample.get("outcome") in {"win", "lose"}:
            self.outcome = sample["outcome"]
        seconds = tick / 100
        for mower in sample["mowers"]:
            row, state = mower["row"], mower["state"]
            prev = self.rows.get(row)
            if state == 2 and row not in self.events:
                continuous = prev is not None and prev["state"] == 1 and not prev["dead"] and 0 <= tick - self.tick <= 100
                self.events[row] = dict(row=row, time_s=seconds if continuous else None,
                                        uncertainty_s=(tick - self.tick) / 100 if continuous else None)
            self.rows[row] = mower.copy()
        self.tick = tick
        return dict(game="real_pvz", game_time_s=seconds, level=sample["level"],
                    outcome=self.outcome, outcome_source="goty-1096-readonly-result",
                    board_id=f"{self.board:x}-{self.generation}", paused=sample["paused"],
                    mowers_used=len(self.events),
                    mower_events=list(self.events.values()),
                    mowers_ready=sum(m["state"] == 1 and not m["dead"] for m in sample["mowers"]),
                    mower_timing_coverage="from_start" if self.first_tick <= 1 else "partial",
                    mower_observation_start_s=self.first_tick / 100,
                    mower_time_source="goty-1096-readonly-game-ticks")


class MowerMonitor:
    """Sample even across websocket reconnects; only ServerLink reads the snapshot."""
    def __init__(self, pid):
        self.reader = ReadOnlyGame(pid)
        self.tracker = MowerTracker()
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.latest = None
        self.thread = threading.Thread(target=self._run, name="judge-mowers", daemon=True)
        self.thread.start()

    def _run(self):
        failed = False
        while not self.stop.is_set():
            try:
                sample = self.reader.sample()
                with self.lock:
                    self.latest = self.tracker.update(sample) if sample else dict(game="real_pvz", mower_available=False)
                failed = False
            except (OSError, ValueError):
                with self.lock:
                    self.latest = dict(game="real_pvz", mower_available=False)
                if not failed:
                    log.warning("mower telemetry unavailable; no score fabricated", exc_info=True)
                failed = True
            self.stop.wait(0.05)

    def read_state(self):
        with self.lock:
            return copy.deepcopy(self.latest)

    def close(self):
        self.stop.set()
        self.thread.join()
        self.reader.close()
