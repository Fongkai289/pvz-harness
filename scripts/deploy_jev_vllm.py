"""Scoped Jev client adapter update; shared model services remain untouched."""
import deploy_codex_coach as deploy

deploy.FILES=['server/jev_vllm.py','server/action_selector.py','server/config.py',
              'tests/test_jev_vllm.py','.env.example']
deploy.REMOTE=deploy.REMOTE.replace("'before-codex-coach-'", "'before-jev-vllm-'")
deploy.REMOTE=deploy.REMOTE.replace("'-m','pytest','-q','--disable-warnings'",
    "'-m','pytest','tests/test_jev_vllm.py','tests/test_action_selector.py','tests/test_system0_actions.py','-q','--disable-warnings'")
deploy.REMOTE=deploy.REMOTE.replace("env=root/'.env';", """
import asyncio
from server.action_selector import OneJevSelector
async def probe():
    selector=OneJevSelector('http://127.0.0.1:18222/v1/completions','AutoTrust-JEV-27B-v1.1',5)
    try:
        started=time.monotonic()
        choice,scores=await selector.rank('Sun=100. Threat row 2. Peashooter ready. Planner: defend row 2.',
                                           {'wait':'Wait','plant':'Plant peashooter row 2 column 2'})
        print(json.dumps({'jev_choice':choice,'probabilities':scores,'latency':time.monotonic()-started}),flush=True)
    finally:await selector.client.aclose()
asyncio.run(probe())
env=root/'.env';""")
deploy.REMOTE=deploy.REMOTE.replace("'PVZ_COACH_TIMEOUT':'240'", "'PVZ_COACH_TIMEOUT':'240',\n'PVZ_ONEJEV_URL':'http://127.0.0.1:18222/v1/completions','PVZ_ONEJEV_MODEL':'AutoTrust-JEV-27B-v1.1','PVZ_DECIDER_TIMEOUT':'5'")
deploy.REMOTE=deploy.REMOTE.replace("assert h['playbook']==initial['playbook']", "assert h['playbook']==initial['playbook'] and h['jev_model']=='AutoTrust-JEV-27B-v1.1'")
deploy.REMOTE=deploy.REMOTE.replace('codex-coach-deploy.json','jev-vllm-deploy.json')

if __name__=='__main__':deploy.main()
