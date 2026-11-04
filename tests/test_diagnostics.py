"""测试连接: before a game, time the tunnel and upload from the client and each model from the server."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from server import diagnostics
from tests.test_server import settings

REAL_CLIENT, REAL_ASYNC_CLIENT = httpx.Client, httpx.AsyncClient  # patches below replace the module attributes


def test_server_times_one_tiny_text_call_per_model(settings, monkeypatch):
    seen = []
    def handler(request):
        seen.append(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    monkeypatch.setattr(diagnostics.httpx, "AsyncClient",
                        lambda timeout: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler)))
    ranked = []
    async def rank(client, url, model, state, options, config):
        ranked.append((state, list(options)))
        return "wait", {}
    monkeypatch.setattr(diagnostics, "rank", rank)
    monkeypatch.setattr(diagnostics, "readout", lambda: {})
    from dataclasses import replace
    result = asyncio.run(diagnostics.check_models(replace(settings, decider="onejev")))
    assert result["qwen"]["ok"] and result["jev"]["ok"] and ranked == [("Connection test.", ["wait", "go"])]
    assert b'"max_tokens":1' in seen[0].replace(b" ", b"") and b"image" not in seen[0]
    off = asyncio.run(diagnostics.check_models(replace(settings, decider="rules")))
    assert not off["jev"]["ok"] and "not used" in off["jev"]["detail"]


def test_endpoints_need_the_token_and_bound_the_upload(settings, monkeypatch):
    from fastapi.testclient import TestClient
    from server.app import UPLOAD_PROBE_MAX, create_app
    from server.planner import MockPlanner
    async def check(_settings):
        return {"qwen": {"ok": True, "ms": 5, "detail": "fixture"}, "jev": {"ok": True, "ms": 7, "detail": "fixture"}}
    monkeypatch.setattr(diagnostics, "check_models", check)
    client = TestClient(create_app(settings, planner=MockPlanner(0)))
    auth = {"Authorization": f"Bearer {settings.token}"}
    assert client.post("/diagnostics").status_code == 401
    assert client.post("/diagnostics", headers=auth).json()["jev"]["ms"] == 7
    assert client.post("/diagnostics/upload", content=b"x" * 1000, headers=auth).json() == {"bytes": 1000}
    assert client.post("/diagnostics/upload", content=b"x" * (UPLOAD_PROBE_MAX + 1), headers=auth).status_code == 413


# ---- client ----

@pytest.fixture
def wc():
    return pytest.importorskip("client.windows_client")


def run(wc, monkeypatch, handler):
    monkeypatch.setattr(wc.httpx, "Client", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler)))
    return wc.run_diagnostics(SimpleNamespace(base="http://server.invalid", token="test-only"))


def test_client_reports_tunnel_upload_and_model_tiles(wc, monkeypatch):
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        assert request.headers["Authorization"] == "Bearer test-only"
        if request.url.path == "/diagnostics/upload":
            assert len(request.content) == wc.UPLOAD_PROBE
            return httpx.Response(200, json={"bytes": len(request.content)})
        return httpx.Response(200, json={"qwen": {"ok": True, "ms": 412, "detail": "Qwen3.8-27B"},
                                         "jev": {"ok": False, "ms": 30000, "detail": "ConnectError: refused"}})
    tiles = run(wc, monkeypatch, handler)["tiles"]
    assert tiles["tunnel"][2] == tiles["upload"][2] == tiles["qwen"][2] == "ok"
    assert tiles["upload"][0].endswith("KiB/s") and tiles["qwen"][0] == "412 ms"
    assert tiles["jev"] == ["失败", "ConnectError: refused", "failed"]


def test_a_dead_tunnel_or_wrong_token_is_named(wc, monkeypatch):
    def dead(request):
        raise httpx.ConnectError("refused", request=request)
    tiles = run(wc, monkeypatch, dead)["tiles"]
    assert tiles["tunnel"][0] == "失败" and "SSH" in tiles["tunnel"][1]
    assert all(tiles[k][2] == "failed" for k in ("upload", "qwen", "jev"))
    def token(request):
        return httpx.Response(200, json={}) if request.url.path == "/health" else httpx.Response(401)
    tiles = run(wc, monkeypatch, token)["tiles"]
    assert tiles["tunnel"][2] == "ok" and "令牌" in tiles["upload"][1] and tiles["qwen"][2] == "failed"


def test_test_button_only_outside_a_connection_and_once_at_a_time(wc):
    from unittest.mock import Mock
    panel = wc.ControlPanel(Mock())
    assert not panel.button_states()["测试连接"]
    panel.operator_view = SimpleNamespace()
    assert panel.button_states()["测试连接"]
    panel.link_active.set()
    assert not panel.button_states()["测试连接"]
    panel.link_active.clear()
    panel.testing = True
    assert not panel.button_states()["测试连接"]
