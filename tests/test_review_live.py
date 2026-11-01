import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsi.review_live import LiveReview, build_evidence, review, select_session
from tests.test_server import settings


def events():
    return [
        dict(kind="hello", t_server=1, client_id="windows", playbook_loaded="v001"),
        dict(kind="episode", t_server=1, event="start"),
        dict(kind="state", t_server=2, data=dict(game="real_pvz", board_id="b", game_time_s=210,
                                                mowers_used=1, mower_events=[dict(row=0, time_s=50)])),
        dict(kind="jev", t_server=3, action="wait", reason="no budget", decision_id=None),
        dict(kind="state", t_server=4, data=dict(game="real_pvz", board_id="b", game_time_s=220,
                                                mowers_used=1, mower_events=[dict(row=0, time_s=50)])),
        dict(kind="episode", t_server=5, event="end", summary=dict(score=None)),
        dict(kind="session_closed", t_server=6),
    ]


def write_log(root, name, rows):
    path = root / name / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(e) for e in rows), encoding="utf-8")
    return path


def test_latest_does_not_silently_select_old_run_if_newest_is_active(tmp_path):
    write_log(tmp_path, "20261030-120000-windows-old", events())
    write_log(tmp_path, "20261030-130000-windows-new", events()[:-2])
    with pytest.raises(ValueError, match="still active"):
        select_session(tmp_path, None)
    with pytest.raises(ValueError, match="invalid session"):
        select_session(tmp_path, "../other")


def test_evidence_separates_historical_mowers_and_does_not_invent_game_outcome():
    evidence = build_evidence(events())
    assert evidence["observed_outcome"] == "unknown"
    assert len(evidence["boards"]) == 1
    board = evidence["boards"][0]
    assert board["initial"]["data"]["game_time_s"] == 210
    assert board["final"]["data"]["mowers_used"] == 1  # not summed over snapshots
    assert evidence["counts"]["jev_no_call_reasons"] == {"no budget": 1}


def test_review_capture_cadence_is_not_inferred_from_storage_or_frame_number_gaps():
    log = events()
    log[0]["effective_timing"] = {"frame_log_seconds": 4}
    log[2:2] = [dict(kind="frame", t_server=2+i*.2, frame_id=i*4, t_capture=100+i*.2) for i in range(4)]
    evidence = build_evidence(log)
    assert evidence["frame_evidence"]["received_frames"] == 4
    assert evidence["frame_evidence"]["median_capture_interval_seconds"] == pytest.approx(.2)


def test_manual_coach_is_cached_and_never_changes_playbook(settings, tmp_path):
    data = settings.data_dir
    (data / "playbooks").mkdir(parents=True)
    source = Path("playbook_seed/v001.json").read_bytes()
    (data / "playbooks" / "v001.json").write_bytes(source)
    # CURRENT intentionally differs: review must load what actually played.
    (data / "CURRENT").write_text("v099\n")
    path = write_log(settings.runs_dir, "20261030-120000-windows-test", events())
    calls = []
    async def model(role, messages, schema, *args, **kwargs):
        payload = json.loads(messages[1]["content"])
        assert payload["played_playbook"]["version"] == "v001"
        assert payload["promotion_allowed"] is False
        calls.append(role)
        return LiveReview(diagnosis="证据有限", evidence=["E3 等待"], objective="验证预算阻塞",
                          uncertainties=["没有胜负真值"]), dict(model="fake-coach", latency_seconds=.1)
    first, output = asyncio.run(review(path, settings, call=model))
    second, _ = asyncio.run(review(path, settings, call=model))
    assert first == second and calls == ["live_coach"]
    assert first["promoted"] is False and (output / "review.txt").exists()
    assert (data / "CURRENT").read_text() == "v099\n"
    assert (data / "playbooks" / "v001.json").read_bytes() == source
    assert len(list((data / "playbooks").iterdir())) == 1
