"""Post-game reviews and durable native RSI queue; evaluation owns promotion."""
import asyncio
import json
from pathlib import Path

from rsi.review_live import review, format_review
from rsi.code_store import write_json
from rsi.native_jobs import NativeJobs

CLEAN_WIN = "won without a lawn mower: nothing to review"
RESULT_TAIL_BYTES = 262144  # the game's end event is among the last lines of its log


def needs_improvement(outcome, mowers_used):
    """A game is reviewed (and may queue a training round) only if it cost a lawn mower (every loss does).

    A win without a mower needs no review (operator's call, 2026-11-04: a Coach review costs ~110k tokens).
    An unknown mower count is reviewed.
    """
    return outcome != "win" or not isinstance(mowers_used, int) or mowers_used > 0


class LiveReviews:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.data_dir / "live_reviews"
        self.tasks = {}
        self.gate = asyncio.Semaphore(1)
        self.learning = NativeJobs(settings)
        self._results = {}  # session -> ((log size, mtime), result)

    def status(self, session):
        path = self.root / session / "status.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "not_triggered"}
        return {**data, "result": self.result(session)}

    def result(self, session):
        """How the game ended (outcome, mowers used, level), read from the end of its event log.

        Shown beside the review state, which says nothing about the game itself. Cached until the log changes.
        """
        path = self.settings.runs_dir / session / "events.jsonl"
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (stat.st_size, stat.st_mtime_ns)
        cached = self._results.get(session)
        if cached and cached[0] == key:
            return cached[1]
        with path.open("rb") as stream:
            stream.seek(max(0, stat.st_size - RESULT_TAIL_BYTES))
            tail = stream.read()
        found = None
        for line in reversed(tail.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue  # the cut first line of the tail
            if event.get("kind") == "episode" and event.get("event") == "end":
                final = (event.get("summary") or {}).get("final_state") or {}
                found = {"outcome": final.get("outcome"), "mowers": final.get("mowers_used"), "level": final.get("level")}
                break
        self._results[session] = (key, found)
        return found

    def publish(self, session, data):
        folder = self.root / session
        folder.mkdir(parents=True, exist_ok=True)
        data = {"session": session, **data}
        write_json(folder / "status.json", data)
        latest = self.root / "LATEST.json"
        if not latest.exists() or session >= json.loads(latest.read_text(encoding="utf-8"))["session"]:
            write_json(latest, {"session": session})

    def latest(self):
        path = self.root / "LATEST.json"
        if not path.exists():
            return {"status": "not_triggered"}
        return self.status(json.loads(path.read_text(encoding="utf-8"))["session"])

    def recent(self, limit=8):
        """The newest games' reviews first, each as status() gives it (the record once completed)."""
        sessions = sorted((path.parent.name for path in self.root.glob("*/status.json")), reverse=True)
        return [self.status(session) for session in sessions[:limit]]

    def enqueue(self, session, improve=True):
        if session in self.tasks or self.status(session).get("status") in ("completed", "skipped"):
            return
        if not improve:
            self.publish(session, {"status": "skipped", "improve": False, "reason": CLEAN_WIN})
            return
        self.publish(session, {"status": "queued", "improve": improve})
        task = asyncio.create_task(self._run(session, improve))
        self.tasks[session] = task
        task.add_done_callback(lambda _: self.tasks.pop(session, None))

    async def _run(self, session, improve=True):
        try:
            async with self.gate:
                self.publish(session, {"status": "reviewing", "improve": improve})
                record, output = await review(self.settings.runs_dir / session / "events.jsonl", self.settings)
                requested = self.settings.auto_native_rsi and improve
                self.publish(session, {"status": "completed", "text": format_review(record),
                                       "review": record, "artifact": str(output),
                                       "native_rsi_requested": requested,
                                       **({} if improve else {"native_rsi_skipped": "won without a lawn mower"})})
                if requested:
                    self.learning.enqueue(session)
        except asyncio.CancelledError:
            self.publish(session, {"status": "queued", "improve": improve})
            raise
        except Exception as exc:
            self.publish(session, {"status": "failed", "error": type(exc).__name__})

    def recover(self):
        # Previously queued jobs have already passed Session's explicit result gate.
        for path in self.root.glob("*/status.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") in {"queued", "reviewing"}:
                self.enqueue(path.parent.name, improve=data.get("improve", True))
            elif data.get("status") == "completed" and data.get("native_rsi_requested") and self.settings.auto_native_rsi:
                self.learning.enqueue(path.parent.name)
