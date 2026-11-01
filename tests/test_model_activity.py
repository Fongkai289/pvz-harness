from client.model_activity import ModelActivity, activity_lines
from common.protocol import ModelTiming


def test_running_completion_idle_and_real_start_counts():
    now = [100.0]
    activity = ModelActivity(clock=lambda: now[0])
    assert activity.snapshot("scene")["status"] == "idle"
    activity.update(ModelTiming(stage="scene", status="running", call_id=1, interval_s=4))
    now[0] += 2
    assert activity.snapshot("scene")["age_s"] == 2
    assert "正在推理" in activity_lines("Qwen", activity.snapshot("scene"))[0]
    activity.update(ModelTiming(stage="scene", status="completed", call_id=1, latency_ms=2000))
    assert activity.snapshot("scene")["count"] == 1
    assert activity.snapshot("scene")["recent_count"] == 1
    activity.update(ModelTiming(stage="scene", status="idle", call_id=1, reason="等待画面"))
    assert activity.snapshot("scene")["count"] == 1
    now[0] = 161
    assert activity.snapshot("scene")["recent_count"] == 0
    assert activity.snapshot("scene")["age_s"] == 61


def test_failure_and_disconnect_do_not_remain_running():
    activity = ModelActivity()
    activity.update(ModelTiming(stage="plan", status="running", call_id=1))
    activity.update(ModelTiming(stage="plan", status="failed", call_id=1, latency_ms=3000,
                                error=True, reason="TimeoutError"))
    assert activity.snapshot("plan")["status"] == "failed"
    activity.update(ModelTiming(stage="jev", status="running", call_id=1))
    activity.disconnect()
    assert activity.snapshot("jev")["status"] == "failed"
    assert "未收到" in activity.snapshot("jev")["reason"]
    assert activity.snapshot("plan")["reason"] == "TimeoutError"


def test_stale_completion_cannot_overwrite_new_call():
    activity = ModelActivity()
    activity.update(ModelTiming(stage="scene", status="running", call_id=1))
    activity.update(ModelTiming(stage="scene", status="completed", call_id=1, latency_ms=5))
    activity.update(ModelTiming(stage="scene", status="running", call_id=2))
    activity.update(ModelTiming(stage="scene", status="completed", call_id=1, latency_ms=7))
    sample = activity.snapshot("scene")
    assert sample["status"] == "running" and sample["count"] == 2 and sample["recent_count"] == 2


def test_legacy_completion_still_counts_a_call():
    activity = ModelActivity()
    activity.update(ModelTiming(stage="scene", latency_ms=2000))
    assert activity.snapshot("scene")["count"] == 1


def test_pause_freezes_age_and_rolling_window_until_new_connection():
    now = [100.0]
    activity = ModelActivity(clock=lambda: now[0])
    activity.update(ModelTiming(stage="jev", status="running", call_id=1))
    now[0] = 105
    activity.freeze()
    now[0] = 500
    activity.disconnect()
    snapshot = activity.snapshot("jev")
    assert snapshot["age_s"] == 5 and snapshot["recent_count"] == 1
    assert ModelActivity().snapshot("jev")["count"] == 0
