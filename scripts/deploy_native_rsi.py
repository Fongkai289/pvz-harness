"""Back up and deploy only native-RSI files; no shared model service changes."""
from pathlib import Path
import subprocess
import tarfile
import argparse
import ipaddress

parser=argparse.ArgumentParser()
parser.add_argument('--package-only',action='store_true')
args=parser.parse_args()

root = Path(__file__).resolve().parents[1]
files = '''common/file_lock.py rsi/native_bridge.py rsi/native_gate.py rsi/native_jobs.py
rsi/native_loop.py rsi/native_runtime.py rsi/native_player.py rsi/native_engineer.py
rsi/native_schedule.py rsi/native_transport.py rsi/native_arena_worker.py
rsi/code_bundle.py rsi/code_engineer.py rsi/code_store.py rsi/code_coach.py rsi/code_model.py
rsi/code_runtime.py rsi/code_evaluate.py rsi/pixel_tools.py rules/code_gate.py rsi/coach.py server/playbook.py
server/config.py server/app.py server/action_selector.py server/live_reviews.py
server/session.py rules/playbook_rules.py rules/replan_rules.py server/action_gate.py server/planner.py
rsi/postgame.py rsi/review_live.py tests/test_system0_actions.py tests/test_replan.py
server/mechanics.py tests/test_seed_estimates.py tests/test_fallback.py
rules/lessons.py server/lessons.py tests/test_lessons.py
rsi/book_sync.py rsi/human_edit.py tests/test_book_sync.py server/threats.py knowledge/pvz_mechanics.json
rsi/portable_bridge.py tests/test_orphan_bridge.py tests/test_targeted_round.py
server/level_books.py tests/test_level_training.py
client/operator_view.py scripts/native_rsi.ps1 scripts/deploy_native_rsi.py tests/test_native_rsi.py
tests/test_pvzrl_contract.py tests/test_live_reviews.py tests/test_native_engineer.py tests/test_dynamic_modules.py docs/native-rsi.md
tests/test_native_throughput.py
docs/HANDOFF_windows.md docs/strategy-messages.md .env.example'''.split()
package = root / 'data/manual_test/native-rsi.tgz'
with tarfile.open(package, 'w:gz') as archive:
    for file in files:
        archive.add(root / file, arcname=file)
print('PACKAGE='+str(package),flush=True)
if args.package_only:
    raise SystemExit(0)
key = str(root.parent.parent / 'pvz_operator')
host = 'operator@server.example.com'
base = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=yes', '-i', key]
network=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-Command',
                       ". './scripts/connect_server.ps1'; Get-SshBindAddress"],cwd=root,capture_output=True,
                       text=True,encoding='utf-8',errors='replace',timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
if network.returncode: raise RuntimeError('Could not determine Wi-Fi address')
address=network.stdout.strip()
bind=['-o','BindAddress='+str(ipaddress.IPv4Address(address))] if address else []
subprocess.run(['scp', *base, *bind, '-P', '22',
                str(package), host + ':/srv/pvz-harness/data/native-rsi-upload.tgz'], check=True)
remote = r'''
from pathlib import Path
from datetime import datetime
import json, tarfile, subprocess, urllib.request
root = Path('/srv/pvz-harness').resolve()
health = json.load(urllib.request.urlopen('http://127.0.0.1:8765/health'))
assert not health['sessions'], 'Active sessions: refuse restart during play'
with tarfile.open('/srv/pvz-harness/data/native-rsi-upload.tgz') as archive:
    members = archive.getmembers()
    assert all(m.isfile() and (root/m.name).resolve().is_relative_to(root) for m in members)
    backup = root/'data'/'deploy_backups'/('before-native-rsi-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.tgz')
    backup.parent.mkdir(exist_ok=True)
    with tarfile.open(backup, 'w:gz') as old:
        for member in members:
            if (root/member.name).exists(): old.add(root/member.name, arcname=member.name)
    backup.chmod(0o600)
    print('BACKUP='+str(backup), flush=True)
    archive.extractall(root, filter='data')
checks = ['tests/test_native_rsi.py','tests/test_native_engineer.py','tests/test_native_throughput.py','tests/test_dynamic_modules.py','tests/test_live_reviews.py','tests/test_server.py',
          'tests/test_action_selector.py','tests/test_rsi.py','tests/test_isolation.py',
          'tests/test_decision_view.py','tests/test_review_live.py','tests/test_pvzrl_contract.py',
          'tests/test_system0_actions.py']
subprocess.run([str(root/'.venv/bin/python'),'-m','pytest',*checks,'-q','--disable-warnings'],cwd=root,check=True)
health = json.load(urllib.request.urlopen('http://127.0.0.1:8765/health'))
assert not health['sessions'], 'Active sessions: refuse restart during play'
subprocess.run(['tmux','kill-session','-t','pvz'],check=True)
subprocess.run(['tmux','new-session','-d','-s','pvz','-c',str(root),'bash scripts/run_server.sh'],check=True)
print('RESTARTED=pvz',flush=True)
'''
subprocess.run(['ssh', *base, *bind, '-p', '22', host,
                'cd /srv/pvz-harness && .venv/bin/python -'],
               input=remote.encode('utf-8'), check=True)
