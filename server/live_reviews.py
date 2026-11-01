"""Post-game reviews and durable native RSI queue; evaluation owns promotion."""
import asyncio
import json
from pathlib import Path

from rsi.review_live import review, format_review
from rsi.code_store import write_json
from rsi.native_jobs import NativeJobs


class LiveReviews:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.data_dir / "live_reviews"
        self.tasks = {}
        self.gate = asyncio.Semaphore(1)
        self.learning = NativeJobs(settings)

    def status(self, session):
        path = self.root / session / "status.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "not_triggered"}

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

    def enqueue(self, session):
        if session in self.tasks or self.status(session).get("status") == "completed":
            return
        self.publish(session, {"status": "queued"})
        task = asyncio.create_task(self._run(session))
        self.tasks[session] = task
        task.add_done_callback(lambda _: self.tasks.pop(session, None))

    async def _run(self, session):
        try:
            async with self.gate:
                self.publish(session, {"status": "reviewing"})
                record, output = await review(self.settings.runs_dir / session / "events.jsonl", self.settings)
                self.publish(session, {"status": "completed", "text": format_review(record),
                                       "review": record, "artifact": str(output),
                                       "native_rsi_requested": self.settings.auto_native_rsi})
                if self.settings.auto_native_rsi:
                    self.learning.enqueue(session)
        except asyncio.CancelledError:
            self.publish(session, {"status": "queued"})
            raise
        except Exception as exc:
            self.publish(session, {"status": "failed", "error": type(exc).__name__})

    def recover(self):
        # Previously queued jobs have already passed Session's explicit result gate.
        for path in self.root.glob("*/status.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") in {"queued", "reviewing"}:
                self.enqueue(path.parent.name)
            elif data.get("status") == "completed" and data.get("native_rsi_requested") and self.settings.auto_native_rsi:
                self.learning.enqueue(path.parent.name)
