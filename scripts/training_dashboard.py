"""Local control center: training overview, one-click training, and client lifecycle controls."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from common.file_lock import file_lock
from rsi.portable_loop import SKIPPED, TRAIN_LEVELS, campaign_records, clean_win, mower_wins, won_levels

CACHE={}
LAUNCH={}  # the training process this control center started: {name, process, log}


def panel_request(action=None):
    path='/status' if action is None else '/action/'+action
    request=Request('http://127.0.0.1:8767'+path, data=b'' if action else None,
                    headers={'X-PvZ-Panel':'local'})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=1) as response:
            value=json.load(response)
        if action is None and time.time()-value.get('updated',0)>3:
            return 503, {'online':False,'message':'客户端未响应；已禁用控制按钮'}
        return (202 if action else 200),value
    except HTTPError as exc:
        return exc.code,json.load(exc)
    except (URLError, OSError, ValueError):
        return 503,{'online':False,'message':'真实游戏客户端未连接。点“启动客户端”即可（游戏可以之后再开），连上后此处自动刷新。'}

def read(path, default=None):
    try:
        stamp=path.stat().st_mtime_ns
        if path not in CACHE or CACHE[path][0]!=stamp:
            CACHE[path]=(stamp,json.loads(path.read_text(encoding='utf-8')))
        return CACHE[path][1]
    except (OSError,ValueError): return default


def snapshot(worker, job):
    folder=worker/job; status=read(worker/'status.json',{})
    if status.get('id',status.get('job'))!=job: status={}
    reports=[]
    for path in sorted(folder.glob('step-*/episode-*.json')):
        ep=read(path,{})
        if not ep.get('complete'):continue
        reports.append({**{k:ep.get(k) for k in ('level','outcome','score','elapsed_game_s','wall_s','mower_times')},
                        'stage':path.parent.name,'file':path.stem})
    tokens=read(ROOT/'data/codex_coach_worker/tokens.json',[])
    rounds=[r for r in tokens if job in r['round_id']]
    summary_path=worker/(job.removesuffix('-rsi')+'-summary.json')
    summary=read(summary_path,{})
    status={**status,**summary.get('rsi',{})}
    if summary.get('status')=='blocked':
        status={**status,'status':'blocked','detail':summary.get('error') or status.get('detail')}
    elif summary.get('status')=='complete' and not summary.get('rsi'):
        status={**status,'status':'complete'}
    updated_paths=[worker/'status.json',summary_path] if status else []
    updated=max((p.stat().st_mtime for p in updated_paths if p.exists()),default=None)
    fields=('status','step','mode','side','completed','total','workers','parent','candidate','changes',
            'diagnosis','rationale','verdicts','promoted','detail','reason')
    public_status={k:status[k] for k in fields if k in status}
    if public_status.get('total') is None:
        step=status.get('step',0)
        suite='development' if step<3 else 'confirmation' if step<5 else 'audit'
        specs=status.get('suites',{}).get(suite)
        if specs is not None:public_status['total']=len(specs)
    return {'job':job,'time':time.time(),'updated':updated,'status':public_status,
            'episodes':reports,'coach':rounds,
            'summary':{k:summary[k] for k in ('started','status','before','after') if k in summary}}


def action_nodes(episode):
    """Join actual placements to compact observations; never invent persistent plants."""
    post=episode.get('postgame') or {}
    timeline=sorted(post.get('timeline',[]),key=lambda p:p['t'])
    observations=sorted(episode.get('milestones',[])+episode.get('trace',[]),key=lambda p:p['tick'])
    names=(post.get('legend') or {}).get('plants',{})
    labels={'cherry bomb':'樱桃炸弹','sunflower':'向日葵','peashooter':'豌豆射手',
            'potato mine':'土豆雷','wall-nut':'坚果'}
    nodes=[]
    for decision in post.get('decisions',[]):
        placement=decision.get('placement')
        if not placement or decision.get('unexecutable') or decision.get('rejected'):continue
        tick=decision['t']
        prior=next((p for p in reversed(observations) if p['tick']<=tick),None)
        cards=(prior or {}).get('scene',{}).get('cards',[])
        plant=next((c['plant'] for c in cards if c['slot']==placement['slot']),None)
        label=labels.get(plant,plant or '未知植物')
        before=next((p for p in reversed(timeline) if p['t']<=tick),None)
        action=dict(tick=tick,plant=plant,placement=placement,source=decision.get('source','unknown'))

        def node(sample, after=False):
            plants=[]
            for row,line in enumerate(sample.get('rows',[])):
                for col,code in enumerate(line):
                    if code!='.':plants.append(dict(row=row,col=col,plants=[names.get(code,'未知植物')]))
            return dict(tick=sample['t'] if after else tick,
                label=(f'{label}后观察' if after else f'种植{label} · 第{placement["row"]+1}行第{placement["col"]+1}列'),
                scene=dict(t_capture=sample['t']/100,rows=len(sample.get('rows',[])) or 5,sun=sample.get('sun'),
                    cards=[{**c,'ready':c['slot'] in sample.get('ready',[])} for c in cards],zombies=[]),
                plants=plants,placement=None if after else placement,recorded_action=action,
                action_phase='after' if after else 'before',observation_kind='compact',
                zombie_summary=sample.get('z',[]),plan=decision.get('plan_brief'),plan_scope='动作时规划摘要',
                decision_observation_tick=tick,mower_events=[])
        if before is not None:nodes.append(node(before))
        # Short-lived bombs can appear and disappear between adjacent samples.
        # Keep both the first following sample and the first >=1s later, if saved.
        if plant=='cherry bomb':
            after=next((p for p in timeline if p['t']>tick),None)
            later=next((p for p in timeline if p['t']>=tick+100),None)
            for sample in {p['t']:p for p in (after,later) if p is not None}.values():
                nodes.append(node(sample,after=True))
    return nodes


def episode_detail(path):
    episode=read(path)
    if not episode or not episode.get('complete'):return None
    points={p['tick']:p for p in episode.get('milestones',[])
            if p['tick']==0 or p['tick']>=500 or p.get('mower_triggered_rows')}
    trace=episode.get('trace',[])
    if trace:points[trace[-1]['tick']]=trace[-1]
    nodes=[]
    mower_index=0
    for tick,point in sorted(points.items()):
        rows=point.get('mower_triggered_rows') or []
        events=[]
        for row in rows:
            times=episode.get('mower_times') or []
            events.append({'row':row,'time_s':times[mower_index] if mower_index<len(times) else None})
            mower_index+=1
        label=('开局' if tick==0 else '小推车触发' if rows else
               '终局前最后观察' if trace and tick==trace[-1]['tick'] else '定时观察')
        nodes.append({**{k:point.get(k) for k in ('tick','scene','plants','plan','placement','timings','decision_reasons','decision_observation_tick')},
                      'label':label,'mower_events':events})
    actions=action_nodes(episode)
    action_ticks={n['tick'] for n in actions if n['action_phase']=='before'}
    nodes=[n for n in nodes if n['tick'] not in action_ticks or n['mower_events']]+actions
    return {'level':episode['level'],'outcome':episode['outcome'],'elapsed_game_s':episode['elapsed_game_s'],
            'wall_s':episode.get('wall_s'),'throughput':episode.get('throughput',{}),
            'nodes':sorted(nodes,key=lambda n:(n['tick'],n.get('action_phase','')))}


def worker_running(worker):
    """A training or evaluation worker holds WORKER.lock for its whole run."""
    try:
        with file_lock(worker/'WORKER.lock',blocking=False):return False
    except OSError:return True


def log_tail(path, lines=6):
    try:return '\n'.join(path.read_text(encoding='utf-8',errors='replace').splitlines()[-lines:])
    except OSError:return ''


LIVE_FRESH_S=90  # a game's live file is rewritten every decision; older ones belong to finished games


def live_games(worker, job):
    """Games of this job still in progress (one per evaluation lane): level, game time, decisions, lawn counts."""
    if not job or not re.fullmatch('[A-Za-z0-9_-]+',job):return []
    now=time.time()
    files=sorted((p for p in (worker/job/'live').glob('*.json')
                  if not p.name.endswith('-frames.json') and now-p.stat().st_mtime<=LIVE_FRESH_S),
                 key=lambda p:p.stat().st_mtime)
    games=[]
    for path in files:
        value=read(path)
        if not value:continue
        scene=value.get('scene') or {}
        games.append({'job':job,'episode':path.stem,'level':value.get('level'),'game_s':round((value.get('tick') or 0)/100),
                      'decisions':value.get('decisions'),'sun':scene.get('sun'),'zombies':len(scene.get('zombies') or []),
                      'plants':sum(len(p.get('plants') or []) for p in value.get('plants') or []),
                      'updated':path.stat().st_mtime})
    return games


def live_frames(worker, job, episode):
    """Key frames of a game in progress (player-side boards), shaped like replay nodes."""
    data=read(worker/job/'live'/f'{episode}-frames.json',{}) or {}
    names={'cherry bomb':'樱桃炸弹','sunflower':'向日葵','peashooter':'豌豆射手','potato mine':'土豆雷','wall-nut':'坚果',
           'snow pea':'寒冰射手','repeater':'双发射手','chomper':'大嘴花'}
    nodes=[]
    for frame in data.get('frames') or []:
        scene,place=frame.get('scene') or {},frame.get('placement')
        plant=next((c.get('plant') for c in scene.get('cards') or [] if place and c.get('slot')==place.get('slot')),None)
        label=(f"种植{names.get(plant,plant or '植物')} · 第{place['row']+1}行第{place['col']+1}列" if place else '定时观察')
        nodes.append({'tick':frame.get('tick'),'scene':scene,'plants':frame.get('plants') or [],'placement':place,
                      'plan':{'objective':frame.get('plan')},'plan_scope':'当时的规划目标','label':label,'mower_events':[],
                      'decision_observation_tick':frame.get('tick')})
    return {'level':data.get('level'),'nodes':nodes,'live':True}


def evaluation_progress(worker, job):
    """Which gate stage a Coach round is on and how many of its games are finished."""
    from rsi.native_jobs import STEPS
    # Between stages status.json is the job ({id, suites}); during games it is the runner's {job, completed, total}.
    status=read(worker/'status.json',{}) or {}
    if (status.get('id') or status.get('job'))!=job or not isinstance(status.get('step'),int) or not 0<=status['step']<len(STEPS):
        return None
    step=status['step']; stage=STEPS[step][0]
    done=status.get('completed')
    if not isinstance(done,int):
        done=sum(bool((read(p) or {}).get('complete')) for p in (worker/job/f'step-{step}').glob('episode-*.json'))
    total=status.get('total') or len((status.get('suites') or {}).get(stage) or [])
    return {'step':step,'stage':stage,'mode':STEPS[step][1],'side':STEPS[step][2],'completed':done,'total':total,
            'candidate':status.get('candidate'),'status':status.get('status')}


def training_status(worker):
    state=dict(read(worker/'training.json',{}) or {})
    running=worker_running(worker)
    launch=LAUNCH.get('process')
    launched_here=launch is not None and state.get('name')!=LAUNCH['name']
    if launched_here and launch.poll() is None:
        state.update(state='starting',name=LAUNCH['name'],message='正在连接服务器并检查游戏环境')
        running=True
    elif launched_here:
        state.update(state='failed_to_start',name=LAUNCH['name'],
                     message='训练进程启动后退出：\n'+log_tail(LAUNCH['log']))
    elif state.get('state')=='running' and not running:
        state.update(state='interrupted',message='训练进程已不在运行（窗口被关闭或异常退出）')
    # Recomputed from the campaign records so the strip is right before a run and after a crash.
    state.update(levels=list(TRAIN_LEVELS),skipped=SKIPPED,won=sorted(won_levels(worker,clean=True)),
                 mower_wins=sorted(mower_wins(worker)))
    state['running']=running
    state['stopping']=running and (worker/'STOP').exists()
    state['live']=live_games(worker,state.get('job')) if running else []
    state['evaluation']=evaluation_progress(worker,state.get('job')) if running and state.get('phase')=='rsi' else None
    return state


def start_training(worker, level=None):
    """Full per-level training, or with level only that level on a fresh seed."""
    if worker_running(worker) or (LAUNCH.get('process') and LAUNCH['process'].poll() is None):
        return 409,{'error':'已有训练或评测在运行'}
    for name in ('STOP','PAUSE'):  # a deliberate new start clears the last stop/pause request
        (worker/name).unlink(missing_ok=True)
    if level is not None and (type(level) is not int or level not in TRAIN_LEVELS):
        return 400,{'error':'只能单独训练 1-2 至 1-10'}
    name=time.strftime('train-%Y%m%d-%H%M%S')+('' if level is None else f'-L{level}')
    log=worker/(name+'.log')
    with log.open('wb') as stream:
        process=subprocess.Popen([sys.executable,'-X','utf8','-u','-m','rsi.portable_loop','--name',name,'--mode','train',
                                  *([] if level is None else ['--level',str(level)])],
                                 cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    LAUNCH.update(name=name,process=process,log=log)
    return 202,{'name':name}


def level_history(worker):
    """Per level: every game played and every Coach attempt (work_dir/levels/*.json), plus older campaign
    records from before per-level training, so the overview can show how often each level was played."""
    levels={}
    for level in range(1,11):
        data=read(worker/'levels'/f'{level}.json',{}) or {}
        levels[level]={'level':level,'games':list(data.get('games') or []),'attempts':list(data.get('attempts') or [])}
    logged={g.get('job') for entry in levels.values() for g in entry['games']}
    for record in campaign_records(worker):
        if record.get('job') and record['job'] in logged:
            continue
        levels[record['level']]['games'].append({'kind':'level_game','job':record.get('job'),
            'stage':'step-0' if record.get('job') else None,'file':'episode-0' if record.get('job') else None,
            **{k:record.get(k) for k in ('outcome','score','mowers','wall_s')},'older':True})
    for entry in levels.values():
        games=entry['games']
        entry.update(played=len(games),wins=sum(g.get('outcome')=='win' for g in games),
                     clean_wins=sum(clean_win(g) for g in games),tries=len(entry['attempts']),
                     rounds=len({a.get('attempt') for a in entry['attempts']}),
                     kept=sum(bool(a.get('kept')) for a in entry['attempts']),
                     version=next((a.get('version') for a in reversed(entry['attempts']) if a.get('kept')),
                                  next((g.get('version') for g in reversed(games) if g.get('version')),None)))
    return {'levels':[levels[l] for l in range(1,11)],'skipped':SKIPPED,'trained':list(TRAIN_LEVELS)}


CLIENT_LAUNCH={}  # the client launcher this control center started


def launch_client():
    """Run scripts/start_client.ps1 in its own visible window (it may ask for the token there)."""
    process=CLIENT_LAUNCH.get('process')
    if process is not None and process.poll() is None:
        return 409,{'error':'启动窗口已经打开，请看任务栏里的“PvZ 客户端”窗口'}
    script=ROOT/'scripts/start_client.ps1'
    CLIENT_LAUNCH['process']=subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script)],
                                              cwd=ROOT,creationflags=getattr(subprocess,'CREATE_NEW_CONSOLE',0))
    return 202,{'launched':True}


def stop_training(worker):
    if not worker_running(worker) and not (LAUNCH.get('process') and LAUNCH['process'].poll() is None):
        return 409,{'error':'没有正在运行的训练'}
    (worker/'STOP').write_text('stop requested from control center\n',encoding='utf-8')
    return 202,{'stopping':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job');parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args()
    worker=ROOT/'data/portable_rsi/worker'
    pinned=args.job
    if pinned and not re.fullmatch('[A-Za-z0-9_-]+',pinned):raise ValueError('invalid job')

    def current_job():
        """The pinned job, else whatever job the worker is on now (training moves between jobs)."""
        if pinned:return pinned
        status=read(worker/'status.json',{}) or {}
        job=status.get('id') or status.get('job') or 'no-training'
        return job if re.fullmatch('[A-Za-z0-9_-]+',job) else 'no-training'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.headers.get('Host')!=f'127.0.0.1:{args.port}':
                self.reply_json(403,{'error':'请使用 127.0.0.1 打开控制中心'});return
            url=urlparse(self.path)
            if url.path=='/':
                data=(ROOT/'client/training_dashboard.html').read_bytes();mime='text/html; charset=utf-8'
            elif url.path=='/training_dashboard.js':
                data=(ROOT/'client/training_dashboard.js').read_bytes();mime='text/javascript; charset=utf-8'
            elif url.path=='/api/status':
                data=json.dumps(snapshot(worker,current_job()),ensure_ascii=False).encode();mime='application/json; charset=utf-8'
            elif url.path=='/api/training':
                self.reply_json(200,training_status(worker));return
            elif url.path=='/api/levels':
                self.reply_json(200,level_history(worker));return
            elif url.path=='/api/live':
                query=parse_qs(url.query);job=query.get('job',[''])[0];episode=query.get('episode',[''])[0]
                if not re.fullmatch('[A-Za-z0-9_-]+',job) or not re.fullmatch('[0-9a-f]{32}',episode):
                    self.send_error(400);return
                self.reply_json(200,live_frames(worker,job,episode));return
            elif url.path=='/api/client':
                code,value=panel_request()
                self.reply_json(code,value);return
            elif url.path=='/client':
                data=(ROOT/'client/operator_dashboard.html').read_bytes();mime='text/html; charset=utf-8'
            elif url.path=='/api/episode':
                query=parse_qs(url.query);stage=query.get('stage',[''])[0];file=query.get('file',[''])[0]
                job=query.get('job',[current_job()])[0]
                if (not re.fullmatch(r'step-\d+(-engineer-\d+)?',stage) or not re.fullmatch(r'episode-\d+',file)
                        or not re.fullmatch('[A-Za-z0-9_-]+',job)):
                    self.send_error(400);return
                detail=episode_detail(worker/job/stage/(file+'.json'))
                if detail is None:self.reply_json(404,{'error':'尚无完整对局记录'});return
                self.reply_json(200,detail);return
            else:self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type',mime)
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)))
            self.end_headers();self.wfile.write(data)
        def reply_json(self, code, value):
            data=json.dumps(value,ensure_ascii=False).encode()
            self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)))
            self.end_headers();self.wfile.write(data)
        def do_POST(self):
            expected=f'127.0.0.1:{args.port}'
            if (self.headers.get('Host')!=expected or self.headers.get('Origin')!='http://'+expected
                    or self.headers.get('X-PvZ-Control')!='1'):
                self.reply_json(403,{'error':'仅允许从本机控制台操作'});return
            url=urlparse(self.path)
            if url.path=='/api/client/launch':
                code,value=launch_client()
                self.reply_json(code,value);return
            if url.path in ('/api/training/start','/api/training/stop'):
                level=parse_qs(url.query).get('level',[None])[0]
                if url.path.endswith('stop'):code,value=stop_training(worker)
                elif level is not None and not level.isdigit():code,value=400,{'error':'invalid level'}
                else:code,value=start_training(worker,None if level is None else int(level))
                self.reply_json(code,value);return
            action=self.path.removeprefix('/api/client/')
            if self.path!='/api/client/'+action or action not in ('start','pause','resume','end','choose_seeds','test_links'):
                self.reply_json(404,{});return
            code,value=panel_request(action)
            self.reply_json(code,value)
    print(f'http://127.0.0.1:{args.port} — {pinned or "following the current job"}',flush=True)
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()


if __name__=='__main__':main()
