"""Model call lifecycle and a rolling 60-second start count, using the local clock."""
from collections import deque
import threading
import time

from common.protocol import ModelTiming


class ModelActivity:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._stages = {}
        self._frozen_at = None

    def freeze(self):
        with self._lock:
            if self._frozen_at is None:
                self._frozen_at = self.clock()

    def update(self, event: ModelTiming):
        now = self.clock()
        with self._lock:
            s = self._stages.setdefault(event.stage, dict(
                status="idle", count=0, last_start=None, latency_ms=None, starts=deque(),
                interval_s=None, reason="等待触发", call_id=0))
            if event.call_id and event.call_id < s["call_id"]:
                return
            new_call = event.call_id > s["call_id"] or (event.call_id == 0 and event.status != "idle")
            if new_call:
                s["count"] = event.call_id or s["count"] + 1
                s["call_id"] = event.call_id
                s["last_start"] = now if event.status == "running" else now - event.latency_ms / 1000
                s["starts"].append(s["last_start"])
            s.update(status=event.status, interval_s=event.interval_s, reason=event.reason)
            if event.status in {"completed", "failed"}:
                s["latency_ms"] = event.latency_ms

    def disconnect(self):
        with self._lock:
            if self._frozen_at is None:
                self._frozen_at = self.clock()
            for s in self._stages.values():
                if s["status"] == "running":
                    s.update(status="failed", reason="连接已结束；未收到调用结果", latency_ms=None)

    def snapshot(self, stage):
        with self._lock:
            now = self.clock() if self._frozen_at is None else self._frozen_at
            s = self._stages.get(stage)
            if s is None:
                return dict(status="idle", count=0, recent_count=0, age_s=None,
                            latency_ms=None, interval_s=None, reason="等待触发")
            while s["starts"] and s["starts"][0] <= now - 60:
                s["starts"].popleft()
            return {**{k: v for k, v in s.items() if k != "starts"},
                    "recent_count": len(s["starts"]),
                    "age_s": None if s["last_start"] is None else max(0, now - s["last_start"])}


def activity_lines(label, sample):
    names = dict(idle="未触发", running="正在推理", completed="完成", failed="失败")
    status = sample["status"]
    if status == "idle" and sample["count"]:
        names["idle"] = "本轮未触发"
    detail = sample["reason"]
    if status == "running" and sample["age_s"] is not None:
        detail = f"已等待 {sample['age_s']:.1f}s"
    elif status in {"completed", "failed"} and sample["latency_ms"] is not None:
        detail = f"{sample['latency_ms']:.0f} ms" + (f" · {detail}" if detail else "")
    first = f"{label}：{names[status]}" + (f" · {detail}" if detail else "")
    age = "—" if sample["age_s"] is None else f"{sample['age_s']:.0f}s"
    interval = "" if sample["interval_s"] is None else f" · 轮询 {sample['interval_s']:g}s"
    second = f"累计 {sample['count']} 次 · 近60s {sample['recent_count']} 次 · 距触发 {age}{interval}"
    return [first, second]
