"""Move this project's remote files to the user-requested shared filesystem.

Copies and hashes first; switches only with no active game. Removes the old path
after verification. Never touches model services or other tmux sessions.
"""
from pathlib import Path
import ipaddress
import subprocess
import base64
import json

ROOT=Path(__file__).resolve().parents[1]

REMOTE=r'''
from pathlib import Path
import hashlib,json,os,shutil,stat,subprocess,time,urllib.request
import base64

source=Path('/srv/pvz-harness')
parent=Path('/srv/projects')
target=parent/'pvz-agent'
stamp=time.strftime('%Y%m%d-%H%M%S')
staging=parent/('.pvz-agent-migrating-'+stamp)
rollback=source.parent/('.pvz-agent-rollback-'+stamp)
assert source.is_dir() and not source.is_symlink() and source.resolve()==source
assert parent.is_dir() and parent.resolve()==parent and os.access(parent,os.W_OK)
assert not any(os.path.lexists(p) for p in (target,staging,rollback))
assert not (os.statvfs(parent).f_flag & getattr(os,'ST_NOEXEC',8))

def health():
    return json.load(urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=5))

initial=health()
assert initial['ok'] and not initial['sessions'], 'Active game; do not migrate'
staging.mkdir(mode=0o700)
count=0
def copy_file(a,b):
    global count
    result=shutil.copy2(a,b)
    count+=1
    if count%1000==0: print('COPIED_FILES='+str(count),flush=True)
    return result

print('COPYING='+str(staging),flush=True)
for child in source.iterdir():
    dest=staging/child.name
    if child.is_symlink(): dest.symlink_to(os.readlink(child),target_is_directory=child.is_dir())
    elif child.is_dir(): shutil.copytree(child,dest,symlinks=True,copy_function=copy_file)
    else: copy_file(child,dest)

def fingerprint(p):
    info=p.lstat()
    if stat.S_ISLNK(info.st_mode): return ('link',os.readlink(p))
    if stat.S_ISDIR(info.st_mode): return ('dir',)
    assert stat.S_ISREG(info.st_mode), 'Unexpected non-regular project entry: '+str(p)
    digest=hashlib.sha256()
    with p.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): digest.update(block)
    return ('file',info.st_size,digest.hexdigest(),stat.S_IMODE(info.st_mode))

def manifest(root):
    result={}
    for folder,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:
            path=Path(folder)/name
            result[path.relative_to(root).as_posix()]=fingerprint(path)
    return result

# Check interpreter/imports at the new filesystem before interrupting the service.
subprocess.run([str(staging/'.venv/bin/python'),'-B','-c',
    'import fastapi,httpx,cv2,numpy; print("NEW_RUNTIME_IMPORTS_OK",flush=True)'],cwd=staging,check=True)
assert health()['sessions']==[], 'A game started during copy; original service left running'
print('STOPPING_OWN_SERVICE=pvz',flush=True)
subprocess.run(['tmux','kill-session','-t','pvz'],check=True)
switched=False
try:
    # Ensure shutdown completed before the final immutable snapshot.
    for _ in range(30):
        try: health()
        except Exception: break
        time.sleep(.2)
    else: raise RuntimeError('Application still answers after its tmux session stopped')

    before=manifest(source)
    copied=manifest(staging)
    extras=set(copied)-set(before)
    # Do not silently delete unexpected files. Copy has not been used as an app.
    assert not extras, 'Unexpected staging entries; preserve both copies for inspection'
    changed=0
    for name,expected in before.items():
        if copied.get(name)==expected: continue
        a=source/name; b=staging/name
        if expected[0]=='dir': b.mkdir(parents=True,exist_ok=True)
        elif expected[0]=='link':
            if os.path.lexists(b): b.unlink()
            b.symlink_to(os.readlink(a),target_is_directory=a.is_dir())
        else:
            b.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(a,b)
        changed+=1
    after=manifest(staging)
    assert before==after, 'Content/permission verification failed; original retained'
    print('HASH_VERIFIED_ENTRIES='+str(len(before))+' FINAL_SYNC_CHANGES='+str(changed),flush=True)
    staging.rename(target)
    source.rename(rollback)
    switched=True
    patched=[]
    for relative in PATH_FILES:
        path=target/relative
        if not path.is_file(): continue
        original=path.read_bytes()
        updated=original.replace(str(source).encode(),str(target).encode())
        if updated!=original:
            path.write_bytes(updated)
            patched.append(relative)
    for relative,encoded in UPDATES.items():
        path=target/relative
        assert path.resolve().is_relative_to(target) and path.parent.is_dir(), relative
        path.write_bytes(base64.b64decode(encoded))
        if relative not in patched: patched.append(relative)
    subprocess.run([str(target/'.venv/bin/pytest'),'tests/test_portable.py',
        'tests/test_native_rsi.py','tests/test_native_engineer.py','tests/test_native_throughput.py',
        'tests/test_action_selector.py','tests/test_scene.py','tests/test_isolation.py',
        '-q','--disable-warnings'],cwd=target,check=True)
    subprocess.run(['tmux','new-session','-d','-s','pvz','-c',str(target),'bash scripts/run_server.sh'],check=True)
    restored=None
    for _ in range(40):
        try:
            restored=health()
            if restored.get('ok'): break
        except Exception: pass
        time.sleep(.5)
    assert restored and restored['ok'] and restored['playbook']==initial['playbook'], 'New service health failed'
    assert not os.path.lexists(source), 'Old project path must not remain'
    pane=subprocess.check_output(['tmux','display-message','-p','-t','pvz','#{pane_current_path}'],text=True).strip()
    assert Path(pane).resolve()==target, 'Service not running in destination'
    assert manifest(rollback)==before, 'Old source changed after cutover; preserve it'
    # All old bytes were verified at destination before launch. Remove only the
    # exact rollback tree created above, never paths computed outside this root.
    assert rollback.parent==Path('/srv/operator') and rollback.name.startswith('.pvz-agent-rollback-')
    assert rollback.resolve()==rollback and not rollback.is_symlink()
    shutil.rmtree(rollback)
    report=dict(source=str(source),destination=str(target),old_path_exists=False,patched_files=patched,
        verified_entries=len(before),copied_files=count,final_sync_changes=changed,
        health=restored,service_cwd=pane,completed=time.time(),old_physical_copy_removed=True)
    audit=target/'data/migrations'; audit.mkdir(parents=True,exist_ok=True)
    (audit/(stamp+'.json')).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('MIGRATION_COMPLETE='+json.dumps(report),flush=True)
except BaseException:
    if switched and rollback.is_dir():
        subprocess.run(['tmux','kill-session','-t','pvz'],check=False)
        if not os.path.lexists(source) and rollback.is_dir():
            rollback.rename(source)
    if source.is_dir() and not source.is_symlink():
        subprocess.run(['tmux','new-session','-d','-s','pvz','-c',str(source),'bash scripts/run_server.sh'],check=False)
    print('Migration failed; copies preserved and original service restart attempted',flush=True)
    raise
'''

def main():
    network=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-Command',
        ". './scripts/connect_server.ps1'; Get-SshBindAddress"],cwd=ROOT,capture_output=True,text=True,
        encoding='utf-8',errors='replace',timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    network.check_returncode()
    address=network.stdout.strip()
    args=['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',
          '-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
          '-i',str(ROOT.parent.parent/'pvz_operator'),'-p','22']
    if address: args+=['-b',str(ipaddress.IPv4Address(address))]
    args+=['operator@server.example.com','cd / && /usr/bin/python3 -u -']
    updates={name:base64.b64encode((ROOT/name).read_bytes()).decode() for name in (
        'scripts/deploy.ps1','scripts/deploy_native_rsi.py','scripts/deploy_portable.py',
        'docs/HANDOFF_windows.md','docs/ARCHITECTURE.md','docs/PROJECT.md')}
    # Existing remote files retain their contents except this project's prefix.
    discover=r'''
PATH_FILES=[]
for folder in ('scripts','docs','rsi','.venv/bin'):
    for p in (target if target.exists() else source).joinpath(folder).rglob('*'):
        if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts:
            if folder=='.venv/bin' or p.suffix in ('.py','.ps1','.sh','.md'):
                PATH_FILES.append(p.relative_to(source).as_posix())
PATH_FILES+=['README.md','.env']
'''
    script=REMOTE.replace("initial=health()",discover+"\ninitial=health()")
    script='UPDATES='+repr(updates)+'\n'+script
    subprocess.run(args,input=script.encode(),check=True)

if __name__=='__main__': main()
