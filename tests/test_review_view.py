"""赛后复盘 in the control center: the review arrives as fields for cards, not one block of text."""
from client.operator_view import OperatorView
from tests.test_server import settings


def view(review, learning=None, rounds=()):
    v = OperatorView("ws://server.invalid/ws", "test-only")
    v.review, v.learning, v.coach_usage = review, learning or {"status": "not_triggered"}, {"rounds": list(rounds)}
    return v.review_view()


def test_a_completed_review_becomes_cards_with_training_and_token_rows():
    record = {"coach": {"diagnosis": "Row 3 had no attacker.", "objective": "Cover every row early.",
                        "evidence": ["E12: mower in row 3", "E40: wait with 200 sun"], "uncertainties": "one game only"},
              "model_call": {"model": "gpt-6-astra"}}
    rounds = [{"round_id": "data/live_reviews/s1/abc123", "tokens": {"calls": 2, "input_tokens": 10, "cached_input_tokens": None,
                                                                     "output_tokens": 3, "total_tokens": 13}}]
    v = view({"session": "s1", "status": "completed", "review": record, "native_rsi_skipped": "won without a lawn mower"},
             {"status": "waiting_runtime", "parent": "v009", "updated": 1793355019.6, "detail": "old"}, rounds)
    assert (v["label"], v["session"], v["model"]) == ("复盘完成 · 待你确认下一局", "s1", "gpt-6-astra")
    assert v["evidence"] == ["E12: mower in row 3", "E40: wait with 200 sun"] and v["uncertainties"] == ["one game only"]
    assert "learning" not in v  # the page shows the Portable training run itself (/api/training), not the pvz_rl queue
    assert v["usage"] == [{"round": "abc123", "calls": 2, "input_tokens": 10, "cached_input_tokens": None,
                           "output_tokens": 3, "total_tokens": 13}]
    assert v["skipped"]


def test_waiting_and_failed_reviews_carry_no_invented_content():
    waiting = view({"status": "reviewing", "session": "s2"})
    assert waiting["label"] == "Coach 正在复盘" and not waiting["diagnosis"] and waiting["evidence"] == []
    failed = view({"status": "failed", "session": "s3", "error": "TimeoutError"})
    assert failed["label"] == "复盘失败" and failed["error"] == "TimeoutError"
    assert view({"status": "not_triggered"})["usage"] == []


def test_the_list_is_newest_first_and_uses_the_fresher_latest_review():
    v = OperatorView("ws://server.invalid/ws", "test-only")
    v.review = {"session": "20261104-102524-windows-a", "status": "completed"}
    v.reviews = [{"session": "20261104-102524-windows-a", "status": "reviewing"},
                 {"session": "20261103-215504-windows-b", "status": "failed", "error": "Interrupted"}]
    rows = v.review_views()
    assert [(r["session"], r["status"]) for r in rows] == [("20261104-102524-windows-a", "completed"),
                                                           ("20261103-215504-windows-b", "failed")]
    v.review = {"session": "20261104-120000-windows-c", "status": "queued"}  # ended after the last list fetch
    assert v.review_views()[0]["session"] == "20261104-120000-windows-c"
    v.reviews, v.review = [], {"status": "not_triggered"}
    assert [r["status"] for r in v.review_views()] == ["not_triggered"]  # an old server without /reviews


def test_server_lists_recent_reviews_newest_first(settings):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.live_reviews import LiveReviews
    from server.planner import MockPlanner
    reviews = LiveReviews(settings)
    for session in ("20261103-215504-windows-b", "20261104-102524-windows-a", "20261102-090000-windows-z"):
        reviews.publish(session, {"status": "completed"})
    client = TestClient(create_app(settings, planner=MockPlanner(0)))
    assert client.get("/reviews").status_code == 401
    listed = client.get("/reviews?limit=2", headers={"Authorization": f"Bearer {settings.token}"}).json()
    assert [r["session"] for r in listed] == ["20261104-102524-windows-a", "20261103-215504-windows-b"]


def test_each_review_carries_how_its_game_ended(settings):
    import json
    from server.live_reviews import LiveReviews
    reviews = LiveReviews(settings)
    session = "20261103-215504-windows-b"
    log = settings.runs_dir / session / "events.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"kind": "state", "data": {}}) + "\n", encoding="utf-8")
    reviews.publish(session, {"status": "failed", "error": "Interrupted"})
    assert reviews.status(session)["result"] is None  # no end yet
    end = {"kind": "episode", "event": "end", "summary": {"final_state": {"outcome": "win", "mowers_used": 0, "level": 7}}}
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(end) + "\n")
    listed = reviews.recent()[0]
    assert listed["status"] == "failed" and listed["result"] == {"outcome": "win", "mowers": 0, "level": 7}
    assert reviews.status("20261104-000000-windows-none")["result"] is None  # no log at all
