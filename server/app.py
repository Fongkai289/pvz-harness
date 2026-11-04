"""Server entry point: `uv run python -m server.app`."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hmac
import json
import logging
import re
import secrets
import time

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Request

from common.protocol import Hello, Welcome, dumps, parse_client_message
from server.config import Settings, load_settings, effective_settings, timing_values
from server.action_selector import ActionSelector, make_selector
from server.planner import Planner, make_planner
from server.playbook import PlaybookStore
from server.scene import QwenLooker
from server.session import Session
from server.live_reviews import LiveReviews, needs_improvement
from server.decision_view import DecisionView
from server.mechanics import catalog

log = logging.getLogger("pvz.server")

HELLO_TIMEOUT = 10.0
UPLOAD_PROBE_MAX = 2_000_000  # bytes accepted by the 测试连接 upload probe


def create_app(settings: Settings, planner: Planner | None = None,
               selector: ActionSelector | None = None, looker: QwenLooker | None = None) -> FastAPI:
    planner = planner or make_planner(settings)
    selector = selector or make_selector(settings)
    looker = looker or (QwenLooker(settings.qwen_url, settings.qwen_model, timeout=settings.planner_timeout)
              if settings.planner == "qwen" else None)
    store = PlaybookStore(settings.data_dir)
    app = FastAPI(title="pvz-agent server")
    app.state.sessions = {}
    reviews = LiveReviews(settings)
    app.state.reviews = reviews
    decisions = DecisionView(settings.runs_dir)

    def authorize(authorization):
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {settings.token}"):
            raise HTTPException(status_code=401, detail="authentication required")

    @app.on_event("startup")
    async def recover_reviews():
        reviews.recover()

    @app.on_event("shutdown")
    async def stop_reviews():
        tasks = list(reviews.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @app.get("/playbook")
    async def current_playbook(authorization: str | None = Header(default=None)):
        authorize(authorization)
        book = store.load()
        return {"current": book.version, "playbook": book.active_dump(settings.decider),
                "selector": settings.decider,
                "effective_timing": timing_values(effective_settings(settings, book))}

    @app.get("/reviews")
    async def recent_reviews(limit: int = 8, authorization: str | None = Header(default=None)):
        authorize(authorization)
        return reviews.recent(max(1, min(limit, 20)))

    @app.get("/reviews/latest")
    async def latest_review(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return reviews.latest()

    @app.get("/mechanics")
    async def game_mechanics(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return catalog()

    @app.get("/learning/latest")
    async def latest_learning(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return reviews.learning.latest()

    @app.get("/decisions/latest")
    async def latest_decisions(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return decisions.latest(app.state.sessions)

    @app.get("/coach/usage")
    async def coach_usage(authorization: str | None = Header(default=None)):
        authorize(authorization)
        from rsi.codex_queue import CoachQueue
        return {"provider": settings.coach_provider, "model": settings.coach_model,
                "rounds": await asyncio.to_thread(CoachQueue(settings.coach_queue_dir).dispatch, {"op":"stats"})}

    @app.post("/diagnostics")
    async def diagnostics(authorization: str | None = Header(default=None)):
        """控制中心 · 测试连接: one tiny text-only call to each model, timed here on the server."""
        authorize(authorization)
        from server.diagnostics import check_models
        return await check_models(settings)

    @app.post("/diagnostics/upload")
    async def upload_probe(request: Request, authorization: str | None = Header(default=None)):
        """Upload bandwidth through the tunnel: the client times sending this body."""
        authorize(authorization)
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > UPLOAD_PROBE_MAX:
                raise HTTPException(status_code=413, detail="upload probe too large")
        return {"bytes": size}

    @app.post("/seed_choice")
    async def seed_choice(body: dict, authorization: str | None = Header(default=None)):
        """Operator's 选植物: read the chooser screenshot, let Qwen pick, return the packets to click."""
        authorize(authorization)
        from server.seed_choice import choose, read_chooser
        if looker is None:
            raise HTTPException(status_code=503, detail="选卡需要 Qwen（当前不是 qwen planner）")
        try:
            data = base64.b64decode(str(body.get("jpeg", "")), validate=True)
            if len(data) > 2_000_000:
                raise ValueError("截图过大")
            image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("截图无法解码")
            chooser = read_chooser(image)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        try:
            result = await choose(looker, image, chooser, store.load().planner.knowledge)
        except Exception as exc:
            log.exception("seed choice failed")
            raise HTTPException(status_code=502, detail=f"Qwen 选卡失败：{exc}"[:300])
        record = {"time": time.time(), "slots": chooser.slots, "available": chooser.available, **result}
        folder = settings.data_dir / "seed_choices"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(record, ensure_ascii=False),
                                                                     encoding="utf-8")
        log.info("seed choice: %s (%s)", result["plants"], result["reason"])
        return result

    @app.get("/reviews/{session_id}")
    async def review_status(session_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            raise HTTPException(status_code=400, detail="invalid session")
        return reviews.status(session_id)

    @app.get("/health")
    async def health() -> dict:
        planner_model = {"claude": settings.planner_model, "qwen": settings.qwen_model}.get(settings.planner)
        return {
            "ok": True,
            "planner": settings.planner,
            "planner_model": planner_model,
            "planner_mode": settings.planner_mode,
            "planner_send_image": settings.planner_send_image if settings.planner != "mock" else None,
            "jev": settings.decider,
            "mechanics_version": catalog()["version"],
            "control_contract": "advisory-strategy-auto-suns-v4",
            "sun_collection": "code-two-frame-v1" if settings.decider == "onejev" else "rules-baseline",
            "mower_evaluation": "mower-time-v1",
            "automatic_coach": True,
            "coach_provider": settings.coach_provider,
            "coach_model": settings.coach_model if settings.coach_provider == "codex" else settings.qwen_model,
            "automatic_native_rsi": settings.auto_native_rsi,
            "learning_backend": "pvz_rl",
            "separate_zombie_vision": True,
            "danger_replan": store.load().timing.danger_replan,
            "danger_min_seconds": store.load().timing.danger_min_seconds,
            "model_telemetry": True,
            "model_activity": True,
            "planting_ready": looker is not None,
            "jev_model": settings.onejev_model if settings.decider == "onejev" else None,
            "loadout": settings.loadout,
            "playbook": store.current(),
            "effective_timing": timing_values(effective_settings(settings, store.load())),
            "sessions": list(app.state.sessions),
        }

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        try:
            hello = parse_client_message(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT))
        except Exception:
            await ws.close(code=1008, reason="expected hello")
            return
        if not isinstance(hello, Hello) or not hmac.compare_digest(hello.token, settings.token):
            await ws.close(code=1008, reason="bad hello or token")
            return

        try:
            playbook = store.load(hello.playbook)
        except (FileNotFoundError, ValueError) as e:
            await ws.close(code=1008, reason=f"bad playbook: {e}"[:120])
            return

        client = re.sub(r"[^A-Za-z0-9_-]", "_", hello.client_id)[:32]
        session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{client}-{secrets.token_hex(3)}"
        session = Session(ws, settings, planner, selector, playbook, session_id, looker)
        session.telemetry_enabled = hello.telemetry
        session.runlog.event("hello", **hello.model_dump(exclude={"type", "token"}),
                             control_contract="advisory-strategy-auto-suns-v4",
                             playbook_loaded=playbook.version,
                             effective_timing=timing_values(session.settings))
        app.state.sessions[session_id] = session
        await ws.send_text(dumps(Welcome(session_id=session_id, playbook=playbook.version,
                                         telemetry=hello.telemetry)))
        log.info("session %s connected", session_id)

        tasks = [asyncio.create_task(session.fast_loop()), asyncio.create_task(session.plan_loop()),
                 asyncio.create_task(session.look_loop()), asyncio.create_task(session.plants_loop()),
                 asyncio.create_task(session.zombies_loop()), asyncio.create_task(session.sun_loop())]
        try:
            await session.initialize_model_activity()
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await session.on_frame(msg["bytes"])
                elif msg.get("text") is not None:
                    await session.on_text(msg["text"])
        except WebSocketDisconnect:
            pass
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            session.close()
            if session.completed_outcome:
                reviews.enqueue(session_id, improve=needs_improvement(session.completed_outcome,
                                                                      session.completed_mowers))
            app.state.sessions.pop(session_id, None)
            log.info("session %s closed", session_id)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    log.info("planner=%s send_image=%s jev=%s listening on %s:%d", settings.planner,
             settings.planner_send_image, settings.decider, settings.host, settings.port)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
