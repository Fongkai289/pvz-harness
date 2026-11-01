"""Sequential campaign then one full automatic Coach/Engineer/gate round."""
import argparse
import json
from pathlib import Path
import time

from common.file_lock import file_lock
from rsi.code_store import write_json
from rsi.native_loop import SSHBridge, evaluate_task, control, Stopped
from rsi.native_jobs import TERMINAL, digest
from rsi.portable_runtime import preflight


def campaign(config,bridge,root,name):
    records=[]
    for level in range(1,11):
        control(root)
        job=bridge.call('portable_create',name=f'{name}-level-{level}',level=level,seed=71000+level)
        task=bridge.call('request',job_id=job['id'])
        report=evaluate_task(config,task,bridge)
        bridge.call('portable_campaign_report',job_id=job['id'],report=report)
        ep=report['episodes'][0]
        records.append(dict(level=level,outcome=ep['outcome'],score=ep['score'],
                            wall_s=ep['wall_s'],throughput=ep['throughput']))
        write_json(root/(name+'.json'),records)
        print(json.dumps(dict(campaign=name,level=level,outcome=ep['outcome'],score=ep['score'])),flush=True)
        if ep['outcome']!='win': break
    return records


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('data/portable_rsi/config.json'))
    parser.add_argument('--name',required=True)
    parser.add_argument('--mode',choices=['campaign','rsi','both'],default='both')
    args=parser.parse_args()
    config=json.loads(args.config.read_text(encoding='utf-8-sig'))
    root=Path(config['work_dir']); root.mkdir(parents=True,exist_ok=True)
    preflight(config)
    with file_lock(root/'WORKER.lock',blocking=False):
        bridge=SSHBridge(config,root)
        summary=dict(name=args.name,started=time.time(),status='running')
        try:
            summary['before']=bridge.call('portable_info')
            if args.mode in ('campaign','both'):
                summary['campaign_before']=campaign(config,bridge,root,args.name+'-before')
            if args.mode in ('rsi','both'):
                job=bridge.call('portable_create',name=args.name+'-rsi')
                while job['status'] not in TERMINAL:
                    control(root)
                    job=bridge.call('prepare',job_id=job['id'])
                    write_json(root/'status.json',job)
                    print(json.dumps({k:job.get(k) for k in ('id','status','step','detail','reason')}),flush=True)
                    if job['status'] in TERMINAL: break
                    if job['step']==2 and not job.get('candidate') and not (job.get('engineering') or {}).get('pending'): continue
                    task=bridge.call('request',job_id=job['id'])
                    try: report=evaluate_task(config,task,bridge)
                    except Exception as exc:
                        if task.get('engineering_test') and not isinstance(exc,Stopped):
                            job=bridge.call('test_error',job_id=job['id'],request_digest=digest(task),error=str(exc))
                            continue
                        raise
                    job=bridge.call('report',job_id=job['id'],report=report)
                summary['rsi']={k:v for k,v in job.items() if k not in ('suites','engineering')}
                if job['status']=='promoted':
                    summary['campaign_after']=campaign(config,bridge,root,args.name+'-after')
            summary.update(after=bridge.call('portable_info'),status='complete')
        except BaseException as exc:
            summary.update(status='blocked',error=f'{type(exc).__name__}: {exc}')
            raise
        finally:
            bridge.close()
            write_json(root/(args.name+'-summary.json'),summary)


if __name__=='__main__': main()
