"""Audited, bounded Qwen requests shared by independent research roles."""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

import httpx
from pydantic import BaseModel

from rsi.code_evaluate import Interrupted
from rsi.code_store import write_json


async def request_role(role: str, messages: list[dict], schema: type[BaseModel], settings,
                       *, controls: Path, audit_dir: Path) -> tuple[BaseModel, dict]:
    if sys.platform != "linux":
        raise RuntimeError("Model calls must run on the configured Linux server")
    if (controls / "STOP").exists() or (controls / "PAUSE").exists():
        raise Interrupted("explicit stop/pause: role request cancelled")
    body = {"model": settings.qwen_model, "max_tokens": 8192, "temperature": 0.3,
            "chat_template_kwargs": {"enable_thinking": False}, "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": role, "schema": schema.model_json_schema()}}}
    request_id = uuid.uuid4().hex
    audit_dir.mkdir(parents=True, exist_ok=True)
    write_json(audit_dir / f"{request_id}-request.json", {"role": role, "body": body})
    start = time.monotonic()
    async with httpx.AsyncClient(timeout=180) as client:
        request = asyncio.create_task(client.post(settings.qwen_url, json=body))
        try:
            while not request.done():
                if (controls / "STOP").exists() or (controls / "PAUSE").exists():
                    raise Interrupted("explicit stop/pause: role request cancelled")
                await asyncio.wait({request}, timeout=0.2)
            response = await request
            # Preserve errors as well as successful payloads to locate model/API failures.
            write_json(audit_dir / f"{request_id}-response.json", {
                "role": role, "latency_seconds": time.monotonic() - start,
                "status_code": response.status_code, "body": response.text})
            response.raise_for_status()
            payload = response.json()
            result = schema.model_validate_json(payload["choices"][0]["message"]["content"])
            return result, {"role": role, "model": settings.qwen_model,
                            "request_id": request_id, "response_id": payload.get("id"),
                            "finish_reason": payload["choices"][0].get("finish_reason"),
                            "usage": payload.get("usage"), "latency_seconds": time.monotonic() - start}
        except BaseException as exc:
            write_json(audit_dir / f"{request_id}-error.json", {
                "role": role, "latency_seconds": time.monotonic() - start,
                "error": f"{type(exc).__name__}: {exc}"})
            raise
        finally:
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
