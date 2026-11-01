"""Portable research uses the production roles and fixed RSI gates, isolated CURRENT."""
import asyncio
from dataclasses import replace
import json
import logging
from pathlib import Path
import sys
import time

from common.file_lock import file_lock
from rsi.code_store import write_json
from rsi.native_bridge import Bridge, serve
from rsi.native_engineer import NativeEngineer
from rsi.native_jobs import NativeJobs, digest, TERMINAL
from rsi.native_gate import episode_score
from rsi.native_player import NativeCodeStore
from server.config import load_settings
from server.playbook import PlaybookStore

UPSTREAM='98b659380014c89c3553fbb2839f5e5a84d6df3d'


class PortableJobs(NativeJobs):
    backend='pvz_portable'
    upstream=UPSTREAM
    identity_fields=('game_sha256','adapter_sha256','assets_sha256','worker_source')


def isolated_settings(settings):
    root=settings.data_dir/'portable_study'
    root.mkdir(parents=True,exist_ok=True)
    with file_lock(root/'INIT.lock'):
        if not (root/'origin.json').exists():
            live=PlaybookStore(settings.data_dir)
            original=live.load()
            store=PlaybookStore(root)
            path=store.dir/(original.version+'.json')
            if not path.exists(): store.write(original)
            elif store.load(original.version)!=original: raise ValueError('Portable seed book collision')
            if original.player_code:
                source=NativeCodeStore(settings.data_dir).source(original.player_code)
                if NativeCodeStore(root).add(source)!=original.player_code: raise ValueError('code seed mismatch')
            store.set_current(original.version)
            write_json(root/'origin.json',dict(version=original.version,digest=digest(original.model_dump()),
                time=time.time(),scope='Portable research only; live CURRENT unchanged'))
    return replace(settings,data_dir=root)


class PortableBridge(Bridge):
    def __init__(self,settings):
        super().__init__(isolated_settings(settings))
        self.jobs=PortableJobs(self.settings)

    async def handle(self,request):
        op=request['op']
        if op=='portable_info':
            book=self.jobs.store.load()
            return dict(backend='pvz_portable',version=book.version,player_code=book.player_code,
                        scope='research CURRENT',engine_digest=self.jobs.engine_digest())
        if op=='portable_create':
            name=request['name']
            job=self.jobs.enqueue(name)
            if request.get('level') is not None:
                level=request['level']; seed=request['seed']
                if type(level) is not int or not 1<=level<=10 or type(seed) is not int or not 0<=seed<2**30:
                    raise ValueError('invalid campaign spec')
                if job.get('kind') not in (None,'campaign'): raise ValueError('job kind collision')
                if job.get('kind')=='campaign' and job['suites']['development']!=[dict(level=level,seed=seed)]:
                    raise ValueError('campaign spec changed')
                job.update(kind='campaign',suites=dict(development=[dict(level=level,seed=seed)]),
                           status='evaluating')
            else:
                job['kind']='rsi'
            self.jobs.save(job)
            return job
        if op=='portable_campaign_report':
            job=self.jobs.load(request['job_id']); task=self.jobs.request(job); report=request['report']
            if job.get('kind')!='campaign' or report['request_digest']!=digest(task): raise ValueError('wrong campaign')
            env=report['environment']; eps=report['episodes']
            if (env['backend']!='pvz_portable' or env['upstream']!=UPSTREAM or env.get('fixture')
                    or env['engine_digest']!=job['engine_digest'] or len(eps)!=1):
                raise ValueError('invalid campaign evidence')
            ep=eps[0]
            if {k:ep[k] for k in ('seed','level')}!=task['episodes'][0]: raise ValueError('campaign spec mismatch')
            if not all(ep.get(k) for k in ('complete','seed_applied','mower_coverage')) or ep.get('model_errors') or ep.get('invalid_actions'):
                raise ValueError('incomplete campaign')
            score=episode_score(ep['outcome'],ep['progress'],ep['elapsed_game_s'],ep['mower_times'])
            if abs(score-ep['score'])>1e-9: raise ValueError('campaign score mismatch')
            write_json(self.jobs.folder(job['id'])/'campaign.json',report)
            job.update(status='campaign_complete',outcome=ep['outcome'],score=score)
            self.jobs.save(job)
            return job
        if op=='prepare':
            job=self.jobs.load(request['job_id'])
            if job['status'] in TERMINAL: return job
            self.jobs.verify_versions(job)
            if job['step']==2 and job['candidate'] is None:
                await NativeEngineer(self.jobs).advance(job,{
                    'source':'Portable development games; no human-supplied tactical diagnosis',
                    'backend':'pvz_portable','live_review':None})
            return job
        try:
            return await super().handle(request)
        except Exception as exc:
            if op=='decide':
                job=self.jobs.load(request['job_id'])
                job.update(status='blocked',detail=f'{type(exc).__name__}: {exc}'[:500])
                self.jobs.save(job)
            raise


async def main():
    settings=load_settings()
    if settings.planner!='qwen' or settings.decider!='onejev': raise ValueError('production roles required')
    bridge=PortableBridge(settings)
    try:
        with file_lock(bridge.jobs.root/'WORKER.lock',blocking=False): await serve(bridge)
    finally: await bridge.close()


if __name__=='__main__':
    logging.basicConfig(level=logging.WARNING,stream=sys.stderr)
    asyncio.run(main())
