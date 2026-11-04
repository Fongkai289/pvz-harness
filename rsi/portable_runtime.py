"""Owned Portable process; production models are called only by the SSH bridge."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import uuid

from server.scene import Scene, Card, Zombie
from rsi.native_runtime import PLANTS, ZOMBIES

UPSTREAM = '98b659380014c89c3553fbb2839f5e5a84d6df3d'
NAMES = dict(enumerate(PLANTS)) | {-1:'empty',49:'explode-o-nut',50:'giant wall-nut'}


def preflight(config):
    repo=Path(config['repository']).resolve()
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    if revision!=UPSTREAM: raise ValueError('Portable upstream changed')
    exe=Path(config['game_executable'])
    resources=Path(config['resources'])
    if not (resources/'main.pak').is_file() or not (resources/'properties').is_dir():
        raise ValueError('complete owned GOTY resources required')
    # Bind all local adapter edits as well as the actual binary, not just git HEAD.
    diff=subprocess.check_output(['git','diff','HEAD'],cwd=repo)
    sources=diff+(repo/'src/Harness.cpp').read_bytes()+(repo/'tools/harness_benchmark.py').read_bytes()
    return dict(upstream=revision,backend='pvz_portable',
                game_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
                adapter_sha256=hashlib.sha256(sources.replace(b'\r\n',b'\n')).hexdigest(),
                assets_sha256=hashlib.sha256((resources/'main.pak').read_bytes()).hexdigest())


def player_state(raw, mask, tick):
    g=raw['global_state']
    cards=raw['cards']
    if [c['slot'] for c in cards]!=list(range(len(cards))): raise ValueError('noncontiguous Portable cards')
    plants={}
    for p in raw['plants']:
        plants.setdefault((p['row'],p['col']),[]).append(NAMES[p['type']])
    zombies=[]; nearest={}
    for z in raw['zombies']:
        row=z['row']; col=max(0,min(9,int((z['x']-40)//80)))
        zombies.append(Zombie(type=ZOMBIES[z['type']],row=row,col=col))
        nearest[row]=min(nearest.get(row,9),col)
    scene=Scene(frame_id=tick,t_capture=tick/100,lawn='day',rows=5,sun=g['sun'],
        cards=[Card(slot=c['slot'],plant=NAMES[c['plant_type']],ready=c['ready'],brightness=0,
                    cost=c['cost']) for c in cards], zombies=zombies,nearest_col=nearest,
        positions='portable_training_state',shovel=g.get('shovel'))
    return dict(scene=scene.model_dump(),plants=[dict(row=r,col=c,plants=p) for (r,c),p in plants.items()],
        allowed_positions=[[s,r,c] for s in range(len(cards)) for r in range(5) for c in range(9)
                           if mask[1+s*54+r*9+c]])


class PortableArena:
    def __init__(self,config,mode):
        self.identity=preflight(config)
        self.mode=mode
        repo=Path(config['repository']).resolve()
        module_spec=importlib.util.spec_from_file_location('portable_pipe',repo/'tools/harness_benchmark.py')
        module=importlib.util.module_from_spec(module_spec); module_spec.loader.exec_module(module)
        self.config=config; self.engine_class=module.Engine; self.has_reset=False
        self._start()
        self.seen_mowers=set(); self.level=None

    def _start(self):
        config=self.config
        root=Path(config['work_dir'])/'instances'/uuid.uuid4().hex
        root.mkdir(parents=True)
        # The build/runtime DLL path is local to this child; no system PATH writes.
        self.engine=self.engine_class(Path(config['game_executable']).resolve(),Path(config['resources']).resolve(),root,
                                  runtime_bin=config['runtime_bin'])

    def _info(self,state,placement=None):
        mask=[False]*757; mask[0]=True
        for slot,row,col in state['allowed']: mask[1+slot*54+row*9+col]=True
        events={row:t for row,t in enumerate(state['mower_ticks']) if t>=0 and row not in self.seen_mowers}
        self.seen_mowers.update(events)
        return dict(raw_observation=dict(backend='pvz_portable',global_state=dict(scene=0,grid_rows=5,
                sun=state['sun'],current_wave=state['wave'],total_waves=state['waves'],shovel=state.get('shovel')),
                plants=state['plants'],zombies=state['zombies'],
                cards=[dict(slot=c['slot'],plant_type=c['type'],ready=c['ready'],cost=c['cost']) for c in state['cards']]),
            game_clock=state['tick'],rng_seed_support=2,rng_seed_applied=True,level_id=self.level,playthrough=1,
            observation_quality='fresh',replay_eligible=True,action_mask=mask,
            action_result=dict(success=True) if placement else None,
            events=dict(mower_triggered_rows=list(events),mowers_used=len(events),mower_trigger_ticks=events),
            result={'win':'WON','lose':'LOST','running':'NONE'}[state['outcome']])

    def _fresh(self,level):
        # Cache/profile state in a reused engine can perturb later episodes.
        # A fresh owned process+save per episode costs startup time, but pairs fairly.
        if self.has_reset:
            self.engine.close()
            self._start()
        self.has_reset=True
        self.level=level; self.seen_mowers=set()

    def reset(self,level,seed):
        self._fresh(level)
        return self._info(self.engine.call(f'reset {level} {seed}'))

    def prepare(self,level,seed):
        """reset() that stops at the seed chooser: (offer, None) there, else (None, info) already playing.

        The offer is what the chooser screen shows: free slots, unlocked plants, previewed zombie types."""
        from server.seed_choice import SEED_TYPES
        self._fresh(level)
        state=self.engine.call(f'prepare {level} {seed}')
        if not state.get('chooser'):
            return None,self._info(state)
        return dict(level=level,slots=state['slots'],available=[SEED_TYPES[t] for t in state['available']],
                    preview=[ZOMBIES[z] for z in state['preview']]),None

    def choose(self,plants):
        from server.seed_choice import SEED_TYPES
        return self._info(self.engine.call('choose '+' '.join(str(SEED_TYPES.index(p)) for p in plants)))

    def step(self,placement):
        args='-1 0 0' if placement is None else '{slot} {row} {col}'.format(**placement)
        state=self.engine.call('step 25 '+args)
        return state['terminal'],state['tick']>=60000 and not state['terminal'],self._info(state,placement)

    def shovel(self,cell):
        """Dig up the plant in this cell with the game's own shovel; no ticks pass."""
        self.engine.call('dig {row} {col}'.format(**cell))

    def pixels(self):
        import cv2
        self.engine.call('render')
        frame=cv2.imread(str(self.engine.root/'save/observation.ppm'))
        if frame is None or frame.shape[:2]!=(600,800) or frame.std()<2:
            raise ValueError('invalid Portable frame')
        ok,jpeg=cv2.imencode('.jpg',frame)
        if not ok: raise ValueError('image encoding failed')
        return jpeg.tobytes()

    def close(self):
        self.engine.close()
