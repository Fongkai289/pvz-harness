import json
from pathlib import Path
import pytest
from common.local_config import ssh_settings, remote_command


def test_private_config_and_explicit_overrides(tmp_path):
    p=tmp_path/'local.json'
    p.write_text(json.dumps({'ssh':{'host':'user@host.invalid','port':2222,'key':'keys/client',
                                    'remote_dir':'/srv/my project'}}))
    result=ssh_settings({'port':2200},config_path=p)
    assert result['port']==2200 and Path(result['key'])==tmp_path/'keys/client'
    assert remote_command(result,'python -m server.app')=="cd '/srv/my project' && python -m server.app"


def test_missing_private_config_does_not_guess_a_server(tmp_path):
    with pytest.raises(ValueError,match='Missing ssh.host'):
        ssh_settings(config_path=tmp_path/'missing.json')


@pytest.mark.parametrize('override',[{'host':'host; touch /tmp/a'},{'port':True},{'port':0},
                                     {'port':65536},{'remote_dir':'/'},{'remote_dir':'/srv/../etc'}])
def test_invalid_destination_is_rejected(tmp_path,override):
    base={'host':'user@host.invalid','port':22,'key':'key','remote_dir':'/srv/project'}
    base.update(override)
    p=tmp_path/'local.json';p.write_text(json.dumps({'ssh':base}))
    with pytest.raises(ValueError):ssh_settings(config_path=p)
