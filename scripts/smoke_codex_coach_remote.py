"""Run on Linux with the Windows worker active; no games, candidates or promotions.

Exercises the production role transport and two harness tools in one Astra thread.
"""
import asyncio
import json
import time
from pathlib import Path

from server.config import load_settings
from server.playbook import PlaybookStore
from rsi.code_model import request_role
from rsi.native_engineer import NativeCoachAction, NativeCoachWorkspace, SYSTEM
from rsi.native_player import SEED, CONTRACT
from rsi.coach_usage import summarize_calls
from rsi.code_store import write_json


async def main():
    settings=load_settings()
    assert settings.coach_provider=='codex'
    root=settings.data_dir/'codex_integration_checks'/time.strftime('%Y%m%d-%H%M%S')
    root.mkdir(parents=True)
    book=PlaybookStore(settings.data_dir).load()
    ws=NativeCoachWorkspace(SEED,root,{'docs/contract.txt':CONTRACT},root,book=book)
    messages=[{'role':'system','content':SYSTEM}, {'role':'user','content':
        'Integration check only, not a learning round. There is no game evidence. '
        'First read docs/contract.txt with read_file. Then after feedback call check. '
        'Do not modify files, request games, or finish. Do not claim any gameplay improvement.'}]
    calls=[]; actions=[]
    for expected in ('read_file','check'):
        action,metadata=await request_role('coach',messages,NativeCoachAction,settings,
                                          controls=root,audit_dir=root/'calls')
        assert action.action==expected,action.action
        result=ws.dispatch(action)
        actions.append(action.action);calls.append(metadata)
        messages.extend([{'role':'assistant','content':action.model_dump_json()},
                         {'role':'user','content':json.dumps({'tool_result':result})}])
    assert calls[0]['thread_id']==calls[1]['thread_id']
    assert calls[0]['turn_id']!=calls[1]['turn_id']
    totals=summarize_calls(calls)
    assert totals['total_tokens']==calls[-1]['raw_token_usage']['total']['totalTokens']
    report={'integration_check_only':True,'model':settings.coach_model,'actions':actions,
            'thread_id':calls[0]['thread_id'],'calls':calls,'tokens':totals,
            'playbook_unchanged':PlaybookStore(settings.data_dir).current()==book.version}
    assert report['playbook_unchanged']
    write_json(root/'result.json',report)
    print(json.dumps({'artifact':str(root/'result.json'),**report}),flush=True)


if __name__=='__main__':asyncio.run(main())
