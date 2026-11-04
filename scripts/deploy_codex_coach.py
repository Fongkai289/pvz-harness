"""Deploy the coherent player/Coach sources and run tests before enabling Codex.

Only this project's files and tmux pvz are changed. Credentials stay on Windows.
"""
from pathlib import Path
import base64
import ipaddress
import json
import subprocess

ROOT=Path(__file__).resolve().parents[1]
FILES=sorted({p.relative_to(ROOT).as_posix() for folder in ('rsi','server','rules','common','tests')
              for p in (ROOT/folder).glob('*.py')} | {
    'client/operator_view.py','docs/codex-coach.md','docs/HANDOFF_windows.md',
    'docs/REDESIGN_agnes.md','scripts/codex_coach.ps1','scripts/prepare_pvz1065.py','.env.example'})
REMOTE=r'''
from pathlib import Path
import base64,json,subprocess,tarfile,time,urllib.request,hashlib
root=Path('/srv/pvz-harness')
assert root.resolve()==root
health=lambda:json.load(urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=5))
initial=health(); assert initial['ok'] and not initial['sessions'],'Active game'
backup=root/'data/deploy_backups'/('before-codex-coach-'+time.strftime('%Y%m%d-%H%M%S')+'.tgz')
backup.parent.mkdir(parents=True,exist_ok=True)
with tarfile.open(backup,'w:gz') as archive:
    for name in list(FILES)+['.env']:
        p=root/name
        assert p.resolve().is_relative_to(root)
        if p.is_file(): archive.add(p,arcname=name)
backup.chmod(0o600)
print('BACKUP='+str(backup),flush=True)
for name,encoded in FILES.items():
    p=root/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(base64.b64decode(encoded))
subprocess.run([str(root/'.venv/bin/python'),'-m','pytest','-q','--disable-warnings'],cwd=root,check=True)
assert not health()['sessions'],'Game started during deployment; configuration left unchanged'
env=root/'.env'; lines=env.read_text().splitlines()
values={'PVZ_COACH_PROVIDER':'codex','PVZ_COACH_MODEL':'gpt-6-astra','PVZ_COACH_TIMEOUT':'240'}
lines=[line for line in lines if line.split('=',1)[0].strip() not in values]
env.write_text('\n'.join(lines+[k+'='+v for k,v in values.items()])+'\n');env.chmod(0o600)
subprocess.run(['tmux','kill-session','-t','pvz'],check=True)
subprocess.run(['tmux','new-session','-d','-s','pvz','-c',str(root),'bash scripts/run_server.sh'],check=True)
for _ in range(30):
    try:
        h=health()
        if h['ok'] and h.get('coach_provider')=='codex':break
    except Exception:pass
    time.sleep(.5)
else:raise RuntimeError('New service did not become healthy; backup preserved')
assert h['playbook']==initial['playbook']
manifest={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in FILES}
(root/'data/codex-coach-deploy.json').write_text(json.dumps({'backup':str(backup),'files':manifest,'health':h},indent=2))
print(json.dumps({'health_ok':h['ok'],'coach_provider':h['coach_provider'],'coach_model':h['coach_model'],
                  'playbook':h['playbook'],'verified_files':len(manifest)}),flush=True)
'''

def main():
    network=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-Command',
        ". './scripts/connect_server.ps1'; Get-SshBindAddress"],cwd=ROOT,capture_output=True,text=True,
        encoding='utf-8',errors='replace',timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    network.check_returncode()
    args=['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',
          '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
          '-i',str(ROOT.parent.parent/'pvz_operator'),'-p','22']
    if network.stdout.strip():args+=['-b',str(ipaddress.IPv4Address(network.stdout.strip()))]
    args+=['operator@server.example.com','cd /srv/pvz-harness && .venv/bin/python -u -']
    payload={name:base64.b64encode((ROOT/name).read_bytes().replace(b'\r\n',b'\n')).decode() for name in FILES}
    subprocess.run(args,input=('FILES='+repr(payload)+'\n'+REMOTE).encode(),check=True)

if __name__=='__main__':main()
