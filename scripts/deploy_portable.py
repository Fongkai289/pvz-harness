"""Deploy scoped research RPC support without restarting any shared service."""
from pathlib import Path
import subprocess
from remote_config import ssh_settings, remote_command
import tarfile
import ipaddress

root=Path(__file__).resolve().parents[1]
ssh=ssh_settings()
files='''common/local_config.py common/project_paths.py scripts/remote_config.py rsi/portable_runtime.py rsi/portable_bridge.py rsi/portable_loop.py
rsi/native_runtime.py rsi/native_loop.py rsi/native_jobs.py rsi/native_engineer.py
rsi/native_bridge.py rsi/native_schedule.py rsi/native_transport.py
server/scene.py server/action_selector.py rules/hard_rules.py tests/test_portable.py
rsi/native_player.py rsi/code_runtime.py rsi/code_bundle.py rsi/code_engineer.py rsi/coach.py
rsi/postgame.py rsi/review_live.py rules/playbook_rules.py rules/replan_rules.py server/action_gate.py server/planner.py
server/config.py server/session.py tests/test_system0_actions.py tests/test_replan.py tests/test_native_engineer.py
tests/test_scene.py server/mechanics.py tests/test_seed_estimates.py tests/test_fallback.py
rules/lessons.py server/lessons.py tests/test_lessons.py docs/portable-rsi.md docs/HANDOFF_windows.md'''.split()
package=root/'data/portable_rsi/deploy.tgz'
with tarfile.open(package,'w:gz') as archive:
    for path in files: archive.add(root/path,arcname=path)
network=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-Command',
    ". './scripts/connect_server.ps1'; Get-SshBindAddress"],cwd=root,capture_output=True,text=True,
    encoding='utf-8',errors='replace',timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
network.check_returncode()
address=network.stdout.strip()
base=['-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',
      '-i',ssh['key']]
if address: base+=['-o','BindAddress='+str(ipaddress.IPv4Address(address))]
host=ssh['host']
subprocess.run(['scp',*base,'-P',str(ssh['port']),str(package),host+':'+ssh['remote_dir']+'/data/portable-support-upload.tgz'],check=True)
remote=r'''
from pathlib import Path
from datetime import datetime
import tarfile, subprocess, json
root=Path.cwd().resolve()
backup=root/'data'/'deploy_backups'/('before-portable-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.tgz')
backup.parent.mkdir(parents=True,exist_ok=True)
with tarfile.open(root/'data/portable-support-upload.tgz') as archive:
    members=archive.getmembers()
    assert all(m.isfile() and (root/m.name).resolve().is_relative_to(root) for m in members)
    with tarfile.open(backup,'w:gz') as old:
        for m in members:
            if (root/m.name).is_file(): old.add(root/m.name,arcname=m.name)
    backup.chmod(0o600)
    archive.extractall(root,filter='data')
print('BACKUP='+str(backup),flush=True)
subprocess.run([str(root/'.venv/bin/python'),'-m','pytest','tests/test_portable.py',
    'tests/test_native_rsi.py','tests/test_native_engineer.py','tests/test_native_throughput.py',
    'tests/test_action_selector.py','tests/test_scene.py','tests/test_isolation.py',
    'tests/test_system0_actions.py','-q','--disable-warnings'],cwd=root,check=True)
print('Portable RPC ready; live service not restarted',flush=True)
'''
subprocess.run(['ssh',*base,'-p',str(ssh['port']),host,remote_command(ssh,'.venv/bin/python -')],
               input=remote.encode(),check=True)
