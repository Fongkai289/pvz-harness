import asyncio
import json
import math
import httpx
import pytest

from server.jev_vllm import rank,MODEL


@pytest.mark.parametrize('bad',[None,'missing','nan','bool','model','tokens'])
def test_code_readout_protocol_and_fail_closed(bad):
    sent=[]
    def handle(request):
        body=json.loads(request.content);sent.append(body)
        if request.url.path=='/tokenize':
            assert body['chat_template_kwargs']=={'enable_thinking':False}
            assert 'A: wait: Wait' in body['messages'][1]['content']
            return httpx.Response(200,json={'tokens':[True] if bad=='tokens' else [10,20]})
        assert body['prompt']==[10,20] and body['max_tokens']==1
        assert body['allowed_token_ids']==[32,33] and body['logprobs']==2
        probs={'token_id:32':math.log(.2),'token_id:33':math.log(.8)}
        if bad=='missing':probs.pop('token_id:33')
        if bad=='nan':probs['token_id:33']='NaN'
        if bad=='bool':probs['token_id:33']=True
        return httpx.Response(200,json={'model':'wrong' if bad=='model' else MODEL,
            'choices':[{'logprobs':{'top_logprobs':[probs]}}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await rank(client,'http://jev/v1/completions',MODEL,'state',
                              {'wait':'Wait','plant':'Plant'}, {'codes':['A','B'],'readout_token_ids':[32,33]})
    if bad:
        with pytest.raises((ValueError,KeyError)):asyncio.run(run())
    else:
        winner,scores=asyncio.run(run()); assert winner=='plant' and scores['plant']==pytest.approx(.8)
        assert sum(scores.values())==pytest.approx(1)
