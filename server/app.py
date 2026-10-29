"""Server entry point: `uv run python -m server.app`."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import re
import secrets
import time

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from common.protocol import Hello, Welcome, dumps, parse_client_message
from server.config import Settings, load_settings
from server.decider import Judge, make_judge
from server.planner import Planner, make_planner
from server.playbook import PlaybookStore
from server.scene import QwenLooker
from server.session import Session

log = logging.getLogger("pvz.server")

HELLO_TIMEOUT = 10.0


def create_app(settings: Settings, planner: Planner | None = None,
               jev_judge: Judge | None = None, looker: QwenLooker | None = None) -> FastAPI:
    planner = planner or make_planner(settings)
    jev_judge = jev_judge or make_judge(settings)
    looker = looker or (QwenLooker(settings.qwen_url, settings.qwen_model, timeout=settings.planner_timeout)
              if settings.planner_mode == "split" and settings.planner == "qwen" else None)
    store = PlaybookStore(settings.data_dir)
    app = FastAPI(title="pvz-agent server")
    app.state.sessions = {}

    @app.get("/health")
    async def health() -> dict:
        planner_model = {"claude": settings.planner_model, "qwen": settings.qwen_model}.get(settings.planner)
        return {
            "ok": True,
            "planner": settings.planner,
            "planner_model": planner_model,
            "planner_mode": "split" if looker else "end_to_end",
            "planner_send_image": settings.planner_send_image if settings.planner != "mock" else None,
            "jev": settings.decider,
            "jev_model": settings.onejev_model if settings.decider == "onejev" else None,
            "loadout": settings.loadout,
            "playbook": store.current(),
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
        session = Session(ws, settings, planner, jev_judge, playbook, session_id, looker)
        session.runlog.event("hello", **hello.model_dump(exclude={"type", "token"}),
                             playbook_loaded=playbook.version)
        app.state.sessions[session_id] = session
        await ws.send_text(dumps(Welcome(session_id=session_id, playbook=playbook.version)))
        log.info("session %s connected", session_id)

        tasks = [asyncio.create_task(session.fast_loop()), asyncio.create_task(session.plan_loop()),
                 asyncio.create_task(session.look_loop()), asyncio.create_task(session.plants_loop())]
        try:
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
