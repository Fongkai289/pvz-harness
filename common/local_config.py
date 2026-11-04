"""Private operator configuration; never contains defaults for a real deployment."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex

ROOT = Path(__file__).resolve().parents[1]


def ssh_settings(overrides=None, *, config_path=None):
    path = Path(config_path or os.environ.get('PVZ_LOCAL_CONFIG', ROOT / 'local.json'))
    values = {}
    if path.is_file():
        values.update(json.loads(path.read_text(encoding='utf-8-sig')).get('ssh', {}))
    values.update({k: v for k, v in (overrides or {}).items() if v not in (None, '', 0)})
    for name in ('host', 'key', 'remote_dir'):
        if not isinstance(values.get(name), str) or not values[name].strip():
            raise ValueError(f'Missing ssh.{name}: copy local.example.json to local.json and configure it')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*', values['host']):
        raise ValueError('ssh.host must be one SSH destination (user@host or an SSH alias)')
    port = values.get('port', 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('ssh.port must be an integer in 1..65535')
    values['port'] = port
    remote = values['remote_dir']
    if not PurePosixPath(remote).is_absolute() or remote == '/' or '..' in PurePosixPath(remote).parts or '\n' in remote or '\r' in remote:
        raise ValueError('ssh.remote_dir must be an absolute project directory, not /')
    key = Path(os.path.expandvars(values['key'])).expanduser()
    if not key.is_absolute():
        key = path.resolve().parent / key
    values['key'] = str(key.resolve())
    return values


def remote_command(ssh, command):
    return 'cd ' + shlex.quote(ssh['remote_dir']) + ' && ' + command
