"""Protocol check only; no strategy or model evaluation."""
import json
import argparse
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.project_paths import load_runtime_config
from rsi.portable_runtime import PortableArena

config=load_runtime_config('data/portable_rsi/config.json')
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--parity',action='store_true')
args=parser.parse_args()
arena=PortableArena(config,'state')
try:
    for level in ([] if args.parity else range(1,11)):
        info=arena.reset(level,424242)
        frame=arena.pixels()
        path=ROOT/'data/portable_rsi'/f'level-{level}-initial.jpg'; path.write_bytes(frame)
        print(json.dumps(dict(level=level,clock=info['game_clock'],
            cards=info['raw_observation']['cards'],image_bytes=len(frame))),flush=True)
    if args.parity:
        paths=[]
        for render in (False,False,True):
            arena.reset(4,99222)
            state=arena.engine.call('state'); path=[state]
            while not state['terminal'] and state['tick']<60000:
                if render: arena.pixels()
                state=arena.engine.call('step 1000 -1 0 0'); path.append(state)
            assert state['terminal']
            paths.append(path)
        result=dict(kind='WAIT-only protocol check',same_seed_equal=paths[0]==paths[1],render_preserves_observations=paths[0]==paths[2],
                    final_tick=paths[0][-1]['tick'],identity=arena.identity)
        (ROOT/'data/portable_rsi/render-parity.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        (ROOT/'data/portable_rsi/render-traces.json').write_text(json.dumps(paths),encoding='utf-8')
        print(json.dumps(result),flush=True)
        assert result['render_preserves_observations']
finally:
    arena.close()
