"""Pre-game connection test (控制中心 · 测试连接): one tiny text-only call per model, made by the server.

The models are shared pools: Windows never calls them, nothing here sends an image, and each check is the
smallest request that still exercises inference (Qwen: one token; Jev: its real two-option code readout).
"""
from __future__ import annotations

import asyncio
import time

import httpx

from server.jev_vllm import rank, readout


async def _timed(check) -> dict:
    started = time.monotonic()
    try:
        detail = await check()
        return {"ok": True, "ms": round((time.monotonic() - started) * 1000), "detail": detail}
    except Exception as exc:
        return {"ok": False, "ms": round((time.monotonic() - started) * 1000), "detail": f"{type(exc).__name__}: {exc}"[:200]}


async def check_models(settings, timeout: float = 30.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        async def qwen():
            response = await client.post(settings.qwen_url, json={
                "model": settings.qwen_model, "max_tokens": 1, "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": "Reply with OK."}]})
            response.raise_for_status()
            return settings.qwen_model

        async def jev():
            if settings.decider != "onejev":
                raise RuntimeError(f"not used (decider {settings.decider})")
            await rank(client, settings.onejev_url, settings.onejev_model, "Connection test.",
                       {"wait": "Do nothing.", "go": "Proceed."}, readout())
            return settings.onejev_model

        qwen_result, jev_result = await asyncio.gather(_timed(qwen), _timed(jev))
    return {"qwen": qwen_result, "jev": jev_result}
