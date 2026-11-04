"""One-click training: resume from the first level never won, improve after a loss, show progress."""
import importlib.util
import json
from pathlib import Path

import pytest

from common.file_lock import file_lock
from rsi import portable_loop
from rsi.native_loop import Stopped

ROOT = Path(__file__).resolve().parents[1]


def records(root, name, outcomes):
    """outcomes: (level, "win" | "mower" | "lose"); "mower" is a win that used one lawn mower."""
    rows = [{"level": l, "outcome": "lose" if o == "lose" else "win", "mowers": 1 if o != "win" else 0}
            for l, o in outcomes]
    (root / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")


def test_first_unbeaten_level_comes_from_every_recorded_campaign(tmp_path):
    assert portable_loop.first_unbeaten(tmp_path) == 1
    records(tmp_path, "old-before", [(1, "win"), (2, "lose")])
    records(tmp_path, "later-round-1", [(2, "win"), (3, "mower"), (4, "lose")])
    (tmp_path / "status.json").write_text(json.dumps({"level": 5, "outcome": "win"}), encoding="utf-8")  # not a campaign
    # A record from before mower counts were kept: a full score means no mower was used.
    (tmp_path / "legacy.json").write_text(json.dumps([{"level": 6, "outcome": "win", "score": 0.83}]), encoding="utf-8")
    assert portable_loop.won_levels(tmp_path) == {1, 2, 3, 6}
    assert portable_loop.won_levels(tmp_path, clean=True) == {1, 2}
    assert portable_loop.mower_wins(tmp_path) == {3, 6}
    assert portable_loop.first_unbeaten(tmp_path) == 4
    assert portable_loop.first_unbeaten(tmp_path, portable_loop.TRAIN_LEVELS, clean=True) == 3
    records(tmp_path, "all", [(l, "win") for l in range(2, 11)])
    assert portable_loop.first_unbeaten(tmp_path, portable_loop.TRAIN_LEVELS, clean=True) is None


class Script:
    """Fake level games and Coach rounds. games: outcome per level game in play order ("win", "mower", "lose");
    rounds: per round {variant: (better, cleared)} for the parallel variants."""
    def __init__(self, games, rounds):
        self.games, self.rounds, self.played, self.created, self.started, self.results = list(games), list(rounds), [], [], [], {}
        self.lock = __import__("threading").Lock()
        self.resume = {}

    def play_level(self, config, bridge, root, name, level, progress=None, level_book=False, seed=None):
        assert level_book and seed is None  # training plays the level's own playbook on its fixed seed
        outcome = self.games.pop(0)
        self.played.append(level)
        return {"level": level, "outcome": "lose" if outcome == "lose" else "win", "mowers": 0 if outcome == "win" else 2,
                "score": 1.0 if outcome == "win" else .4, "job": f"game-{len(self.played)}"}

    def rsi_round(self, config, bridge, root, name, progress=None, target=None, create=None, on_status=None):
        request = create()
        with self.lock:
            self.created.append((request["level"], request["evidence_job"], request["group"], request["angle"]))
            if len(self.created) % 2 == 1:
                self.current = self.rounds.pop(0)
            better, cleared = self.current[request["angle"]]
        self.results[name] = {"id": name, "status": "tested", "better": better, "cleared": cleared, "candidate": "v" + name,
                              "result": {"outcome": "win", "score": 1.0 if cleared else .6, "mowers": 0 if cleared else 1}}
        on_status(self.results[name])
        return self.results[name] | {"tests": 1}

    def keep(self, ids):
        jobs = [dict(self.results[i]) for i in ids]
        best = max((j for j in jobs if j["better"]), key=lambda j: (j["cleared"], j["result"]["score"]), default=None)
        for job in jobs:
            job.update(status="promoted" if job is best else "rejected", promoted=job is best)
        return {"best": best["id"] if best else None, "jobs": jobs}


class Bridge:
    def __init__(self, script):
        self.script = script

    def call(self, op, **kw):
        if op == "portable_level_start":
            self.script.started.append(kw["level"])
            return {"level": kw["level"], "version": "v010"}
        if op == "portable_level_keep":
            return self.script.keep(kw["jobs"])
        if op == "portable_level_resume":
            return self.script.resume.pop(kw["level"], None)
        assert op == "portable_level_attempt"
        return kw


def run_train(tmp_path, monkeypatch, games, rounds):
    script = Script(games, rounds)
    monkeypatch.setattr(portable_loop, "play_level", script.play_level)
    monkeypatch.setattr(portable_loop, "rsi_round", script.rsi_round)
    progress = portable_loop.Progress(tmp_path, "train-x", "train")
    portable_loop.train({}, Bridge(script), tmp_path, "train-x", progress)
    return script, json.loads((tmp_path / "training.json").read_text(encoding="utf-8"))


def test_each_round_tries_parallel_fixes_and_keeps_the_best(tmp_path, monkeypatch):
    records(tmp_path, "earlier", [(1, "lose"), (2, "win"), (3, "lose")])
    script, state = run_train(tmp_path, monkeypatch, ["win", "mower"] + ["win"] * 6,
                              [{"a": (True, False), "b": (False, False)}, {"a": (False, False), "b": (True, True)}])
    # 1-1 is skipped and 1-2 was won before; 1-3 wins at once; 1-4 costs a mower and takes two rounds.
    assert script.started[:2] == [3, 4] and script.played == [3, 4, 5, 6, 7, 8, 9, 10]
    by_round = sorted(script.created, key=lambda c: (c[2], c[3]))
    # Both variants start from the same evidence, each from its own suggested starting point.
    assert by_round == [(4, "game-2", "train-x-L4-attempt1", "a"), (4, "game-2", "train-x-L4-attempt1", "b"),
                        (4, "train-x-L4-attempt1a", "train-x-L4-attempt2", "a"),
                        (4, "train-x-L4-attempt1a", "train-x-L4-attempt2", "b")]
    assert [(r["round"], r["variant"], r["status"]) for r in state["rounds"]] == [
        (1, "a", "promoted"), (1, "b", "rejected"), (2, "a", "rejected"), (2, "b", "promoted")]
    assert state["state"] == "complete" and state["won"] == list(range(2, 11)) and state["skipped"] == [1]
    assert set(state["variants"]) == {"a", "b"}
    attempts = json.loads((tmp_path / "levels" / "4.json").read_text(encoding="utf-8"))["attempts"]
    assert [(a["attempt"], a["variant"], a["kept"]) for a in attempts] == [(1, "a", True), (1, "b", False), (2, "a", False), (2, "b", True)]


def test_a_level_that_resists_every_round_stops_training(tmp_path, monkeypatch):
    monkeypatch.setattr(portable_loop, "MAX_LEVEL_ATTEMPTS", 3)
    script, state = run_train(tmp_path, monkeypatch, ["lose"], [{"a": (False, False), "b": (False, False)}] * 3)
    assert script.played == [2] and len(state["rounds"]) == 6
    assert {c[1] for c in script.created} == {"game-1"}  # nothing kept: every round starts from the same game
    assert state["state"] == "complete" and "试了 3 次" in state["message"] and state["level"] == 2


def test_campaign_stops_at_a_mower_only_when_asked(tmp_path, monkeypatch):
    class Bridge:
        def call(self, op, **kw):
            return {"id": kw.get("name", "job")}
    def evaluate(config, task, bridge):
        return {"episodes": [{"outcome": "win", "score": 0.8, "mower_times": [42.0], "wall_s": 1, "throughput": {}}]}
    monkeypatch.setattr(portable_loop, "evaluate_task", evaluate)
    played = portable_loop.campaign({}, Bridge(), tmp_path, "c", start=9)
    assert [(r["level"], r["mowers"]) for r in played] == [(9, 1), (10, 1)]  # old modes: wins continue
    assert len(portable_loop.campaign({}, Bridge(), tmp_path, "d", start=9, clean=True)) == 1


def test_stop_request_is_recorded_as_stopped(tmp_path, monkeypatch):
    class Bridge:
        def __init__(self, config, root): pass
        def call(self, op, **kw): return None if op == "portable_level_resume" else {"version": "v009", "closed": []}
        def close(self): pass
    def stop(*args, **kwargs):
        raise Stopped("worker explicitly stopped")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"work_dir": str(tmp_path)}), encoding="utf-8")
    monkeypatch.setattr(portable_loop, "preflight", lambda config: None)
    monkeypatch.setattr(portable_loop, "SSHBridge", Bridge)
    monkeypatch.setattr(portable_loop, "play_level", stop)
    monkeypatch.setattr("sys.argv", ["portable_loop", "--config", str(config), "--name", "train-x", "--mode", "train"])
    with pytest.raises(Stopped):
        portable_loop.main()
    state = json.loads((tmp_path / "training.json").read_text(encoding="utf-8"))
    assert state["state"] == "stopped" and state["message"] == "已按停止键停止"


# ---- control center ----

@pytest.fixture
def dashboard(monkeypatch):
    spec = importlib.util.spec_from_file_location("training_dashboard_under_test", ROOT / "scripts/training_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Process:
    def __init__(self, args, **kwargs):
        self.args, self.code = args, None
        kwargs["stdout"].write(b"preflight failed: Portable upstream changed\n")
    def poll(self):
        return self.code


def test_start_launches_one_training_and_clears_an_old_stop(dashboard, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard.subprocess, "Popen", Process)
    (tmp_path / "STOP").write_text("old")
    code, value = dashboard.start_training(tmp_path)
    assert code == 202 and value["name"].startswith("train-") and not (tmp_path / "STOP").exists()
    assert dashboard.LAUNCH["process"].args[-4:] == ["--name", value["name"], "--mode", "train"]
    assert dashboard.training_status(tmp_path)["state"] == "starting"
    assert dashboard.start_training(tmp_path)[0] == 409  # already launched
    dashboard.LAUNCH["process"].code = 1  # exited before writing training.json
    status = dashboard.training_status(tmp_path)
    assert status["state"] == "failed_to_start" and "upstream changed" in status["message"] and not status["running"]


def test_status_shows_running_live_game_interruption_and_stop(dashboard, tmp_path):
    assert dashboard.training_status(tmp_path)["running"] is False
    assert dashboard.stop_training(tmp_path)[0] == 409
    live = tmp_path / "job-1" / "live"
    live.mkdir(parents=True)
    (live / "e1.json").write_text(json.dumps({"level": 3, "tick": 4567, "decisions": 12,
        "scene": {"sun": 75, "zombies": [{}, {}]}, "plants": [{"plants": ["peashooter"]}, {"plants": ["sunflower"]}]}))
    (tmp_path / "training.json").write_text(json.dumps({"name": "train-1", "state": "running", "phase": "campaign",
                                                        "level": 3, "job": "job-1", "won": [1, 2]}))
    with file_lock(tmp_path / "WORKER.lock", blocking=False):
        status = dashboard.training_status(tmp_path)
        [game] = status["live"]
        assert status["running"] and game["game_s"] == 46 and game["zombies"] == 2
        assert dashboard.stop_training(tmp_path)[0] == 202 and dashboard.training_status(tmp_path)["stopping"]
    status = dashboard.training_status(tmp_path)  # lock released without a final state: the process died
    assert status["state"] == "interrupted" and status["live"] == []


def test_coach_round_shows_its_stage_count_and_every_running_lane(dashboard, tmp_path):
    import os
    import time
    job = tmp_path / "rsi-1"
    (job / "live").mkdir(parents=True)
    (job / "step-0").mkdir()
    for name, level in (("a", 2), ("b", 3), ("old", 4)):
        (job / "live" / f"{name}.json").write_text(json.dumps({"level": level, "tick": 3000, "scene": {}, "plants": []}))
    stale = time.time() - 600
    os.utime(job / "live" / "old.json", (stale, stale))  # a finished game's file stops changing
    (job / "step-0" / "episode-0.json").write_text(json.dumps({"complete": True}))
    (tmp_path / "status.json").write_text(json.dumps({"id": "rsi-1", "status": "waiting_runtime", "step": 0,
                                                      "suites": {"development": [{}] * 8}}))
    (tmp_path / "training.json").write_text(json.dumps({"name": "t", "state": "running", "phase": "rsi", "job": "rsi-1"}))
    with file_lock(tmp_path / "WORKER.lock", blocking=False):
        status = dashboard.training_status(tmp_path)
    assert sorted(g["level"] for g in status["live"]) == [2, 3]
    assert status["evaluation"] == {"step": 0, "stage": "development", "mode": "state", "side": "base",
                                    "completed": 1, "total": 8, "candidate": None, "status": "waiting_runtime"}


def test_progress_writes_wait_for_a_reader_instead_of_crashing(tmp_path):
    """The control center reads live files while the trainer replaces them (WinError 5 on Windows)."""
    import threading
    import time
    from rsi.code_store import write_json
    path = tmp_path / "live.json"
    write_json(path, {"tick": 1})
    stream = path.open("rb")
    threading.Timer(0.3, stream.close).start()
    started = time.monotonic()
    write_json(path, {"tick": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"tick": 2}
    assert not list(tmp_path.glob("*.tmp"))
    if __import__("os").name == "nt":
        assert time.monotonic() - started >= 0.2  # it really waited for the reader


def test_dropped_connection_replays_the_level_game(tmp_path, monkeypatch):
    """Link reset during 1-2's game: reconnect and replay that game, never count the half game."""
    calls, reopened = [], []
    def play_level(config, bridge, root, name, level, progress=None, level_book=False, seed=None):
        calls.append(name)
        if len(calls) == 1:
            raise RuntimeError("RPC connection failed") from RuntimeError("research process disconnected")
        return {"level": level, "outcome": "win", "mowers": 0, "score": 1.0, "job": name}
    class Link:
        root = tmp_path
        def call(self, op, **kw):
            return None if op == "portable_level_resume" else {"version": "v010"}
        def reopen(self, failed=None):
            reopened.append(1)
    monkeypatch.setattr(portable_loop, "play_level", play_level)
    monkeypatch.setattr(portable_loop, "RECONNECT_DELAYS", (0, 0, 0))
    progress = portable_loop.Progress(tmp_path, "train-x", "train")
    portable_loop.train({}, Link(), tmp_path, "train-x", progress)
    assert calls[:2] == ["train-x-L2", "train-x-L2-retry1"] and reopened == [1]
    state = json.loads((tmp_path / "training.json").read_text(encoding="utf-8"))
    assert state["state"] == "complete" and state["rounds"] == [] and state["notice"] == ""


def test_connection_that_stays_down_stops_after_the_last_retry(tmp_path, monkeypatch):
    def play_level(*args, **kwargs):
        raise RuntimeError("RPC connection failed") from RuntimeError("SSH closed")
    class Link:
        root = tmp_path
        def call(self, op, **kw):
            return None if op == "portable_level_resume" else {"version": "v010"}
        def reopen(self, failed=None):
            pass
    monkeypatch.setattr(portable_loop, "play_level", play_level)
    monkeypatch.setattr(portable_loop, "RECONNECT_DELAYS", (0, 0, 0))
    with pytest.raises(RuntimeError, match="RPC connection failed"):
        portable_loop.train({}, Link(), tmp_path, "train-x", portable_loop.Progress(tmp_path, "train-x", "train"))
    # A server-side error is not a dropped link: no retry.
    assert not portable_loop.connection_lost(RuntimeError("Planner failed: bad response"))


def test_stage_count_also_reads_the_runners_progress_file(dashboard, tmp_path):
    (tmp_path / "status.json").write_text(json.dumps({"status": "evaluating", "job": "rsi-1", "step": 3, "mode": "state",
                                                      "side": "base", "completed": 5, "total": 12, "workers": 2}))
    progress = dashboard.evaluation_progress(tmp_path, "rsi-1")
    assert (progress["stage"], progress["completed"], progress["total"]) == ("confirmation", 5, 12)
    assert dashboard.evaluation_progress(tmp_path, "other-job") is None


def test_level_history_counts_games_and_attempts_per_level(dashboard, tmp_path):
    (tmp_path / "levels").mkdir()
    (tmp_path / "levels" / "4.json").write_text(json.dumps({"level": 4, "games": [
        {"time": 1, "kind": "level_game", "job": "g1", "outcome": "win", "mowers": 2, "score": .5, "version": "v010"},
        {"time": 3, "kind": "coach_test", "attempt": 1, "job": "a1", "outcome": "win", "mowers": 0, "score": 1.0}],
        "attempts": [{"time": 2, "attempt": 1, "job": "a1", "kept": True, "cleared": True, "version": "v012",
                      "diagnosis": "row 2 fell", "rationale": "wall earlier", "changes": {"files": ["player/policy.py"]}}]}))
    records(tmp_path, "older-run", [(2, "win"), (3, "mower")])  # before per-level training: still counted
    history = dashboard.level_history(tmp_path)
    by_level = {e["level"]: e for e in history["levels"]}
    assert (by_level[4]["played"], by_level[4]["tries"], by_level[4]["kept"], by_level[4]["clean_wins"]) == (2, 1, 1, 1)
    assert by_level[4]["version"] == "v012" and by_level[2]["played"] == 1 and by_level[3]["wins"] == 1
    assert by_level[3]["clean_wins"] == 0 and history["skipped"] == [1]


def test_running_games_expose_their_key_frames(dashboard, tmp_path):
    live = tmp_path / "job-1" / "live"
    live.mkdir(parents=True)
    episode = "a" * 32
    cards = [{"slot": 0, "plant": "peashooter", "ready": True}]
    (live / f"{episode}.json").write_text(json.dumps({"level": 3, "tick": 900, "scene": {"cards": cards}, "plants": []}))
    (live / f"{episode}-frames.json").write_text(json.dumps({"level": 3, "frames": [
        {"tick": 0, "scene": {"cards": cards}, "plants": [], "placement": None, "plan": "economy"},
        {"tick": 450, "scene": {"cards": cards}, "plants": [], "placement": {"slot": 0, "row": 1, "col": 2}, "plan": "hold"}]}))
    [game] = dashboard.live_games(tmp_path, "job-1")  # the frames file is not a second game
    assert (game["job"], game["episode"]) == ("job-1", episode)
    frames = dashboard.live_frames(tmp_path, "job-1", episode)
    assert [n["label"] for n in frames["nodes"]] == ["定时观察", "种植豌豆射手 · 第2行第3列"]
    assert frames["level"] == 3 and frames["nodes"][1]["plan"] == {"objective": "hold"}


def test_parallel_games_report_the_error_that_happened_not_the_echo(tmp_path, monkeypatch):
    from rsi import native_loop
    def run_episode(arena, task, spec, bridge, root, schedule, abort):
        if spec["seed"] == 2:
            raise ValueError("engine crashed in lane 2")
        assert abort.wait(5)
        raise RuntimeError("another evaluation lane failed")
    class Arena:
        def __init__(self, config, mode): pass
        def close(self): pass
    monkeypatch.setattr(native_loop, "run_episode", run_episode)
    task = {"job_id": "j", "step": 0, "upstream": "u", "engine_digest": "e", "mode": "state",
            "episodes": [{"seed": 1, "level": 2}, {"seed": 2, "level": 3}]}
    with pytest.raises(ValueError, match="engine crashed in lane 2"):
        native_loop.evaluate_task({"work_dir": str(tmp_path), "workers": 2}, task, None, arena_factory=Arena)


def test_single_level_training_plays_a_fresh_seed_even_for_a_won_level(tmp_path, monkeypatch):
    records(tmp_path, "earlier", [(4, "win")])  # already won: single training still runs
    seeds = []
    def play_level(config, bridge, root, name, level, progress=None, level_book=False, seed=None):
        seeds.append(seed)
        return {"level": level, "outcome": "win", "mowers": 0, "score": 1.0, "job": "g"}
    monkeypatch.setattr(portable_loop, "play_level", play_level)
    progress = portable_loop.Progress(tmp_path, "train-x-L4", "train")
    portable_loop.train_one_level({}, Bridge(Script([], [])), tmp_path, "train-x-L4", 4, progress)
    state = json.loads((tmp_path / "training.json").read_text(encoding="utf-8"))
    assert len(seeds) == 1 and seeds[0] != 71004 and state["seed"] == seeds[0] and state["single"] == 4
    assert state["state"] == "complete" and "单独训练完成" in state["message"]


def test_start_can_launch_one_level(dashboard, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard.subprocess, "Popen", Process)
    code, value = dashboard.start_training(tmp_path, 5)
    assert code == 202 and value["name"].endswith("-L5")
    assert dashboard.LAUNCH["process"].args[-2:] == ["--level", "5"]
    dashboard.LAUNCH.clear()
    assert dashboard.start_training(tmp_path, 1)[0] == 400  # the tutorial is not trained


def test_an_unfinished_round_from_an_earlier_run_is_finished_before_replaying(tmp_path, monkeypatch):
    script = Script(["win"] * 8, [{"a": (True, True), "b": (False, False)}])
    script.resume[2] = {"group": "train-old-L2-attempt3", "variants": {"a": "train-old-L2-attempt3a", "b": "train-old-L2-attempt3b"},
                        "evidence_job": "old-game", "reason": "mowers", "mowers": 1}
    names = []
    original = script.rsi_round
    def rsi_round(config, bridge, root, name, progress=None, target=None, create=None, on_status=None):
        names.append(name)
        return original(config, bridge, root, name, progress, target, create, on_status)
    monkeypatch.setattr(portable_loop, "play_level", script.play_level)
    monkeypatch.setattr(portable_loop, "rsi_round", rsi_round)
    progress = portable_loop.Progress(tmp_path, "train-new", "train")
    portable_loop.train({}, Bridge(script), tmp_path, "train-new", progress)
    # 1-2 was not replayed: its old round continued under the old job names and the same evidence.
    assert script.played == [3, 4, 5, 6, 7, 8, 9, 10]
    assert sorted(names) == ["train-old-L2-attempt3a", "train-old-L2-attempt3b"]
    assert {c[1] for c in script.created} == {"old-game"} and {c[2] for c in script.created} == {"train-old-L2-attempt3"}
    attempts = json.loads((tmp_path / "levels" / "2.json").read_text(encoding="utf-8"))["attempts"]
    assert all(a["resumed"] for a in attempts) and [a["kept"] for a in attempts] == [True, False]


def test_client_launch_opens_one_window_running_the_start_script(dashboard, monkeypatch):
    launched = []
    class Window:
        def __init__(self, args, **kwargs):
            launched.append((args, kwargs))
            self.code = None
        def poll(self):
            return self.code
    monkeypatch.setattr(dashboard.subprocess, "Popen", Window)
    dashboard.CLIENT_LAUNCH.clear()
    assert dashboard.launch_client()[0] == 202
    args, kwargs = launched[0]
    assert args[-1].endswith("start_client.ps1") and "-File" in args
    assert dashboard.launch_client()[0] == 409  # still open: no second window
    dashboard.CLIENT_LAUNCH["process"].code = 0
    assert dashboard.launch_client()[0] == 202
