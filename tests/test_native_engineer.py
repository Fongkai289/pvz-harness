import asyncio
from dataclasses import replace
import json
import subprocess
import time

import pytest

from rsi.code_bundle import compile_bundle, unpack_source
from rsi.code_coach import CoachTask
from rsi.code_engineer import EngineerAction
from rsi.native_engineer import NativeEngineer
from rsi.native_jobs import NativeJobs, STEPS, digest
from rsi.native_player import NativeCodeStore, PlayerRuntime, SEED, execute, player_input
from rsi.native_bridge import Bridge
from server.action_selector import Selection
from server.planner import Plan, PlanResult
from tests.test_native_rsi import report, raw
from tests.test_server import settings
from tests.test_action_selector import session
from rsi.native_runtime import player_state

CHANGED = '''def decide(observation, memory):
    memory['calls'] = memory.get('calls', 0) + 1
    threats = observation['scene']['zombies']
    rows = [z['row'] for z in threats]
    return {'planner_context': {'observed_threat_rows': rows},
            'jev_context': {'tracked_calls': memory['calls'], 'observed_threat_rows': rows},
            'request_replan': len(rows) > 0}
'''


def changed_source():
    files = unpack_source(SEED, ())
    files['player/policy.py'] = CHANGED
    return compile_bundle(files, ())


def observation():
    from server.scene import Scene
    state = player_state(raw(), [True]*757, 100)
    return player_input(Scene.model_validate(state['scene']),state['plants'],None,0,100)


def test_actual_isolated_code_runs_and_keeps_episode_memory():
    async def run():
        player = PlayerRuntime(changed_source())
        first = await player.step(observation())
        second = await player.step(observation())
        assert first['jev_context']['observed_threat_rows'] == [4]
        assert second['jev_context']['tracked_calls'] == 2
        player.reset()
        assert (await player.step(observation()))['jev_context']['tracked_calls'] == 1
    asyncio.run(run())


def test_code_cannot_return_planting_or_read_host_files():
    bad = "def perceive(image):\n    return image\ndef decide(observation,memory):\n    return {'slot':0,'row':4,'col':3}\n"
    with pytest.raises(ValueError,match="no operation authority"):
        execute(bad,observation(),{})
    with pytest.raises(ValueError):
        execute(bad.replace("return image","return open('.env')"),observation(),{})


def test_candidate_resource_abuse_is_bounded():
    bad = "def perceive(image):\n    return image\ndef decide(observation,memory):\n    return decide(observation,memory)\n"
    with pytest.raises((ValueError,RuntimeError,subprocess.TimeoutExpired)):
        execute(bad,observation(),{},timeout=2)


def test_code_store_detects_post_evaluation_modification(tmp_path):
    store = NativeCodeStore(tmp_path)
    version = store.add(changed_source())
    assert store.source(version) == changed_source()
    (store.root/(version+'.py')).write_text(SEED,encoding='utf-8')
    with pytest.raises(ValueError,match="changed"):
        store.source(version)


@pytest.mark.parametrize('dynamic', [False, True])
def test_coach_engineer_windows_test_reconnect_repair_finish_and_promotion(settings,monkeypatch,dynamic):
    jobs = NativeJobs(replace(settings,native_evolution='code'))
    job = jobs.enqueue('code-native')
    for _ in range(2): job = jobs.accept(job,report(jobs,job))
    calls=[]
    edit = EngineerAction(action='edit_modules',explanation='merge and split the seed modules',
        writes={'player/vision/input.py':'def perceive(image):\n    return image\n',
                'player/strategy.py':CHANGED}, deletes=list(unpack_source(SEED, ()))) if dynamic else EngineerAction(
        action='write_file',explanation='derive threats',path='player/policy.py',content=CHANGED)
    actions=iter([
        EngineerAction(action='read_file',explanation='read',path='docs/contract.txt'),
        edit,
        EngineerAction(action='test_candidate',explanation='test'),
        EngineerAction(action='finish',explanation='submit',diagnosis='observed threats',rationale='tested native code')])
    async def model(role,messages,schema,*args,**kwargs):
        calls.append((role,json.dumps(messages)))
        if role=='coach': return CoachTask(diagnosis='hypothesis',evidence=['development'],objective='improve'), {'role':role}
        return next(actions), {'role':role}
    monkeypatch.setattr('rsi.native_engineer.request_role',model)
    monkeypatch.setattr('rsi.code_engineer.evaluate',lambda *a,**k:pytest.fail('Emulator evaluator must never run'))
    async def run():
        nonlocal job
        driver=NativeEngineer(jobs)
        for _ in range(4):
            job=await driver.advance(jobs.load(job['id']),{'review':'fixture'})
        task=jobs.request(job)
        assert task['engineering_test']==1 and task['book']['player_code']
        assert jobs.store.current()==job['parent'] and job['candidate'] is None
        # Recreate service object: pending Windows evaluation survives disconnect.
        fresh=NativeJobs(jobs.settings)
        assert fresh.request(fresh.load(job['id']))==task
        result=report(fresh,job,.18)
        job=fresh.accept(job,result)
        assert job['engineering']['tested_version']==task['book']['player_code']
        job=await NativeEngineer(fresh).advance(job,None)
        assert job['candidate'] and not job['promoted']
        assert fresh.store.load(job['candidate']).planner == fresh.store.load(job['parent']).planner
        assert fresh.store.load(job['candidate']).player_code==task['book']['player_code']
        for stage, mode, side in STEPS[2:]:
            job=fresh.accept(job,report(fresh,job,.18 if side=='candidate' else .1))
        assert job['promoted'] and fresh.store.load().player_code==task['book']['player_code']
        promoted = fresh.code.source(fresh.store.load().player_code)
        if dynamic:
            assert set(unpack_source(promoted, ())) == {'player/vision/input.py','player/strategy.py'}
        assert (await PlayerRuntime(promoted).step(observation()))['jev_context']['observed_threat_rows'] == [4]
        assert 'engineering' not in fresh.latest()
    asyncio.run(run())
    assert [r for r,_ in calls]==['coach','engineer','engineer','engineer','engineer']
    assert all('"seed":' not in messages for _,messages in calls)


def test_unfinished_or_failed_test_cannot_authorize_finish(settings,monkeypatch):
    jobs=NativeJobs(replace(settings,native_evolution='code')); job=jobs.enqueue('failed-test')
    for _ in range(2): job=jobs.accept(job,report(jobs,job))
    actions=iter([
        EngineerAction(action='write_file',explanation='edit',path='player/policy.py',content=CHANGED),
        EngineerAction(action='test_candidate',explanation='test'),
        EngineerAction(action='finish',explanation='submit',diagnosis='guess',rationale='guess')])
    async def model(role,*args,**kwargs):
        if role=='coach': return CoachTask(diagnosis='hypothesis',evidence=['development'],objective='improve'),{}
        return next(actions),{}
    monkeypatch.setattr('rsi.native_engineer.request_role',model)
    async def run():
        nonlocal job
        driver=NativeEngineer(jobs)
        for _ in range(3): job=await driver.advance(job,{})
        job=driver.feedback(job,error='native executor failed')
        job=await driver.advance(job,None)
        assert job['candidate'] is None and jobs.store.current()==job['parent']
        assert 'exact current' in job['engineering']['messages'][-1]['content']
    asyncio.run(run())


def test_executed_candidate_context_reaches_planner_and_jev_but_not_actions(settings,monkeypatch):
    async def run():
        bridge=Bridge(settings)
        try:
            code=bridge.jobs.code.add(changed_source())
            original=bridge.jobs.store.load()
            book=original.model_copy(update={'version':'v002','parent':original.version,'player_code':code})
            bridge.jobs.store.write(book); bridge.jobs.store.set_current(book.version)
            job=bridge.jobs.enqueue('execute-through-models'); task=bridge.jobs.request(job)
            contexts=[]
            async def plan(scene,knowledge,context):
                contexts.append(context['system0_derived_context'])
                return PlanResult(plan=Plan(objective='fixture'),latency=0)
            async def select(ctx,policy,*,mode):
                assert mode == 'flat'
                assert ctx.tool_context['observed_threat_rows']==[4]
                return Selection(placement=None,reason='Jev chose wait')
            monkeypatch.setattr(bridge.planner,'decide',plan)
            monkeypatch.setattr(bridge.selector,'select',select)
            result=await bridge.decide(job,{'request_digest':digest(task),'tick':100,'episode_id':'unique',
                                          **player_state(raw(),[True]*757,100)})
            assert result['placement'] is None and contexts[0]['observed_threat_rows']==[4]
        finally: await bridge.close()
    asyncio.run(run())


def test_live_session_loads_promoted_code_and_preserves_jev_pause_authority(session,monkeypatch):
    from server.session import Session
    code=NativeCodeStore(session.settings.data_dir).add(changed_source())
    book=session.playbook.model_copy(update={'player_code':code})
    live=Session(session.ws,replace(session.settings,planner_mode='split'),session.planner,
                 session.selector,book,'live-system0')
    for name in ('latest_scene','latest_obs','directive','latest_recv','scene_received','directive_received'):
        setattr(live,name,getattr(session,name))
    live.looker=object(); live.seen_plants_frame=1; live.plants_received=time.monotonic()
    calls=[]
    async def plan(scene,knowledge,jpeg=None,context=None):
        calls.append(('planner',context['system0_derived_context']))
        return PlanResult(plan=Plan(objective='fixture'),latency=0)
    async def select(ctx,policy):
        calls.append(('jev',ctx.tool_context))
        return Selection(placement=None,reason='Jev chose wait')
    monkeypatch.setattr(live.planner,'decide',plan)
    monkeypatch.setattr(live.selector,'select',select)
    async def run():
        await live._plan(live.latest_obs,b'')
        live.latest_recv=live.scene_received=live.directive_received=time.monotonic()
        assert await live._jev_step(live.latest_obs) is None
        live.paused=True
        assert await live._jev_step(live.latest_obs) is None
    try:
        asyncio.run(run())
        assert [name for name,_ in calls]==['planner','jev']
        assert calls[1][1]['observed_threat_rows']==[2]
        assert not live.ws.sent
    finally: live.close()


def test_explicit_retry_preserves_old_evidence_and_restarts_with_code_mode(settings):
    old=NativeJobs(settings); previous=old.enqueue('old-session')
    new=NativeJobs(replace(settings,native_evolution='code'))
    job=new.enqueue('old-session',retry=True)
    assert job['id'] != previous['id'] and job['source_session']=='old-session'
    assert job['step']==0 and job['evolution']=='code'
    assert new.load('old-session')['status']=='stale'
    assert new.enqueue('old-session',retry=True)['id']==job['id']
