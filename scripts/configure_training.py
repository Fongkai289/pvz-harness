"""Write an ignored local Portable config without starting a game or model."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.project_paths import portable_path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resources',type=Path,required=True,help='Your GOTY resource directory with main.pak and properties/')
    parser.add_argument('--force',action='store_true',help='Replace an existing local training configuration')
    args=parser.parse_args()
    resources=args.resources.resolve()
    if not (resources/'main.pak').is_file() or not (resources/'properties').is_dir():
        parser.error('resources must contain main.pak and properties/')
    external=ROOT.parent/'external'
    out=ROOT/'data/portable_rsi/config.json'
    if out.exists() and not args.force:parser.error('configuration exists; inspect it or pass --force')
    config={'backend':'pvz_portable','repository':str(external/'PvZ-Portable'),
            'game_executable':str(external/'PvZ-Portable/build-harness/pvz-portable.exe'),
            'runtime_bin':str(external/'msys64/ucrt64/bin'),'resources':str(resources),
            'work_dir':str(ROOT/'data/portable_rsi/worker'),'workers':2,
            'decision_schedule':{'seconds':2,'planner_seconds':20,'event_min_seconds':.5,
                'events':['new_threat','threat_advance','plant_lost','resource_ready'],'selection':'flat'}}
    for name in ('repository','game_executable','runtime_bin','resources','work_dir'):
        config[name]=portable_path(config[name])
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(config,indent=2)+'\n')
    print('Wrote',out,'; SSH settings are read from local.json')


if __name__=='__main__':main()
