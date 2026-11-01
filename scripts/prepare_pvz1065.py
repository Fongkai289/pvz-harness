"""Build a private 1065 copy from the user's Steam EXE and community delta.

No game is launched. Config changes only after the upstream preflight passes.
Source instructions: https://www.speedrun.com/pvz/guides/swt9q
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EXTERNAL = ROOT.parent.parent / 'external'
PATCH_URL = 'https://www.speedrun.com/static/resource/qwrly.xdelta?v=e3af881'
RESOURCES_URL = 'https://www.speedrun.com/static/resource/5lam4.zip?v=6e73f4e'
TOOL_URL = 'https://github.com/jmacd/xdelta/releases/download/v3.2.2/xdelta3-3.2.2-windows-x86_64.zip'
TOOL_SHA = 'd48f7651d86b78fdca397057b46601f89131f37219675df736db70af44ec3379'
STEAM_SHA = '868f8e2bab0d6a7ef8afc4c5960c608eccef82bd086bd6e0c0e2670199a5ca45'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def obtain(url, path, limit):
    if path.is_file() and path.stat().st_size:
        return
    print(f'Downloading {url}', flush=True)
    partial = path.with_suffix(path.suffix + '.part')
    request = urllib.request.Request(url, headers={'User-Agent': 'pvz-harness-setup/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response, partial.open('wb') as out:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError('Download exceeds expected size bound')
            out.write(chunk)
    partial.replace(path)


def extract_checked(archive, target):
    target = target.resolve()
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > 20000 or sum(e.file_size for e in entries) > 512 * 1024 * 1024:
            raise ValueError('Archive exceeds size bound')
        for entry in entries:
            name = PurePosixPath(entry.filename)
            if (name.is_absolute() or '..' in name.parts or ':' in entry.filename
                    or '\\' in entry.filename or stat.S_ISLNK(entry.external_attr >> 16)
                    or not (target / entry.filename).resolve().is_relative_to(target)):
                raise ValueError('Unsafe archive path')
        zipped.extractall(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--steam-exe', type=Path, default=Path('E:/Steam/steamapps/common/Plants Vs Zombies/PlantsVsZombies.exe'))
    parser.add_argument('--cache', type=Path, default=EXTERNAL/'pvz-1065-setup')
    parser.add_argument('--destination', type=Path, default=EXTERNAL/'pvz-1.2.0.1065')
    parser.add_argument('--config', type=Path, default=ROOT/'data/native_rsi/config.json')
    args = parser.parse_args()
    if sha(args.steam_exe) != STEAM_SHA:
        raise ValueError('Steam source differs from the inspected 1.2.0.1096 EXE; recheck before patching')
    if args.destination.exists():
        raise ValueError(f'Destination already exists; inspect it before retrying: {args.destination}')
    args.cache.mkdir(parents=True, exist_ok=True)
    patch, resources, tool = (args.cache/name for name in ('steam-to-1065.xdelta', 'accompanying-1065.zip', 'xdelta.zip'))
    obtain(PATCH_URL, patch, 20*1024*1024)
    obtain(RESOURCES_URL, resources, 256*1024*1024)
    obtain(TOOL_URL, tool, 10*1024*1024)
    if sha(tool) != TOOL_SHA:
        raise ValueError('Official xdelta archive checksum mismatch')
    if patch.read_bytes()[:3] != bytes.fromhex('d6c3c4'):
        raise ValueError('Patch is not VCDIFF; possible HTML download error')
    # Fresh staging keeps failed attempts reviewable and never overwrites Steam.
    import tempfile
    stage = Path(tempfile.mkdtemp(prefix='prepare-', dir=args.cache))
    extract_checked(tool, stage/'tool')
    extract_checked(resources, stage/'resources')
    patcher = stage/'tool/xdelta3-3.2.2-windows-x86_64/xdelta3.exe'
    packs = list((stage/'resources').rglob('main.pak'))
    if len(packs) != 1:
        raise ValueError('Expected one complete resource directory containing main.pak')
    game = stage/'game'
    shutil.copytree(packs[0].parent, game)
    executable = game/'PlantsVsZombies.exe'
    if executable.exists():
        raise ValueError('Resource archive unexpectedly includes a game EXE')
    subprocess.run([str(patcher), '-d', '-s', str(args.steam_exe), str(patch), str(executable)], check=True, timeout=60)
    if sha(args.steam_exe) != STEAM_SHA:
        raise ValueError('Original Steam source changed during setup')
    from rsi.native_runtime import preflight
    from rsi.code_store import write_json
    original_config = args.config.read_text(encoding='utf-8-sig')
    config = json.loads(original_config)
    config['game_executable'] = str(executable)
    identity = preflight(config)
    # Destination is new and within the requested workspace by default.
    shutil.copytree(game, args.destination)
    config['game_executable'] = str((args.destination/'PlantsVsZombies.exe').resolve())
    preflight(config)
    if args.config.read_text(encoding='utf-8-sig') != original_config:
        raise ValueError('Config changed concurrently; game prepared but config not overwritten')
    (stage/'config-before.json').write_text(original_config, encoding='utf-8')
    write_json(args.config, config)
    record = dict(status='preflight_passed_not_launched', game_executable=config['game_executable'],
                  steam_sha256=STEAM_SHA, patch_sha256=sha(patch), resources_sha256=sha(resources),
                  xdelta_sha256=TOOL_SHA, patch_url=PATCH_URL, resources_url=RESOURCES_URL, **identity)
    write_json(args.cache/'prepared.json', record)
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(f'Preparation stopped: {exc}', file=sys.stderr)
        sys.exit(1)
