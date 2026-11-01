import json
from copy import deepcopy
from dataclasses import replace
import pytest

from tests.test_server import settings
from tests.test_native_rsi import report
from rsi.portable_bridge import PortableJobs, isolated_settings
from rsi.portable_runtime import player_state, NAMES
from server.scene import Scene, Card
from server.planner import Plan
from server.action_selector import DecisionContext, candidates
from server.playbook import PlaybookStore


def test_conveyor_card_cost_and_empty_slots_do_not_use_normal_sun_cost():
    scene=Scene(frame_id=0,t_capture=0,lawn='day',rows=5,sun=0,zombies=[],
        cards=[Card(slot=0,plant='explode-o-nut',ready=True,brightness=0,cost=0),
               Card(slot=1,plant='empty',ready=False,brightness=0,cost=0)])
    ctx=DecisionContext(scene,{},set(),Plan(objective='test'),allowed_positions={(0,2,3)})
    offered=candidates(ctx)
    assert [(p.slot,p.row,p.col) for p in offered]==[(0,2,3)]
    scene.cards[0].cost=150
    assert candidates(ctx)==[]


def test_portable_observation_filters_future_and_judge_fields():
    raw=dict(global_state=dict(sun=50,future_wave=[123]),cards=[dict(slot=0,plant_type=1,ready=True,cost=50)],
        plants=[],zombies=[dict(type=0,row=4,x=400,hp=999)],rng_seed=776,score=1)
    mask=[False]*757; mask[1+4*9]=True
    value=player_state(raw,mask,100)
    assert value['allowed_positions']==[[0,4,0]]
    assert value['scene']['zombies'][0]['row']==4
    assert all(key not in json.dumps(value) for key in ('future_wave','rng_seed','hp','score'))
    assert NAMES[49]=='explode-o-nut' and NAMES[50]=='giant wall-nut'


def test_portable_gates_cannot_accept_pvzrl_or_changed_binary(settings):
    jobs=PortableJobs(settings); job=jobs.enqueue('portable-test')
    data=report(jobs,job)
    with pytest.raises(ValueError,match='backend'): jobs.accept(job,data)
    data['environment'].update(backend='pvz_portable',adapter_sha256='d'*64,assets_sha256='e'*64)
    data['environment'].pop('dll_sha256')
    job=jobs.accept(job,data)
    second=report(jobs,job)
    second['environment'].update(backend='pvz_portable',adapter_sha256='d'*64,assets_sha256='e'*64,game_sha256='f'*64)
    second['environment'].pop('dll_sha256')
    with pytest.raises(ValueError,match='runtime changed'): jobs.accept(job,second)


def test_research_current_is_independent_of_live(settings):
    live=PlaybookStore(settings.data_dir); original=live.current()
    research=isolated_settings(settings)
    assert research.data_dir!=settings.data_dir
    store=PlaybookStore(research.data_dir)
    book=store.load().model_copy(update={'version':store.next_version(),'parent':original})
    store.write(book); store.set_current(book.version)
    assert live.current()==original
    assert PlaybookStore(isolated_settings(settings).data_dir).current()==book.version
