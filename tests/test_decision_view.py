import json

from fastapi.testclient import TestClient
from client.operator_view import OperatorView
from server.decision_view import DecisionView
from server.app import create_app
from tests.test_server import settings


def test_projection_keeps_decisions_and_execution_separate_and_excludes_truth(tmp_path):
    name = "20261030-130000-windows-test"
    path = tmp_path / name / "events.jsonl"
    path.parent.mkdir()
    rows = [dict(kind="state", data={"private_ground_truth": 123}),
            dict(kind="jev", action="plant", decision_id=2, placement=dict(slot=0, row=4, col=3),
                 reason="selected", rejected="context changed"),
            dict(kind="cmd", cmd_id=1, action="click", x=120, y=43, reason="select slot"),
            dict(kind="exec", cmd_id=1, ok=True, result={"private_ground_truth": 456}),
            dict(kind="sun_collection", frame_id=2, detected=2, confirmed=2, clicks_sent=1, total_clicks=3,
                 reason="已下发拾取"),
            dict(kind="cmd", cmd_id=2, action="click", x=300, y=350, reason="collect sun",
                 meta={"decision_source": "sun_collector"})]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n{", encoding="utf-8")
    reader = DecisionView(tmp_path)
    snapshot = reader.latest({name: object()})
    assert snapshot["status"] == "live"
    assert "private_ground_truth" not in json.dumps(snapshot)
    assert snapshot["events"]["jev"]["rejected"] == "context changed"
    view = OperatorView("ws://example/ws", "secret")
    view.decisions = snapshot
    text = view.decisions_text()
    assert "第5行、第4列" in text and "未执行：context changed" in text
    assert "不等于种植成功" in text
    assert "代码拾取" in text and "3 次点击" in view.collection_text()
    assert "不等于确认拾取" in view.collection_text()
    view.decisions = reader.latest({})
    assert "已暂停或断开" in view.decisions_text()
    assert "上次连接" in view.collection_text()


def test_decision_route_requires_auth(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/decisions/latest").status_code == 401
        assert client.get("/decisions/latest", headers={"Authorization": "Bearer secret"}).json() == {"status": "not_started"}
