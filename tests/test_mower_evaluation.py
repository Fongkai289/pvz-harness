from client.mower_state import MowerTracker, ReadOnlyGame
import pytest
from client.fake_lawn import FakeLawn, Zombie, ZOMBIES
from common.protocol import EpisodeEvent, StateMsg
from server.judge import Judge, score, mower_evaluation


def final(t):
    return dict(game="lawn", killed=10, scheduled=10, outcome="win", mowers_used=1,
                mower_timing_coverage="from_start", mower_events=[dict(row=0, time_s=t)])


def test_earlier_is_worse_and_extending_episode_cannot_change_penalty():
    assert score(final(10)) < score(final(60)) < score(final(180)) < .9
    assert score(final(60)) == score({**final(60), "game_time_s": 99999})
    none = {**final(60), "mowers_used": 0, "mower_events": []}
    assert score(none) == 1


def test_missing_time_is_not_zero_or_reconnect_time():
    assert score(final(None)) is None
    metric = mower_evaluation(final(None))
    assert metric["unknown_trigger_times"] == 1 and not metric["comparable"]
    assert metric["first_known_trigger_s"] is None


def sample(tick, state=1, board=100):
    return dict(board=board, tick=tick, level=7, paused=False,
                mowers=[dict(row=0, state=state, dead=False)])


def test_tracker_pause_reconnect_dedup_and_new_board():
    tracker = MowerTracker()
    tracker.update(sample(0))
    tracker.update(sample(1000))
    # Same game tick while paused, regardless of wall time.
    tracker.update({**sample(1000), "paused": True})
    result = tracker.update(sample(1005, 2))
    assert result["mower_events"] == [dict(row=0, time_s=10.05, uncertainty_s=.05)]
    assert tracker.update(sample(1010, 2))["mowers_used"] == 1
    assert tracker.update(sample(0, board=101))["mowers_used"] == 0


def test_tracker_midgame_and_sampling_gap_are_unknown():
    tracker = MowerTracker()
    result = tracker.update(sample(9000, 2))
    assert result["mower_events"][0]["time_s"] is None
    assert result["mower_timing_coverage"] == "partial"
    tracker.update(sample(0, board=200))
    assert tracker.update(sample(1000, 2, board=200))["mower_events"][0]["time_s"] is None


def test_fake_game_records_exact_trigger_once():
    game = FakeLawn(seed=1)
    game.level.schedule = [(100_000, "normal", 4)]
    game.zombies.append(Zombie("normal", 0, 60., *ZOMBIES["normal"]))
    for _ in range(60):
        game.step()
    state = game.state()
    assert len(state["mower_events"]) == state["mowers_used"] == 1
    assert 0 < state["mower_events"][0]["time_s"] <= 3
    assert mower_evaluation(state)["comparable"]


def test_judge_does_not_reuse_previous_round_state():
    judge = Judge()
    judge.on_state(StateMsg(t=0, frame_id=0, data=final(5)))
    judge.on_episode(EpisodeEvent(event="start"))
    summary = judge.on_episode(EpisodeEvent(event="end"))
    assert summary["score"] is None and summary["mower_evaluation"] is None


def test_result_is_latched_until_a_new_board_and_pause_is_not_loss():
    tracker = MowerTracker()
    assert tracker.update({**sample(0), "paused": True})["outcome"] is None
    assert tracker.update({**sample(100), "outcome": "lose"})["outcome"] == "lose"
    assert tracker.update(sample(101))["outcome"] == "lose"
    assert tracker.update(sample(0, board=200))["outcome"] is None
    assert tracker.update({**sample(10, board=200), "outcome": "win"})["outcome"] == "win"


@pytest.mark.parametrize("ui,award,mode,expected", [(3, 0, 0, None), (4, 0, 0, "lose"),
    (5, 1, 0, "win"), (3, 1, 0, "win"), (1, 0, 0, None), (3, 1, 1, None)])
def test_verified_game_scene_and_award_marker(ui, award, mode, expected):
    reader = ReadOnlyGame.__new__(ReadOnlyGame)
    app, board = 100000, 200000
    values = {0x731C50: app, app + 0x91C: ui, app + 0x918: mode, app + 0x868: board,
              board + 0x5580: 100, board + 0x5564: 0, board + 0x11C: 0, board + 0x118: 0,
              board + 0x5568: 7}
    reader.u32 = values.__getitem__
    reader.read = lambda address, size: bytes([award if address == board + 0x5624 else 1])
    assert reader.sample()["outcome"] == expected
