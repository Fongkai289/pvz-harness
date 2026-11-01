"""Bounded, read-only operator projection of player logs. Never reads judge state."""
import json
from pathlib import Path

FIELDS = {
    "sun_collection": ("frame_id", "detected", "confirmed", "clicks_sent", "total_clicks", "reason"),
    "plan": ("frame_id", "plan", "error", "trigger_reasons"),
    "jev": ("frame_id", "decision_id", "action", "reason", "placement", "rejected",
            "selector_error", "blocked_reasons", "budget_spent", "guidance", "decision_context"),
    "zombies": ("frame_id", "scene", "error"),
    "threats": ("rows", "frame_id"),
    "replan_trigger": ("reasons",),
    "cmd": ("cmd_id", "action", "x", "y", "reason", "meta"),
    "exec": ("cmd_id", "ok", "error"),
}


class DecisionView:
    def __init__(self, root: Path):
        self.root = root
        self._key = None
        self._value = None

    def latest(self, active_sessions):
        live = sorted(k for k in active_sessions if "-windows-" in k)
        paths = ([self.root / live[-1] / "events.jsonl"] if live else
                 sorted(self.root.glob("*-windows-*/events.jsonl"), reverse=True)[:1])
        if not paths:
            return {"status": "not_started"}
        path = paths[0]
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key != self._key:
            result = {"session": path.parent.name, "events": {}, "history": [], "commands": []}
            with path.open("rb") as stream:
                start = max(0, stat.st_size - 2 * 1024 * 1024)
                stream.seek(start)
                if start:
                    stream.readline()
                for line in stream.read(2 * 1024 * 1024).splitlines():
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue  # a concurrently written trailing event can be partial
                    kind = event.get("kind")
                    if kind not in FIELDS:
                        continue
                    item = {k: event.get(k) for k in FIELDS[kind]}
                    item.update(kind=kind, t_server=event.get("t_server"))
                    result["events"][kind] = item
                    if kind == "jev":
                        result["history"] = (result["history"] + [item])[-8:]
                    if kind == "cmd":
                        result["commands"] = (result["commands"] + [item])[-6:]
            self._key, self._value = key, result
        return {**self._value, "status": "live" if live else "closed"}
