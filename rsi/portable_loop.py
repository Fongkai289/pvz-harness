"""Sequential campaign and automatic Coach/Engineer/gate rounds.

campaign: levels in order from 1-1, stopping at the first loss.
rsi: one full round. both: campaign, one round, and the campaign again if promoted.
train: levels 1-2..1-10 (1-1 is skipped) in order, each with its own playbook (see train()); a level
is done once won mower-free; up to MAX_LEVEL_ATTEMPTS Coach attempts per level, then the run stops. A dropped
SSH connection is reopened (RECONNECT_DELAYS): the interrupted level is replayed from its start
and a Coach round resumes from its saved job; the half-played game is never counted.
Progress for the control center goes to work_dir/training.json.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import secrets
import threading
from pathlib import Path
import time

from common.file_lock import file_lock
from rsi.code_store import write_json
from rsi.native_loop import SSHBridge, evaluate_task, control, Stopped
from rsi.native_jobs import TERMINAL, digest
from rsi.portable_runtime import preflight

LEVELS = range(1, 11)
# 1-1 is the tutorial: no sun falls and no zombie comes until a first plant is placed, so a
# player that waits never starts the level and the game runs into the tick cap.
TRAIN_LEVELS = range(2, 11)
SKIPPED = [1]
MAX_UNPROMOTED = 3  # gated rounds (old modes)
MAX_LEVEL_ATTEMPTS = 10  # Coach rounds on one level before training stops
PARALLEL_FIXES = ('a', 'b')  # variants per round, each from its own suggested starting point (native_jobs.ANGLES)
RECONNECT_DELAYS = (10, 30, 60)  # seconds before each reconnect; after the last one the run stops


class Link:
    """The SSH research bridge, reopened after the connection drops."""
    def __init__(self, config, root):
        self.config, self.root = config, root
        self.bridge = SSHBridge(config, root)
        self.lock = threading.Lock()  # parallel attempts share the link; only one of them reopens it

    def call(self, op, **payload):
        return self.bridge.call(op, **payload)

    def release_episode(self, task, episode_id):
        return self.bridge.release_episode(task, episode_id)

    def reopen(self, failed=None):
        with self.lock:
            if failed is not None and self.bridge is not failed:
                return  # another thread already reopened it
            try:
                self.bridge.close()
            except Exception:
                pass  # the old ssh process is already gone
            try:
                self.bridge = SSHBridge(self.config, self.root)
            except Exception:
                pass  # network still down: the closed link fails the next call, which counts as another drop

    def close(self):
        self.bridge.close()


def connection_lost(exc):
    """RPC failures from a dead SSH link, not errors the server reported."""
    while exc is not None:
        if str(exc) in ('RPC connection failed', 'research process disconnected'):
            return True
        exc = exc.__cause__
    return False


def with_reconnect(link, progress, action):
    for delay in (*RECONNECT_DELAYS, None):
        used = getattr(link, 'bridge', None)
        try:
            return action()
        except RuntimeError as exc:
            if delay is None or not connection_lost(exc):
                raise
            progress.update(notice=f'服务器连接断开，{delay} 秒后重连；打到一半的这一局作废重打')
            for _ in range(delay):
                control(link.root)  # Stop still works while waiting
                time.sleep(1)
            link.reopen(used)
            progress.update(notice='')


def clean_win(record):
    """Won without any lawn mower; old records without a mower count fall back to a full score."""
    if record.get('outcome') != 'win':
        return False
    if 'mowers' in record:
        return record['mowers'] == 0
    return isinstance(record.get('score'), (int, float)) and record['score'] >= 1 - 1e-9


def campaign_records(root):
    for path in Path(root).glob('*.json'):
        try:
            records = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(records, list):
            yield from (r for r in records if isinstance(r, dict) and r.get('level') in LEVELS)


def won_levels(root, clean=False):
    """Levels won in any recorded campaign under work_dir (any strategy version); clean: mower-free."""
    return {r['level'] for r in campaign_records(root)
            if r.get('outcome') == 'win' and (not clean or clean_win(r))}


def mower_wins(root):
    """Levels won only with a lawn mower so far."""
    return won_levels(root) - won_levels(root, clean=True)


def first_unbeaten(root, levels=LEVELS, clean=False):
    won = won_levels(root, clean)
    return next((level for level in levels if level not in won), None)


class Progress:
    """training.json: what the control center shows while a run is going."""
    def __init__(self, root, name, mode):
        self.path = Path(root) / 'training.json'
        self.state = dict(name=name, mode=mode, state='running', started=time.time(), round=0,
                          phase='starting', level=None, job=None, unpromoted=0, rounds=[], message='',
                          levels=list(TRAIN_LEVELS), skipped=SKIPPED)
        self.root = root
        self.lock = threading.RLock()  # parallel attempts report at the same time
        self.write()

    def update(self, **fields):
        with self.lock:
            self.state.update(fields)
            self.write()

    def variant(self, letter, **fields):
        with self.lock:
            variants = dict(self.state.get('variants') or {})
            variants[letter] = {**variants.get(letter, {}), **fields}
            self.update(variants=variants)

    def write(self):
        self.state.update(updated=time.time(), won=sorted(won_levels(self.root, clean=True)),
                          mower_wins=sorted(mower_wins(self.root)))
        write_json(self.path, self.state)


class LevelLog:
    """work_dir/levels/<level>.json: every game and Coach attempt on one level, across training runs."""
    def __init__(self, root, level):
        self.path = Path(root) / 'levels' / f'{level}.json'
        self.level = level

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'level': self.level, 'games': [], 'attempts': []}

    def add(self, kind, entry):
        data = self.load()
        data[kind].append({'time': time.time(), **entry})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, data)


def test_games(root, job):
    """The Coach's test replays of one attempt, as saved by evaluate_task."""
    games = []
    for test in range(1, (job.get('tests') or 0) + 1):
        stage = f"step-2-engineer-{test}"
        try:
            ep = json.loads((Path(root) / job['id'] / stage / 'episode-0.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        games.append(dict(kind='coach_test', test=test, job=job['id'], stage=stage, file='episode-0',
                          outcome=ep.get('outcome'), score=ep.get('score'), mowers=len(ep.get('mower_times') or []),
                          wall_s=ep.get('wall_s'), elapsed_game_s=ep.get('elapsed_game_s')))
    return games


def play_level(config,bridge,root,name,level,progress=None,level_book=False,seed=None):
    """One campaign game of `level` (its fixed training seed unless given); with level_book, using that level's own playbook."""
    control(root)
    job=bridge.call('portable_create',name=f'{name}-level-{level}',level=level,seed=71000+level if seed is None else seed,
                    **({'level_book':True} if level_book else {}))
    if progress: progress.update(phase='campaign',level=level,job=job['id'])
    task=bridge.call('request',job_id=job['id'])
    report=evaluate_task(config,task,bridge)
    bridge.call('portable_campaign_report',job_id=job['id'],report=report)
    ep=report['episodes'][0]
    record=dict(level=level,outcome=ep['outcome'],score=ep['score'],mowers=len(ep.get('mower_times') or []),
                wall_s=ep['wall_s'],throughput=ep['throughput'],job=job['id'])
    print(json.dumps(dict(campaign=name,level=level,outcome=ep['outcome'],score=ep['score'])),flush=True)
    if progress: progress.update(last={k:record[k] for k in ('level','outcome','score','mowers')})
    return record


def campaign(config,bridge,root,name,start=1,progress=None,clean=False):
    """Levels in order from start; stops at a loss, or with clean also at any mower trigger."""
    records=[]
    for level in range(start,11):
        records.append(play_level(config,bridge,root,name,level,progress))
        write_json(root/(name+'.json'),records)
        if records[-1]['outcome']!='win' or clean and records[-1]['mowers']: break
    return records


def rsi_round(config,bridge,root,name,progress=None,target=None,create=None,on_status=None):
    """One Coach round until its job ends; create opens it (default: a gated round, targeted with target)."""
    job=create() if create else bridge.call('portable_create',name=name,**({'target':target} if target else {}))
    while job['status'] not in TERMINAL:
        control(root)
        job=bridge.call('prepare',job_id=job['id'])
        if on_status: on_status(job)
        else:
            write_json(root/'status.json',job)
            if progress: progress.update(phase='rsi',job=job['id'],rsi_status=job['status'])
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
    tests=(job.get('engineering') or {}).get('tests')
    return {k:v for k,v in job.items() if k not in ('suites','engineering')} | {'tests':tests}


def train(config,bridge,root,name,progress):
    """Per-level training: levels in order, each won mower-free before the next (see train_level)."""
    for level in TRAIN_LEVELS:
        if level in won_levels(root,clean=True):
            continue
        if not train_level(config,bridge,root,name,level,progress):
            progress.update(state='complete',phase='done',level=level,
                            message=f'1-{level} 试了 {MAX_LEVEL_ATTEMPTS} 次都没能不用小推车赢，训练停下')
            return
    progress.update(state='complete',phase='done',level=None,message='1-2 至 1-10 都已无小推车赢过')


def train_one_level(config,bridge,root,name,level,progress):
    """On demand (e.g. after a live loss): train one level on a fresh seed, won before or not."""
    seed=secrets.randbelow(2**30-1)+1
    progress.update(single=level,seed=seed)
    cleared=train_level(config,bridge,root,name,level,progress,seed=seed)
    progress.update(state='complete',phase='done',level=level,
                    message=(f'1-{level} 单独训练完成：新种子 {seed} 的这一局已不用小推车赢下' if cleared else
                             f'1-{level} 单独训练：试了 {MAX_LEVEL_ATTEMPTS} 次都没能不用小推车赢，已停下'))


def train_level(config,bridge,root,name,level,progress,seed=None):
    """Each level has its own playbook (a new level starts from the previous level's). Play the level's game;
    if it is lost or costs a mower, Coach rounds follow (PARALLEL_FIXES variants each, every one tested by
    replaying that same game), until it is won mower-free (True) or MAX_LEVEL_ATTEMPTS rounds run out (False).
    An unfinished round left by an earlier run on the same playbook is finished first instead of replaying."""
    book=with_reconnect(bridge,progress,lambda: bridge.call('portable_level_start',level=level))
    progress.update(round=0,phase='campaign',level=level,book=book.get('version'),target=None)
    log=LevelLog(root,level)
    version=book.get('version')
    resumed=None if seed is not None else with_reconnect(bridge,progress,lambda: bridge.call('portable_level_resume',level=level))
    if resumed:
        evidence,reason,mowers=resumed['evidence_job'],resumed['reason'],resumed['mowers']
        progress.update(notice='接着上一轮训练没做完的 Coach 尝试')
    else:
        tries=[]
        def play():
            tries.append(1)
            suffix='' if len(tries)==1 else f'-retry{len(tries)-1}'
            return play_level(config,bridge,root,f'{name}-L{level}{suffix}',level,progress,level_book=True,seed=seed)
        record=with_reconnect(bridge,progress,play)
        write_json(root/f'{name}-L{level}.json',[record])
        log.add('games',dict(kind='level_game',job=record['job'],stage='step-0',file='episode-0',version=version,
                             seed=seed,**{k:record.get(k) for k in ('outcome','score','mowers','wall_s')}))
        if clean_win(record):
            return True
        evidence,mowers=record['job'],record['mowers']
        reason='lose' if record['outcome']!='win' else 'mowers'
    for attempt in range(1,MAX_LEVEL_ATTEMPTS+1):
        shown=len({(a.get('run'),a.get('attempt')) for a in log.load()['attempts']})+1  # rounds on this level, all runs
        progress.update(round=shown,phase='rsi',level=level,variants={},target=dict(level=level,reason=reason,mowers=mowers))
        if resumed and attempt==1:
            group,names=resumed['group'],resumed['variants']
        else:
            group=f'{name}-L{level}-attempt{attempt}'
            names={letter:group+letter for letter in PARALLEL_FIXES}
        def run_variant(letter,evidence=evidence,group=group,names=names):
            job_name=names[letter]
            def status(job):
                progress.variant(letter,job=job['id'],status=job['status'],detail=job.get('detail') or job.get('reason'))
            return with_reconnect(bridge,progress,lambda: rsi_round(config,bridge,root,job_name,progress,on_status=status,
                create=lambda: bridge.call('portable_level_attempt',name=job_name,level=level,evidence_job=evidence,
                                           group=group,angle=letter)))
        # Parallel variants from the same evidence; the server keeps at most the best one.
        letters=sorted(names)
        with ThreadPoolExecutor(max_workers=len(letters)) as pool:
            futures={letter:pool.submit(run_variant,letter) for letter in letters}
        failures=[f.exception() for f in futures.values() if f.exception()]
        if failures:
            raise failures[0]
        progress.update(notice='')
        ids=[futures[letter].result()['id'] for letter in letters]
        kept=with_reconnect(bridge,progress,lambda: bridge.call('portable_level_keep',jobs=ids))
        best,jobs=kept['best'],kept['jobs']
        for letter,job in zip(letters,jobs):
            job=job|{'tests':futures[letter].result().get('tests')}
            result=job.get('result') or {}
            for game in test_games(root,job):
                log.add('games',game|{'attempt':shown,'variant':letter,'version':job.get('candidate')})
            log.add('attempts',dict(attempt=shown,variant=letter,run=name,job=job['id'],reason=reason,base_version=version,
                                    version=job.get('candidate'),kept=bool(job.get('promoted')),cleared=bool(job.get('cleared')),
                                    duplicate_of=job.get('duplicate_of'),status=job['status'],result=result,tests=job.get('tests'),
                                    diagnosis=job.get('diagnosis'),rationale=job.get('rationale'),changes=job.get('changes'),
                                    detail=job.get('reason'),coach_tokens=job.get('coach_tokens'),
                                    **({'resumed': True} if resumed and attempt==1 else {})))
            progress.update(rounds=progress.state['rounds']+[dict(round=shown,variant=letter,lost_level=level,reason=reason,
                                                                 job=job['id'],status=job['status'],result=result)])
        chosen=next((j for j in jobs if j['id']==best),None)
        if chosen:
            version=chosen.get('candidate')
            evidence=chosen['id']  # its test game is now the level playbook's newest evidence
            result=chosen.get('result') or {}
            reason,mowers='lose' if result.get('outcome')!='win' else 'mowers',result.get('mowers')
            if chosen.get('cleared'):
                write_json(root/f'{name}-L{level}-cleared.json',[dict(level=level,outcome='win',score=result.get('score'),
                                                                     mowers=0,job=chosen['id'])])
                return True
        # Nothing kept: the level still plays the same playbook, same evidence next round.
    return False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('data/portable_rsi/config.json'))
    parser.add_argument('--name',required=True)
    parser.add_argument('--mode',choices=['campaign','rsi','both','train'],default='both')
    parser.add_argument('--level',type=int,choices=range(1,11),help='train mode: only this level, on a fresh seed')
    args=parser.parse_args()
    config=json.loads(args.config.read_text(encoding='utf-8-sig'))
    root=Path(config['work_dir']); root.mkdir(parents=True,exist_ok=True)
    preflight(config)
    with file_lock(root/'WORKER.lock',blocking=False):
        progress=Progress(root,args.name,args.mode) if args.mode=='train' else None
        bridge=Link(config,root) if args.mode=='train' else SSHBridge(config,root)
        summary=dict(name=args.name,started=time.time(),status='running')
        try:
            summary['before']=bridge.call('portable_info')
            if args.mode=='train':
                summary['closed_stale']=bridge.call('portable_close_stale',keep_prefix=args.name)['closed']
            if args.mode=='train' and args.level:
                train_one_level(config,bridge,root,args.name,args.level,progress)
            elif args.mode=='train':
                train(config,bridge,root,args.name,progress)
            if args.mode in ('campaign','both'):
                summary['campaign_before']=campaign(config,bridge,root,args.name+'-before')
            if args.mode in ('rsi','both'):
                summary['rsi']=rsi_round(config,bridge,root,args.name+'-rsi')
                if summary['rsi']['status']=='promoted':
                    summary['campaign_after']=campaign(config,bridge,root,args.name+'-after')
            summary.update(after=bridge.call('portable_info'),status='complete')
        except BaseException as exc:
            summary.update(status='blocked',error=f'{type(exc).__name__}: {exc}')
            if progress:
                stopped=isinstance(exc,Stopped)
                progress.update(state='stopped' if stopped else 'blocked',
                                message='已按停止键停止' if stopped else summary['error'][:500])
            raise
        finally:
            bridge.close()
            write_json(root/(args.name+'-summary.json'),summary)


if __name__ == '__main__': main()
