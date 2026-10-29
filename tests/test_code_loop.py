"""Integration coverage without a model service or native emulator build.

Regression assertions fail normally until the corresponding parent fixes land.
"""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsi import code_coach, code_evaluate, code_loop, code_store
from rsi.code_runtime import CandidateProgram
from rsi.code_store import CodeStore, DOMAIN, digest
from rsi.pixel_tools import HELPERS
from rules.code_gate import MIN_PAIRS


def report(seeds, score, source="", marker="development"):
    return {
        "environment": DOMAIN,
        "source_sha256": digest(source),
        "mean": score,
        "episodes": [
            {"seed": seed, "score": score, "outcome": "timeout",
             "player_p95_ms": 1.0,
             "truth": {"seed": seed, "marker": marker, "tick": 100},
             "trace": [{"truth": {"seed": seed, "marker": marker},
                        "observation": {}, "action": None}]}
            for seed in seeds
        ],
    }


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Use real storage, validation and gates; replace only external work."""
    import server.config

    state = SimpleNamespace(
        args=SimpleNamespace(data_dir=tmp_path / "research", rounds=1, baseline_only=False,
                             settings_env=None),
        evaluations=[], prompts=[], evaluate_hook=None, proposal_hook=None,
    )
    state.store = CodeStore(state.args.data_dir)
    state.seed = (code_loop.ROOT / "playbook_seed" / "sim_player.py").read_text(encoding="utf-8")
    state.initial = digest(state.seed)
    # Patch module references, not sys.platform globally (Path uses the latter).
    monkeypatch.setattr(code_loop, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(server.config, "load_settings", lambda *args, **kwargs: SimpleNamespace())

    def evaluate(source_path, seeds, output, *, controls, **kwargs):
        label = output.stem
        source = source_path.read_text(encoding="utf-8")
        state.evaluations.append({"label": label, "seeds": list(seeds),
                                  "version": source_path.stem, "controls": controls})
        score = 0.2 if label.endswith("-base") or label == "baseline" else 0.5
        marker = "HELDOUT_PRIVATE" if "confirmation" in label or "audit" in label else "development"
        result = report(seeds, score, source, marker)
        if state.evaluate_hook:
            state.evaluate_hook(label, result, controls)
        code_store.write_json(output, result)
        return result

    async def propose(source, development, history, settings, **kwargs):
        state.prompts.append(copy.deepcopy({"source": source, "development": development,
                                           "history": history}))
        proposed = source + "\n# candidate %d\n" % len(state.prompts)
        if state.proposal_hook:
            proposed = state.proposal_hook(source, kwargs["controls"])
        return code_coach.CodeProposal(source=proposed, diagnosis="development evidence",
                                      rationale="one test hypothesis"), {"model": "fake"}

    monkeypatch.setattr(code_loop, "evaluate", evaluate)
    monkeypatch.setattr(code_loop, "propose_code", propose)
    state.run = lambda: asyncio.run(code_loop.run(state.args))
    state.status = lambda: json.loads(
        (Path(json.loads((state.store.root / "LATEST_RUN.json").read_text())["path"])
         / "status.json").read_text())
    state.ledger = lambda: [json.loads(line) for line in state.store.ledger_file.read_text().splitlines()]
    return state


def test_promotes_only_after_three_paired_disjoint_stages(harness):
    harness.run()
    calls = harness.evaluations
    assert [c["label"] for c in calls] == [
        f"round1-{stage}-{side}"
        for stage in ("development", "confirmation", "audit") for side in ("base", "candidate")
    ]
    used = set()
    for index, stage in enumerate(("development", "confirmation", "audit")):
        base, candidate = calls[2 * index:2 * index + 2]
        assert base["seeds"] == candidate["seeds"]
        assert len(set(base["seeds"])) == MIN_PAIRS[stage]
        assert used.isdisjoint(base["seeds"])
        used.update(base["seeds"])
        assert base["version"] == harness.initial
    assert harness.store.current() == calls[-1]["version"] != harness.initial
    promotion = next(row for row in harness.ledger() if row["kind"] == "promoted")
    assert [v["stage"] for v in promotion["evidence"]] == ["development", "confirmation", "audit"]
    assert harness.status()["state"] == "complete"
    assert not (harness.store.root / "RUNNING.lock").exists()


@pytest.mark.parametrize("failed_stage,expected_calls", [("development", 2), ("confirmation", 4), ("audit", 6)])
def test_rejected_candidate_retains_incumbent_and_skips_later_stages(harness, failed_stage, expected_calls):
    def reject(label, result, controls):
        if label == f"round1-{failed_stage}-candidate":
            result["mean"] = 0.0
            for episode in result["episodes"]:
                episode["score"] = 0.0

    harness.evaluate_hook = reject
    harness.run()
    assert harness.store.current() == harness.initial
    assert len(harness.evaluations) == expected_calls
    assert not any(row["kind"] == "promoted" for row in harness.ledger())
    assert harness.status()["state"] == "complete"


@pytest.mark.parametrize("failure", ["syntax", "unchanged", "coach", "worker", "timeout"])
def test_failed_proposal_or_candidate_retains_incumbent(harness, failure):
    def proposal(source, controls):
        if failure == "syntax":
            return "import os"
        if failure == "coach":
            raise RuntimeError("model unavailable")
        if failure == "unchanged":
            return source
        return source + "\n# changed\n"

    def evaluation(label, result, controls):
        if label.endswith("-candidate"):
            if failure == "timeout":
                raise TimeoutError("candidate timeout")
            if failure == "worker":
                raise RuntimeError("worker failed")

    harness.proposal_hook = proposal
    harness.evaluate_hook = evaluation
    harness.run()
    assert harness.store.current() == harness.initial
    assert any(row["kind"] == "invalid" for row in harness.ledger())
    assert not any("confirmation" in row["label"] for row in harness.evaluations)
    assert not (harness.store.root / "RUNNING.lock").exists()


def test_normal_holdout_reports_and_scores_never_enter_next_coach_prompt(harness):
    harness.args.rounds = 2
    harness.run()
    assert len(harness.prompts) == 2
    for prompt in harness.prompts:
        assert "HELDOUT_PRIVATE" not in json.dumps(prompt)
        assert "confirmation" not in json.dumps(prompt["history"])
        assert "audit" not in json.dumps(prompt["history"])
    # No holdout scenario is reused for later development/coaching.
    first_holdout = set().union(*(set(c["seeds"]) for c in harness.evaluations
                                if c["label"].startswith("round1-") and "development" not in c["label"]))
    assert first_holdout.isdisjoint(e["seed"] for e in harness.prompts[1]["development"]["episodes"])


@pytest.mark.parametrize("stage", ["confirmation", "audit"])
def test_holdout_errors_never_enter_next_coach_prompt(harness, stage):
    harness.args.rounds = 2

    def fail(label, result, controls):
        if label == f"round1-{stage}-candidate":
            # Worker errors can contain candidate-computed image values/seeds;
            # exception strings are evidence too, not just report score fields.
            raise RuntimeError("HELDOUT_ERROR_CANARY pixel-derived secret=7813")

    harness.evaluate_hook = fail
    harness.run()
    assert len(harness.prompts) == 2
    assert "HELDOUT_ERROR_CANARY" not in json.dumps(harness.prompts[1])


def test_development_summary_redacts_seeds_and_future_schedule_recursively():
    original = report([938713], 0.2)
    original["episodes"][0]["truth"]["nested"] = {
        "schedule": ["future-secret"], "future_spawns": ["future-secret"]}
    original["episodes"][0]["trace"][0]["truth"]["future_spawns"] = ["future-secret"]
    summary = code_coach.development_summary(original)
    encoded = json.dumps(summary)
    assert "938713" not in encoded
    assert "future-secret" not in encoded
    assert original["episodes"][0]["truth"]["seed"] == 938713
    assert summary["games"][0]["truth"]["tick"] == 100


def test_stop_from_coach_is_obeyed_and_incumbent_retained(harness):
    def stop(source, controls):
        (controls / "STOP").touch()
        raise code_evaluate.Interrupted("explicit stop")

    harness.proposal_hook = stop
    harness.run()
    assert len(harness.evaluations) == 1
    assert harness.store.current() == harness.initial
    assert harness.status()["state"] == "stopped"
    assert not (harness.store.root / "RUNNING.lock").exists()


def test_stop_after_final_audit_prevents_promotion(harness):
    def stop(label, result, controls):
        if label == "round1-audit-candidate":
            (controls / "STOP").touch()

    harness.evaluate_hook = stop
    harness.run()
    assert harness.store.current() == harness.initial
    assert harness.status()["state"] == "stopped"


def test_stop_interrupts_pause_without_waiting(tmp_path, monkeypatch):
    (tmp_path / "STOP").touch()
    (tmp_path / "PAUSE").touch()
    monkeypatch.setattr(code_evaluate, "time", SimpleNamespace(
        sleep=lambda _: pytest.fail("STOP must interrupt PAUSE immediately")))
    with pytest.raises(code_evaluate.Interrupted):
        code_evaluate.control(tmp_path)


class FakeProcess:
    def __init__(self, finished=False):
        self.returncode = 0 if finished else None
        self.killed = False
        self.waited = False
        self.on_poll = None

    def poll(self):
        if self.on_poll:
            self.on_poll()
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self):
        self.waited = True
        return self.returncode


@pytest.fixture
def worker_fixture(tmp_path, monkeypatch):
    source = tmp_path / "candidate.py"
    source.write_text("def perceive(image):\n    return {}\ndef decide(obs, memory):\n    return None\n")
    controls = tmp_path / "controls"
    controls.mkdir()
    state = SimpleNamespace(source=source, output=tmp_path / "report.json", controls=controls,
                            process=FakeProcess(), now=0.0, on_sleep=None, launch=None,
                            on_launch=None, on_finish=None, completed_report=None,
                            report_written=False)

    def finish():
        if state.process.returncode == 0 and not state.report_written:
            if state.completed_report is not None:
                code_store.write_json(state.output, state.completed_report)
            state.report_written = True
            if state.on_finish:
                state.on_finish()

    state.process.on_poll = finish

    def popen(command, **kwargs):
        state.launch = (command, kwargs)
        if state.on_launch:
            state.on_launch()
        return state.process

    def sleep(seconds):
        state.now += seconds
        if state.on_sleep:
            state.on_sleep()
        assert state.now < 2, "monitor did not stop within the deterministic test clock"

    monkeypatch.setattr(code_evaluate.subprocess, "Popen", popen)
    monkeypatch.setattr(code_evaluate, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=sleep))
    state.run = lambda: code_evaluate.evaluate(source, [1], state.output, controls=controls, timeout=0.25)
    return state


def test_worker_timeout_kills_and_reaps_and_strips_secret_environment(worker_fixture, monkeypatch):
    monkeypatch.setenv("TEST_PRIVATE_API_KEY", "do-not-inherit")
    with pytest.raises(TimeoutError):
        worker_fixture.run()
    assert worker_fixture.process.killed and worker_fixture.process.waited
    command, options = worker_fixture.launch
    assert "rsi.code_evaluate" in command
    assert "TEST_PRIVATE_API_KEY" not in options["env"]
    request = json.loads(worker_fixture.output.with_suffix(".request.json").read_text())
    assert request["seeds"] == [1]
    assert Path(request["controls"]) == worker_fixture.controls.resolve()


def test_worker_stop_kills_and_reaps_even_during_pause(worker_fixture):
    worker_fixture.on_launch = lambda: (worker_fixture.controls / "PAUSE").touch()
    worker_fixture.on_sleep = lambda: (worker_fixture.controls / "STOP").touch()
    with pytest.raises(code_evaluate.Interrupted):
        worker_fixture.run()
    assert worker_fixture.process.killed and worker_fixture.process.waited


def test_pause_time_does_not_consume_worker_timeout(worker_fixture):
    worker_fixture.on_launch = lambda: (worker_fixture.controls / "PAUSE").touch()
    expected = report([1], 0.2, worker_fixture.source.read_text())
    worker_fixture.completed_report = expected

    def resume_and_finish():
        if worker_fixture.now >= 0.6:
            (worker_fixture.controls / "PAUSE").unlink()
            worker_fixture.process.returncode = 0

    worker_fixture.on_sleep = resume_and_finish
    assert worker_fixture.run() == expected
    assert worker_fixture.process.waited and not worker_fixture.process.killed


def test_completed_worker_report_is_not_accepted_after_stop(worker_fixture):
    worker_fixture.process.returncode = 0
    worker_fixture.completed_report = report([1], 0.2, worker_fixture.source.read_text())
    worker_fixture.on_finish = lambda: (worker_fixture.controls / "STOP").touch()
    with pytest.raises(code_evaluate.Interrupted):
        worker_fixture.run()
    assert worker_fixture.process.waited


def test_stop_before_worker_launch_does_not_spawn_process(worker_fixture):
    (worker_fixture.controls / "STOP").touch()
    with pytest.raises(code_evaluate.Interrupted):
        worker_fixture.run()
    assert worker_fixture.launch is None


def test_preexisting_report_is_rejected_without_worker_launch(worker_fixture):
    code_store.write_json(worker_fixture.output, report([1], 0.2, worker_fixture.source.read_text()))
    with pytest.raises(FileExistsError):
        worker_fixture.run()
    assert worker_fixture.launch is None


def test_failed_promotion_write_retains_incumbent(harness, monkeypatch):
    original_log = CodeStore.log

    def fail_promotion_log(self, **entry):
        if entry.get("kind") == "promoted":
            raise OSError("promotion ledger write failed")
        return original_log(self, **entry)

    monkeypatch.setattr(CodeStore, "log", fail_promotion_log)
    harness.run()
    assert harness.store.current() == harness.initial


def test_startup_metadata_failure_does_not_leave_running_lock(harness, monkeypatch):
    original_write = code_loop.write_json

    def fail_latest(path, value):
        if path.name == "LATEST_RUN.json":
            raise OSError("metadata write failed")
        return original_write(path, value)

    monkeypatch.setattr(code_loop, "write_json", fail_latest)
    with pytest.raises(OSError, match="metadata write failed"):
        harness.run()
    assert not (harness.store.root / "RUNNING.lock").exists()


@pytest.mark.parametrize("claimed_hash", [None, "0" * 64])
def test_worker_rejects_missing_or_mismatched_source_hash(worker_fixture, claimed_hash):
    worker_fixture.process.returncode = 0
    result = report([1], 0.2, worker_fixture.source.read_text())
    assert result["source_sha256"] != claimed_hash
    if claimed_hash is None:
        result.pop("source_sha256")
    else:
        result["source_sha256"] = claimed_hash
    worker_fixture.completed_report = result
    with pytest.raises((ValueError, RuntimeError), match="(?i)(hash|digest|source|identity)"):
        worker_fixture.run()
    assert worker_fixture.process.waited


@pytest.mark.parametrize("returned_seeds", [[], [2], [1, 1]])
def test_worker_rejects_different_or_missing_episode_seeds(worker_fixture, returned_seeds):
    worker_fixture.process.returncode = 0
    worker_fixture.completed_report = report(returned_seeds, 0.2, worker_fixture.source.read_text())
    with pytest.raises(RuntimeError, match="seeds"):
        worker_fixture.run()
    assert worker_fixture.process.waited


def test_mismatched_candidate_report_hash_cannot_promote(harness, monkeypatch):
    mismatched = []

    def worker(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text())
        source = Path(request["source"]).read_text(encoding="utf-8")
        output = Path(request["output"])
        label = output.stem
        result = report(request["seeds"], 0.5 if label.endswith("-candidate") else 0.2, source)
        if label.endswith("-candidate"):
            assert result["source_sha256"] != harness.initial
            # The scores claim to belong to the incumbent, not the candidate.
            # All seeds/environments remain paired and every score gate passes.
            result["source_sha256"] = harness.initial
            mismatched.append(label)
        process = FakeProcess(finished=True)
        # Simulate the worker writing its report as it finishes, after the
        # real evaluator has checked for stale files and launched the process.
        def finish():
            code_store.write_json(output, result)
            process.on_poll = None
        process.on_poll = finish
        return process

    # Retain the real evaluator's validation; mock only the process boundary.
    monkeypatch.setattr(code_loop, "evaluate", code_evaluate.evaluate)
    monkeypatch.setattr(code_evaluate.subprocess, "Popen", worker)
    harness.run()
    assert mismatched, "the test must exercise a candidate report"
    assert harness.store.current() == harness.initial
    assert not any(row["kind"] == "promoted" for row in harness.ledger())


def test_seed_player_runs_against_real_pixel_helpers_and_renderer():
    from sim.render import PLANTS, render

    source = (code_loop.ROOT / "playbook_seed" / "sim_player.py").read_text(encoding="utf-8")
    candidate = CandidateProgram(source, HELPERS)
    cards = [{"plant": name, "ready": index == 0} for index, name in enumerate(PLANTS)]
    frame = render(sun=50, plants=[], zombies=[], cards=cards, tick=0, limit=18000, outcome="running")
    observation = candidate.perceive(frame)
    assert observation["sun"] == 50
    assert observation["plants"] == observation["zombies"] == []
    assert candidate.decide(observation, {}) == {"plant": "sunflower", "row": 0, "col": 0}
    plants = [{"plant": "sunflower", "row": 0, "col": 0}]
    frame = render(sun=0, plants=plants, zombies=[{"type": "zombie", "row": 3, "x": 200}],
                   cards=[{"plant": name, "ready": False} for name in PLANTS],
                   tick=100, limit=18000, outcome="running")
    observation = candidate.perceive(frame)
    assert observation["plants"] == plants
    assert observation["zombies"] == [{"row": 3, "col": 2.0}]
    assert candidate.decide(observation, {}) is None


def test_episode_runner_resets_memory_and_keeps_truth_out_of_candidate(monkeypatch):
    import sim.emulator

    episodes = []

    class Emulator:
        def __init__(self, seed, ticks):
            self.seed, self.steps, self.done, self.actions = seed, 0, False, []
            episodes.append(self)

        def frame(self):
            return [self.steps]

        def step(self, action, ticks):
            self.actions.append(copy.deepcopy(action))
            self.steps += 1
            self.done = self.steps == 2
            return {"advanced_ticks": ticks, "done": self.done}

        def truth(self):
            return {"seed": self.seed, "scheduled": 1, "killed": 0,
                    "outcome": "timeout" if self.done else "running", "tick": self.steps * 100,
                    "private": "JUDGE_SECRET", "environment": DOMAIN,
                    "engine": {"revision": "fake-native-engine"}}

    monkeypatch.setattr(sim.emulator, "EmulatorEpisode", Emulator)
    source = '''
def perceive(image):
    return {"step": image[0]}
def decide(observation, memory):
    memory["count"] = memory.get("count", 0) + 1
    return {"plant": "sunflower", "row": memory["count"] - 1, "col": observation["step"]}
'''
    result = code_evaluate.run_episodes(source, [11, 22])
    assert result["source_sha256"] == digest(source)
    assert len(result["episodes"]) == 2
    for episode in episodes:
        assert episode.actions == [{"plant": "sunflower", "row": 0, "col": 0},
                                   {"plant": "sunflower", "row": 1, "col": 1}]
    for episode in result["episodes"]:
        for item in episode["trace"]:
            assert set(item["observation"]) == {"step"}
            assert "JUDGE_SECRET" not in json.dumps(item["observation"])
