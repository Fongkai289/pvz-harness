import asyncio
from dataclasses import replace
import json
import time

import pytest
from pydantic import BaseModel

from rsi.codex_queue import CoachQueue, request_coach
from rsi.coach_usage import usage_delta, summarize_calls
from rsi.code_evaluate import Interrupted
from tests.test_server import settings


class Reply(BaseModel):
    ok: bool


def test_cumulative_counters_do_not_double_count_cache_or_reasoning():
    first=dict(inputTokens=100,cachedInputTokens=40,outputTokens=30,reasoningOutputTokens=20,totalTokens=130)
    second=dict(inputTokens=300,cachedInputTokens=140,outputTokens=80,reasoningOutputTokens=50,totalTokens=380)
    calls=[{'usage':usage_delta(first)}, {'usage':usage_delta(second,first)}]
    result=summarize_calls(calls)
    assert result['total_tokens']==380 and result['input_tokens']==300
    assert result['cached_input_tokens']==140 and result['reasoning_output_tokens']==50
    missing=summarize_calls([*calls,{}])
    assert missing['total_tokens'] is None and missing['known_total_tokens']==380
    assert not missing['complete']
    assert usage_delta(first,second)['total_tokens'] is None


def test_queue_roundtrip_reconnect_idempotence_and_tokens(settings,tmp_path):
    s=replace(settings,coach_provider='codex',coach_queue_dir=tmp_path/'queue',coach_timeout=5)
    controls=tmp_path/'round'; controls.mkdir()
    q=CoachQueue(s.coach_queue_dir)
    async def run():
        params=dict(role='coach',messages=[{'role':'user','content':'test evidence'}],schema=Reply,
                    settings=s,controls=controls,audit_dir=controls/'calls')
        task=asyncio.create_task(request_coach(**params))
        await asyncio.sleep(.01)
        item=q.dispatch({'op':'next','owner':'worker'})
        assert q.dispatch({'op':'next','owner':'other'}) is None
        with pytest.raises(ValueError,match='owner'):
            q.dispatch({'op':'complete','id':item['id'],'owner':'other'})
        response={'op':'complete','id':item['id'],'owner':'worker','result':{'ok':True},
                  'metadata':{'usage':usage_delta(dict(inputTokens=4,cachedInputTokens=0,
                       outputTokens=3,reasoningOutputTokens=0,totalTokens=7))}}
        q.dispatch(response); q.dispatch(response)
        result,metadata=await task
        assert result.ok and metadata['usage']['total_tokens']==7
        result2,_=await request_coach(**params)
        assert result2==result
        stats=q.dispatch({'op':'stats'})
        assert len(stats)==1 and stats[0]['tokens']['calls']==1
        assert stats[0]['tokens']['total_tokens']==7
        assert (controls/'coach_tokens.json').exists()
    asyncio.run(run())


@pytest.mark.parametrize('reason',['PAUSE','STOP','timeout','failure'])
def test_cancel_and_failure_never_return_candidate(settings,tmp_path,reason):
    s=replace(settings,coach_provider='codex',coach_queue_dir=tmp_path/'queue',coach_timeout=5)
    controls=tmp_path/'round';controls.mkdir();q=CoachQueue(s.coach_queue_dir)
    async def run():
        task=asyncio.create_task(request_coach('coach',[],Reply,s,controls=controls,audit_dir=controls/'calls'))
        await asyncio.sleep(.01)
        item=q.dispatch({'op':'next','owner':'worker'})
        if reason in ('PAUSE','STOP'): (controls/reason).touch()
        elif reason=='timeout':
            expired=q.read(item['id']); expired['deadline']=time.time()-1; q.save(expired)
        else:q.dispatch({'op':'complete','id':item['id'],'owner':'worker','error':'Astra unavailable'})
        with pytest.raises((Interrupted,RuntimeError)): await task
        q.dispatch({'op':'complete','id':item['id'],'owner':'worker','result':{'ok':True}})
        assert q.read(item['id'])['status'] in ('cancelled','failed')
    asyncio.run(run())


def test_usage_endpoint_is_authenticated(settings,tmp_path):
    from fastapi.testclient import TestClient
    from server.app import create_app
    with TestClient(create_app(replace(settings,coach_queue_dir=tmp_path/'queue'))) as client:
        assert client.get('/coach/usage').status_code==401
        assert client.get('/coach/usage',headers={'Authorization':'Bearer secret'}).json()['rounds']==[]


def test_operator_usage_shows_unknown_and_scope():
    from client.operator_view import OperatorView
    view=OperatorView('ws://localhost/ws','unused')
    view.coach_usage={'rounds':[{'round_id':'test','tokens':summarize_calls([{}])}]}
    text=view.coach_usage_text()
    assert '未知' in text and '不含' in text


def test_codex_desktop_install_without_path(monkeypatch, tmp_path):
    from rsi.codex_coach import resolve_codex
    monkeypatch.setenv('PATH', '')
    monkeypatch.delenv('PVZ_CODEX_EXECUTABLE', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    binary = tmp_path/'OpenAI'/'Codex'/'bin'/'version-hash'/'codex.exe'
    binary.parent.mkdir(parents=True); binary.touch()
    assert resolve_codex() == str(binary.resolve())
    # An explicit configuration error must not silently select another install.
    monkeypatch.setenv('PVZ_CODEX_EXECUTABLE', str(tmp_path/'missing.exe'))
    with pytest.raises(FileNotFoundError, match='Configured Codex'):
        resolve_codex()


def test_startup_failure_completes_claimed_request(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from rsi import codex_coach, native_loop
    q = CoachQueue(tmp_path/'queue')
    item = {'id':'a'*64, 'round_id':'startup-round', 'status':'queued', 'controls':str(tmp_path/'round'),
            'created':time.time(), 'deadline':time.time()+240}
    q.save(item)
    class Bridge:
        def __init__(self, *args): self.rpc = SimpleNamespace()
        def call(self, op, **payload): return q.dispatch({'op':op, **payload})
        def close(self): pass
    def broken_server(*args): raise FileNotFoundError('missing codex.exe')
    monkeypatch.setattr(native_loop, 'SSHBridge', Bridge)
    monkeypatch.setattr(codex_coach, 'AppServer', broken_server)
    monkeypatch.setattr(codex_coach, 'render_status', lambda *args: None)
    codex_coach.lane({}, tmp_path, 'worker', once=True)
    result = q.read(item['id'])
    assert result['status'] == 'failed'
    assert 'FileNotFoundError: missing codex.exe' == result['error']
    assert result['metadata']['stage'] == 'app_server_startup'
    assert result['finished'] < result['deadline']
    assert not list(tmp_path.glob('pending-*.json'))


def test_failed_process_start_closes_log(monkeypatch, tmp_path):
    from rsi import codex_coach
    logs = []
    def broken_popen(*args, **kwargs):
        logs.append(kwargs['stderr'])
        raise OSError('startup failed')
    monkeypatch.setattr(codex_coach, 'resolve_codex', lambda *args: 'codex.exe')
    monkeypatch.setattr(codex_coach.subprocess, 'Popen', broken_popen)
    with pytest.raises(OSError, match='startup failed'):
        codex_coach.AppServer(tmp_path)
    assert len(logs) == 1 and logs[0].closed


def test_thread_writer_released_before_task_acknowledgement(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from rsi import codex_coach, native_loop
    events = []
    class Server:
        def __init__(self, root): events.append('start')
        def run(self, *args):
            events.append('run')
            return {'result':{'ok':True}, 'error':None, 'metadata':{}}
        def close(self): events.append('close')
    class Bridge:
        def __init__(self, *args): self.rpc = SimpleNamespace()
        def call(self, op, **payload):
            if op == 'next': return {'id':'b'*64, 'round_id':'round'}
            if op == 'complete': events.append('ack')
            return []
        def close(self): pass
    monkeypatch.setattr(native_loop, 'SSHBridge', Bridge)
    monkeypatch.setattr(codex_coach, 'AppServer', Server)
    monkeypatch.setattr(codex_coach, 'render_status', lambda *args: None)
    codex_coach.lane({}, tmp_path, 'lane-a', once=True)
    codex_coach.lane({}, tmp_path, 'lane-b', once=True)
    assert events == ['start', 'run', 'close', 'ack'] * 2
