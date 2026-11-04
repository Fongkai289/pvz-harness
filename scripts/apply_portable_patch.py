"""Apply and verify the pinned Portable harness patch; never reset an existing checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def apply(repository):
    repository=Path(repository).resolve()
    package=ROOT/'third_party/pvz-portable'
    manifest=json.loads((package/'manifest.json').read_text())
    patch=package/'harness.patch'
    assert hashlib.sha256(patch.read_bytes()).hexdigest()==manifest['patch_sha256'],'Patch checksum mismatch'
    def git(*args,check=True):
        return subprocess.run(['git','-C',str(repository),*args],check=check,capture_output=True,text=True)
    if git('rev-parse','HEAD').stdout.strip()!=manifest['revision']:
        raise ValueError('Checkout the pinned revision from third_party/pvz-portable/manifest.json first')
    def exact():
        return all((repository/name).is_file() and hashlib.sha256((repository/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()==sha
                   for name,sha in manifest['files'].items())
    if exact():
        print('Pinned harness already present; all file hashes match')
        return
    result=git('apply','--check',str(patch),check=False)
    if result.returncode:
        raise RuntimeError('Patch does not apply cleanly; preserve your changes and use a clean checkout.\n'+result.stderr)
    git('apply',str(patch))
    if not exact():raise RuntimeError('Applied files differ from the manifest')
    print('Applied and verified all harness files')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository',type=Path)
    apply(parser.parse_args().repository)
