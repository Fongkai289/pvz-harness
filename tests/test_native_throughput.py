"""Controlled facts/model stubs verify execution, not real-game throughput."""
import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import io
import json
import subprocess
import sys
import threading

import pytest

from rsi.native_schedule import DecisionClock, DecisionSchedule
from rsi.native_loop import run_episode, evaluate_task, Stopped
from rsi.native_transport import JsonRPC
from rsi.native_bridge import serve, Bridge
from rsi.native_player import PlayerRuntime, PersistentWorker
from rsi.native_runtime import player_state
from rsi.native_jobs import NativeJobs, digest
from rules.playbook_rules import JevPolicy
from server.action_selector import Selection
from server.planner import Plan, PlanResult
from tests.test_action_selector import context, selector
from tests.test_native_rsi import raw
from tests.test_native_engineer import changed_source, observation
from tests.test_server import settings


def public():
    return player_state(raw(), [True]*757, 0)


@pytest.mark.parametrize('event', ['new_threat','threat_advance','plant_lost','resource_ready'])
def test_events_wake_before_period_but_respect_minimum(event):
    before, after = public(), public()
    if event == 'new_threat':
        after['scene']['zombies'].append({'type':'normal','row':0,'col':8})
    if event == 'threat_advance':
        after['scene']['zombies'][0]['col'] -= 1
    if event == 'plant_lost':
        after['plants'] = []
    if event == 'resource_ready':
        before['scene']['sun'] = 0
    clock = DecisionClock()
    assert clock.due(0,before) == ['initial']
    clock.committed(0,before)
    assert clock.due(25,after) == []
    assert event in clock.due(50,after)
    assert event in clock.due(200,after)
    assert 'periodic' in clock.due(200,after)
    clock.committed(50,after)
    assert clock.due(75,after) == []


def test_disabled_events_still_have_heartbeat_and_pixels_cannot_use_state():
    clock = DecisionClock({'events':[]})
    clock.committed(0,public())
    assert clock.due(199,None) == []
    assert clock.due(200,None) == ['periodic']


@pytest.mark.parametrize('schedule', [{'seconds':0}, {'seconds':float('inf')}, {'seconds':11},
                                    {'event_min_seconds':0}, {'events':['plant_now']}, {'selection':'rules'}])
def test_scheduler_cannot_remove_heartbeat_or_add_operations(schedule):
    with pytest.raises(ValueError):
        DecisionSchedule.model_validate(schedule)


class Arena:
    """Two game seconds, eight simulation steps, static visible facts."""
    def __init__(self, *args):
        self.actions = []
    def reset(self, level, seed):
        self.tick = 0
        return dict(rng_seed_support=2,rng_seed_applied=True,level_id=level,playthrough=1,**self.info())
    def info(self):
        facts = raw()
        facts['global_state'].update(current_wave=10,total_waves=10)
        return dict(game_clock=self.tick,result='WON',observation_quality='fresh',replay_eligible=True,
                    raw_observation=facts,action_mask=[True]*757,action_result={'success':True},
                    events={'mowers_used':0,'mower_triggered_rows':[]})
    def step(self, placement):
        self.actions.append(placement)
        self.tick += 25
        return self.tick>=200,False,self.info()
    def pixels(self):
        return b'fixture-image'
    def close(self):
        pass


class RPC:
    def __init__(self):
        self.calls = []
    def call(self, op, **payload):
        self.calls.append(payload)
        return dict(placement={'slot':0,'row':4,'col':2}, plan={'objective':'fixture','valid_for_seconds':60},
                    plan_tick=0,spent=100,timings={'jev_s':.23,'jev_requests':1},
                    scene=payload.get('scene',{}),plants=payload.get('plants',[]))


def test_model_called_once_and_chosen_plant_never_replayed_between_decisions(tmp_path):
    arena, rpc = Arena(), RPC()
    result = run_episode(arena,{'job_id':'fixture','mode':'state'}, {'level':2,'seed':17},rpc,tmp_path)
    assert len(rpc.calls)==1 and len(arena.actions)==8
    assert arena.actions[0] == {'slot':0,'row':4,'col':2}
    assert arena.actions[1:] == [None]*7
    assert result['throughput']['decisions']==1 and result['throughput']['jev_requests']==1
    assert result['throughput']['timings']['environment_step_s']['count']==8


def test_pixels_never_use_hidden_state_for_scheduler(tmp_path):
    arena, rpc = Arena(), RPC()
    run_episode(arena,{'job_id':'fixture','mode':'pixels'}, {'level':2,'seed':17},rpc,tmp_path)
    assert len(rpc.calls)==1
    assert not {'scene','plants','allowed_positions'} & rpc.calls[0].keys()


def test_stop_arriving_with_inference_result_blocks_every_game_step(tmp_path):
    arena = Arena()
    class StopRPC(RPC):
        def call(self, *args, **kwargs):
            result = super().call(*args,**kwargs)
            (tmp_path/'STOP').touch()
            return result
    with pytest.raises(Stopped):
        run_episode(arena,{'job_id':'fixture','mode':'state'},{'level':2,'seed':17},StopRPC(),tmp_path)
    assert arena.actions == []


def test_threat_pending_during_planner_cooldown_survives_until_replan(tmp_path):
    class ThreatArena(Arena):
        def info(self):
            info = super().info()
            if self.tick >= 25:
                info['raw_observation']['plants'] = []
            return info
        def step(self, placement):
            _, truncated, info = super().step(placement)
            return self.tick >= 700, truncated, info
    class CooldownRPC(RPC):
        def call(self, op, **payload):
            result = super().call(op, **payload)
            result['plan_tick'] = 450 if payload['tick'] >= 450 else 0
            return result
    rpc = CooldownRPC()
    run_episode(ThreatArena(), {'job_id':'fixture','mode':'state'},
                {'level':2,'seed':17}, rpc, tmp_path)
    calls = {p['tick']:p for p in rpc.calls}
    assert [50,250,450,650] == list(calls)[1:]
    for tick in (50,250,450):
        assert calls[tick]['pending_replan_events'] == ['plant_lost']
    assert calls[650]['pending_replan_events'] == []


def test_bridge_honors_pending_event_after_cooldown(settings, monkeypatch):
    async def run():
        bridge = Bridge(settings)
        try:
            job = bridge.jobs.enqueue('pending-event'); task = bridge.jobs.request(job)
            calls = []
            async def plan(*args, **kwargs):
                calls.append(kwargs)
                return PlanResult(plan=Plan(objective='new guidance'),latency=0)
            async def select(*args, **kwargs):
                return Selection(placement=None,reason='wait')
            monkeypatch.setattr(bridge.planner,'decide',plan)
            monkeypatch.setattr(bridge.selector,'select',select)
            request = dict(request_digest=digest(task),episode_id='cooldown',
                           plan=Plan(objective='old guidance',valid_for_seconds=60).model_dump(),
                           plan_tick=0,spent=0,pending_replan_events=['plant_lost'],**public())
            early = await bridge.decide(job,dict(request,tick=50))
            assert early['plan_tick']==0 and not calls
            ready = await bridge.decide(job,dict(request,tick=400))
            assert ready['plan_tick']==400 and len(calls)==1
        finally:
            await bridge.close()
    asyncio.run(run())


@pytest.mark.parametrize('seconds', [1.0, 0.0])
def test_candidate_can_tune_schedule_only_within_bounds(settings,monkeypatch,seconds):
    async def run():
        bridge = Bridge(settings)
        try:
            source = "def perceive(image):\n    return image\ndef decide(observation, memory):\n    return {'planner_context':{},'jev_context':{'runtime_schedule':{'seconds':"+str(seconds)+",'selection':'hierarchical'}},'request_replan':False}\n"
            version = bridge.jobs.code.add(source)
            book = bridge.jobs.store.load()
            updated = book.model_copy(update={'version':'v002','parent':book.version,'player_code':version})
            bridge.jobs.store.write(updated); bridge.jobs.store.set_current(updated.version)
            job = bridge.jobs.enqueue('candidate-schedule'); task = bridge.jobs.request(job)
            calls = []
            async def plan(*args, **kwargs):
                return PlanResult(plan=Plan(objective='fixture'),latency=0)
            async def select(*args, **kwargs):
                calls.append(kwargs['mode'])
                return Selection(placement=None,reason='wait')
            monkeypatch.setattr(bridge.planner,'decide',plan)
            monkeypatch.setattr(bridge.selector,'select',select)
            request = dict(request_digest=digest(task),episode_id='schedule',tick=0,**public())
            if seconds == 0:
                with pytest.raises(ValueError):
                    await bridge.decide(job,request)
                assert calls == []
            else:
                result = await bridge.decide(job,request)
                assert result['schedule']['seconds']==1 and calls==['hierarchical']
                assert result['placement'] is None
        finally:
            await bridge.close()
    asyncio.run(run())


def test_parallel_lanes_keep_instance_ids_order_and_resume(settings,tmp_path):
    jobs = NativeJobs(settings)
    task = jobs.request(jobs.enqueue('parallel-fixture'))
    barrier = threading.Barrier(2)
    opened, closed = [], []
    class ConcurrentArena(Arena):
        def __init__(self, config, mode):
            super().__init__()
            self.lane = config['instance_id']
            opened.append(self.lane)
            barrier.wait(timeout=10)
        def close(self):
            closed.append(self.lane)
    config = {'work_dir':str(tmp_path/'worker'),'workers':2}
    rpc = RPC()
    result = evaluate_task(config,task,rpc,arena_factory=ConcurrentArena)
    assert sorted(opened)==[0,1] and sorted(closed)==[0,1]
    assert [ep['seed'] for ep in result['episodes']]==[ep['seed'] for ep in task['episodes']]
    assert len({payload['episode_id'] for payload in rpc.calls})==8
    assert result['throughput']['new_episodes']==8
    assert evaluate_task(config,task,RPC(),arena_factory=ConcurrentArena)==result
    assert len(opened)==2
    with pytest.raises(RuntimeError,match='identity changed'):
        evaluate_task({**config,'workers':1},task,RPC(),arena_factory=ConcurrentArena)


def test_flat_jev_keeps_all_legal_choices_and_wait_in_one_request():
    seen = []
    def response(request):
        import httpx
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200,json={'answers':{key:{'noul':.9 if key=='slot1_row3_col7' else .01}
                                                  for key in body['questions']}})
    chosen = asyncio.run(selector(response).select(context(),JevPolicy(),mode='flat'))
    assert (chosen.placement.slot,chosen.placement.row,chosen.placement.col)==(1,3,7)
    assert len(seen)==1 and 'wait' in seen[0]['questions']
    assert 'slot1_row3_col8' in seen[0]['questions']


def test_persistent_worker_reuses_pid_and_reset_ends_old_process():
    async def run():
        player = PlayerRuntime(changed_source())
        try:
            assert (await player.step(observation()))['jev_context']['tracked_calls']==1
            process = player.worker.process
            assert (await player.step(observation()))['jev_context']['tracked_calls']==2
            assert player.worker.process.pid==process.pid
            player.reset()
            assert process.poll() is not None
            assert (await player.step(observation()))['jev_context']['tracked_calls']==1
        finally:
            player.close()
    asyncio.run(run())


def test_persistent_worker_timeout_kills_candidate():
    source = '''def perceive(image):
    return image
def decide(observation, memory):
    for a in range(100000):
        for b in range(100000):
            memory['x'] = a + b
    return {'planner_context':{},'jev_context':{},'request_replan':False}
'''
    worker = PersistentWorker(source)
    with pytest.raises(TimeoutError):
        worker.call({}, {}, timeout=.1)
    assert worker.process.poll() is not None


def test_bridge_keeps_episode_memories_separate_and_releases_only_owner(settings,monkeypatch):
    async def run():
        bridge = Bridge(settings)
        try:
            version = bridge.jobs.code.add(changed_source())
            book = bridge.jobs.store.load()
            updated = book.model_copy(update={'version':'v002','parent':book.version,'player_code':version})
            bridge.jobs.store.write(updated); bridge.jobs.store.set_current(updated.version)
            job = bridge.jobs.enqueue('separate-memories'); task = bridge.jobs.request(job)
            async def plan(*args,**kwargs):
                return PlanResult(plan=Plan(objective='fixture'),latency=0)
            async def select(*args,**kwargs):
                return Selection(placement=None,reason='wait')
            monkeypatch.setattr(bridge.planner,'decide',plan)
            monkeypatch.setattr(bridge.selector,'select',select)
            def request(episode):
                return dict(request_digest=digest(task),episode_id=episode,tick=0,**public())
            left,right = await asyncio.gather(bridge.decide(job,request('left')),bridge.decide(job,request('right')))
            assert left['system0']['jev_context']['tracked_calls']==right['system0']['jev_context']['tracked_calls']==1
            assert (await bridge.decide(job,request('left')))['system0']['jev_context']['tracked_calls']==2
            left_process = bridge.players[(digest(task),'left')].worker.process
            await bridge.handle(dict(op='release_episode',job_id=job['id'],request_digest=digest(task),episode_id='left'))
            assert left_process.poll() is not None
            assert (await bridge.decide(job,request('right')))['system0']['jev_context']['tracked_calls']==2
        finally:
            await bridge.close()
    asyncio.run(run())


def test_rpc_matches_out_of_order_responses_from_real_child_process():
    code = "import json,sys; a=json.loads(input()); b=json.loads(input()); print(json.dumps(dict(rpc_id=b['rpc_id'],ok=True,result=b['value'])),flush=True); print(json.dumps(dict(rpc_id=a['rpc_id'],ok=True,result=a['value'])),flush=True)"
    process = subprocess.Popen([sys.executable,'-u','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    rpc = JsonRPC(process)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(rpc.call,'decide',value='first')
            b = pool.submit(rpc.call,'decide',value='second')
            assert a.result(timeout=10)=='first'
            assert b.result(timeout=10)=='second'
    finally:
        process.wait(timeout=10)
        process.stdin.close(); process.stdout.close()


def test_server_overlaps_decisions_but_waits_before_queue_mutation(monkeypatch,capsys):
    active, peak, order = 0, 0, []
    together = asyncio.Event()
    class FakeBridge:
        async def handle(self, request):
            nonlocal active, peak
            if request['op']=='decide':
                active+=1; peak=max(peak,active)
                if active == 2:
                    together.set()
                await asyncio.wait_for(together.wait(),timeout=5)
                active-=1
            else:
                assert active==0
            order.append(request['rpc_id'])
            return request['rpc_id']
    requests = [dict(op='decide',rpc_id='a'),dict(op='decide',rpc_id='b'),dict(op='report',rpc_id='c')]
    monkeypatch.setattr(sys,'stdin',io.StringIO(''.join(json.dumps(x)+'\n' for x in requests)))
    asyncio.run(serve(FakeBridge()))
    assert peak==2 and order[-1]=='c'
    assert {json.loads(line)['rpc_id'] for line in capsys.readouterr().out.splitlines()}=={'a','b','c'}
