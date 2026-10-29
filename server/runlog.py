"""Per-session run log: events.jsonl plus sampled frames, under a disk cap."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class RunLog:
    def __init__(self, root: Path, session_id: str, frame_interval: float, max_bytes: int) -> None:
        self.dir = root / session_id
        (self.dir / "frames").mkdir(parents=True, exist_ok=True)
        self._events = (self.dir / "events.jsonl").open("a", buffering=1)
        self.frame_interval = frame_interval
        self.max_bytes = max_bytes
        self.bytes_written = 0
        self._last_frame_t = 0.0
        self.frames_dropped_for_cap = 0

    def event(self, kind: str, **fields: Any) -> None:
        line = json.dumps({"t_server": time.time(), "kind": kind, **fields}, default=str)
        self._events.write(line + "\n")
        self.bytes_written += len(line) + 1

    def maybe_save_frame(self, frame_id: int, jpeg: bytes, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last_frame_t < self.frame_interval:
            return False
        if self.bytes_written + len(jpeg) > self.max_bytes:
            self.frames_dropped_for_cap += 1
            return False
        (self.dir / "frames" / f"{frame_id:08d}.jpg").write_bytes(jpeg)
        self.bytes_written += len(jpeg)
        self._last_frame_t = now
        return True

    def close(self) -> None:
        self._events.close()
