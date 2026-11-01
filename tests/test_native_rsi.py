import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from rsi.native_gate import SCORE_VERSION, compare, episode_score
from rsi.native_jobs import NativeJobs, STEPS, digest
from rsi.native_runtime import player_state, preflight
from rsi.native_bridge import Bridge
from rsi.native_loop import evaluate_task, Stopped, control
from rsi.coach import Proposal
from tests.test_server import settings


def report(jobs, job, value=.1, outcome="lose"):
    task = jobs.request(job)
    return {"request_digest":digest(task), "version":task["version"],
            "environment":{"backend":"pvz_rl", "upstream":task["upstream"], "mode":task["mode"],
                           "score_version":SCORE_VERSION,"engine_digest":task["engine_digest"],
                           "game_sha256":"a"*64,"dll_sha256":"b"*64,"worker_source":"c"*64},
            "episodes":[{**e,"outcome":outcome,"score":value,"complete":True,"seed_applied":True,
                         "mower_coverage":True,"model_errors":0,"invalid_actions":0,"trace":[],
                         "progress":value/.2,"elapsed_game_s":200,"mower_times":[]}
                        for e in task["episodes"]],"mean":value}


def attach_candidate(jobs, job):
    parent = jobs.store.load(job["parent"])
    candidate = Proposal(planner={"knowledge":"Test strategy"},rationale="Fixture only").apply(parent,jobs.store.next_version())
    jobs.store.write(candidate)
    job.update(candidate=candidate.version, candidate_digest=digest(candidate.model_dump()),status="evaluating")
    jobs.save(job)


def test_full_cycle_requires_state_confirmation_and_pixel_audit_before_atomic_promotion(settings):
    jobs = NativeJobs(settings)
    job = jobs.enqueue("finished-windows-test")
    parent = jobs.store.current()
    seeds = [e["seed"] for suite in job["suites"].values() for e in suite]
    assert len(seeds) == len(set(seeds))
    for index, (_, mode, side) in enumerate(STEPS):
        assert jobs.store.current() == parent
        if index == 2:
            attach_candidate(jobs,job)
        result = report(jobs,job, .18 if side == "candidate" else .1)
        job = jobs.accept(job,result)
    assert job["promoted"] and jobs.store.current() == job["candidate"]
    assert len(job["verdicts"]) == 3
    assert jobs.enqueue(job["id"])["candidate"] == job["candidate"]
    assert jobs.next_job() is None
    # Crash after CURRENT write but before the final job checkpoint is recoverable.
    job.update(status="evaluating",promoted=False)
    jobs.save(job)
    assert jobs.next_job() is None
    assert jobs.load(job["id"])["promoted"]


def test_pixel_audit_regression_retains_incumbent(settings):
    jobs = NativeJobs(settings); job = jobs.enqueue("failed-audit")
    for index, (stage, mode, side) in enumerate(STEPS):
        if index == 2:
            attach_candidate(jobs,job)
        value = .05 if stage == "audit" and side == "candidate" else .18 if side == "candidate" else .1
        job = jobs.accept(job,report(jobs,job,value))
    assert job["status"] == "rejected" and not job["promoted"]
    assert jobs.store.current() == job["parent"]


@pytest.mark.parametrize("change", ["timeout","seed","mowers","model","version","request","backend","fixture","score","identity"])
def test_bad_or_partial_evidence_cannot_advance(settings,change):
    jobs = NativeJobs(settings); job = jobs.enqueue("invalid-evidence")
    data = report(jobs,job)
    if change == "timeout": data["episodes"][0]["outcome"] = "timeout"
    if change == "seed": data["episodes"][0]["seed_applied"] = False
    if change == "mowers": data["episodes"][0]["mower_coverage"] = False
    if change == "model": data["episodes"][0]["model_errors"] = 1
    if change == "version": data["version"] = "v999"
    if change == "request": data["request_digest"] = "wrong"
    if change == "backend": data["environment"]["backend"] = "emulator"
    if change == "fixture": data["environment"]["fixture"] = True
    if change == "score": data["episodes"][0]["score"] = .99
    if change == "identity": data["environment"].pop("dll_sha256")
    with pytest.raises(ValueError): jobs.accept(job,data)
    assert jobs.load(job["id"])["step"] == 0


def test_changed_current_refuses_stale_promotion(settings):
    jobs=NativeJobs(settings); job=jobs.enqueue("stale")
    data=report(jobs,job)
    attach_candidate(jobs,job)
    jobs.store.set_current(job["candidate"])
    with pytest.raises(ValueError,match="CURRENT changed"):
        jobs.accept(job,data)
    with pytest.raises(ValueError,match="CURRENT changed"):
        jobs.store.promote_if_current(job["parent"],job["parent"])


def raw():
    return {"global_state":{"scene":0,"grid_rows":5,"sun":125,"zombie_spawn_plan":[999]},
            "cards":[{"slot":0,"plant_type":0,"ready":True}],
            "plants":[{"type":1,"row":4,"col":0}],
            "zombies":[{"type":0,"row":4,"x":390,"hp":200}],"seed":98765,"score":123}


def test_state_whitelist_drops_future_schedule_judge_hp_and_seed_and_keeps_native_mask():
    mask=[False]*757; mask[1+4*9+2]=True
    value=player_state(raw(),mask,100)
    assert value["scene"]["sun"] == 125
    assert value["allowed_positions"] == [[0,4,2]]
    assert not any(key in json.dumps(value) for key in ("spawn_plan","98765",'"hp"','"score"'))
    assert value["scene"]["cards"][0]["plant"] == "peashooter"


def test_pixel_arm_cannot_receive_state_or_native_legality(settings):
    async def exercise():
        bridge=Bridge(settings)
        try:
            job=bridge.jobs.enqueue("pixel-isolation")
            job=bridge.jobs.accept(job,report(bridge.jobs,job))
            task=bridge.jobs.request(job)
            assert task["mode"] == "pixels"
            with pytest.raises(ValueError,match="state leaked"):
                await bridge.decide(job,{"request_digest":digest(task),"tick":0,"scene":{}})
        finally:
            await bridge.close()
    asyncio.run(exercise())


def test_scoring_penalizes_early_mower_and_refuses_partial_game():
    assert episode_score("win",1,200,[20]) < episode_score("win",1,200,[150])
    with pytest.raises(ValueError): episode_score("timeout",1,600,[])


def test_explicit_stop_is_not_auto_recovered(tmp_path):
    (tmp_path/"PAUSE").touch(); (tmp_path/"STOP").touch()
    with pytest.raises(Stopped): control(tmp_path)
    assert (tmp_path/"PAUSE").exists() and (tmp_path/"STOP").exists()


def test_worker_resumes_completed_episodes_without_replaying_or_reopening_game(settings,tmp_path):
    jobs=NativeJobs(settings); task=jobs.request(jobs.enqueue("fixture-run"))
    counters={"opened":0,"steps":0,"closed":0}
    class Arena:
        def __init__(self,*args): counters["opened"]+=1
        def reset(self,level,seed):
            return {"rng_seed_support":2,"rng_seed_applied":True,"level_id":level,"playthrough":1,
                    "game_clock":0,"observation_quality":"fresh","replay_eligible":True,
                    "raw_observation":raw(),"action_mask":[True]*757}
        def step(self,placement):
            counters["steps"]+=1
            return True,False,{"game_clock":100,"result":"WON","observation_quality":"fresh","replay_eligible":True,
                "action_result":{"success":True},"events":{"mowers_used":0,"mower_triggered_rows":[]},
                "raw_observation":{"global_state":{"current_wave":10,"total_waves":10}}}
        def close(self): counters["closed"]+=1
    class RPC:
        def call(self,op,**data):
            return dict(placement=None,plan={"objective":"Fixture"},plan_tick=0,spent=0,timings={},scene=data["scene"],plants=data["plants"])
    config={"work_dir":str(tmp_path/"worker")}
    first=evaluate_task(config,task,RPC(),arena_factory=Arena)
    second=evaluate_task(config,task,RPC(),arena_factory=Arena)
    assert first == second and counters == {"opened":1,"steps":8,"closed":1}
    assert all(ep["outcome"]=="win" for ep in first["episodes"])


def test_changed_model_settings_expire_job_instead_of_blocking_queue(settings):
    jobs = NativeJobs(settings)
    job = jobs.enqueue("old-config")
    changed = NativeJobs(replace(settings, qwen_model="different-model"))
    assert changed.next_job() is None
    assert changed.load(job["id"])["status"] == "stale"


def test_native_identity_cannot_change_across_stages(settings):
    jobs = NativeJobs(settings); job = jobs.enqueue("changed-runtime")
    job = jobs.accept(job, report(jobs, job))
    data = report(jobs, job)
    data["environment"]["dll_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="runtime changed"):
        jobs.accept(job, data)


def test_next_is_read_only_and_paused_job_cannot_generate_candidate(settings, monkeypatch):
    async def exercise():
        bridge = Bridge(settings)
        try:
            job = bridge.jobs.enqueue("paused-coach")
            for _ in range(2):
                job = bridge.jobs.accept(job, report(bridge.jobs, job))
            async def forbidden(*args):
                pytest.fail("Coach called without prepared runtime or during pause")
            monkeypatch.setattr(bridge, "coach", forbidden)
            assert (await bridge.handle({"op":"next"}))["candidate"] is None
            await bridge.handle({"op":"control", "job_id":job["id"], "action":"pause"})
            assert (await bridge.handle({"op":"next"}))["paused"]
            with pytest.raises(RuntimeError, match="paused"):
                await bridge.handle({"op":"prepare", "job_id":job["id"]})
        finally:
            await bridge.close()
    asyncio.run(exercise())


def test_missing_runtime_never_calls_models_or_evaluator(settings, tmp_path, monkeypatch):
    from rsi.native_loop import run
    jobs = NativeJobs(settings); job = jobs.enqueue("waiting-for-game")
    calls = []
    class RPC:
        def __init__(self, *args): pass
        def call(self, op, **payload):
            calls.append(op)
            if op == "next": return job
            if op == "status": return None
            pytest.fail("unexpected operation while runtime missing: " + op)
        def close(self): calls.append("closed")
    def missing(config): raise RuntimeError("Missing compatible game")
    monkeypatch.setattr("rsi.native_loop.preflight", missing)
    run({"work_dir":str(tmp_path / "worker")}, bridge_factory=RPC)
    assert calls == ["next", "status", "closed"]


def test_prepared_development_automatically_asks_coach_once_and_retains_parent(settings, monkeypatch):
    calls = []
    async def fake_propose(book, evidence, ledger, settings, **options):
        calls.append(evidence)
        assert options["strategy_only"]
        assert "state_development" in evidence and "pixels_development" in evidence
        assert '"seed":' not in json.dumps(evidence)
        return Proposal(planner={"knowledge":"模型提出的测试策略"}, rationale="来自开发集"), None
    monkeypatch.setattr("rsi.native_bridge.propose", fake_propose)
    monkeypatch.setattr("rsi.native_bridge.select_session", lambda *args: None)
    monkeypatch.setattr("rsi.native_bridge.read_events", lambda *args: [])
    monkeypatch.setattr("rsi.native_bridge.build_evidence", lambda *args: {"diagnosis":"fixture"})
    async def exercise():
        bridge = Bridge(settings)
        try:
            job = bridge.jobs.enqueue("coach-once")
            for _ in range(2): job = bridge.jobs.accept(job, report(bridge.jobs, job))
            result = await bridge.handle({"op":"prepare", "job_id":job["id"]})
            assert result["candidate"] and bridge.jobs.store.current() == job["parent"]
            again = await bridge.handle({"op":"prepare", "job_id":job["id"]})
            assert again["candidate"] == result["candidate"] and len(calls) == 1
        finally: await bridge.close()
    asyncio.run(exercise())
