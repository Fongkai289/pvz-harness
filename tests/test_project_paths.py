import json
from pathlib import Path

from common.local_config import ssh_settings
from common.project_paths import load_runtime_config, portable_path, resolve_runtime_paths


def test_config_survives_workspace_rename_and_unrelated_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / 'Old workspace'
    app = workspace / 'pvz-harness/app'
    (app / 'data/portable_rsi').mkdir(parents=True)
    (workspace / 'operator_key').write_text('fixture only')
    repo = workspace / 'pvz-harness/external/Portable'
    repo.mkdir(parents=True)
    (repo / 'game.exe').write_text('fixture only')
    config = dict(repository='../external/Portable', game_executable='../external/Portable/game.exe',
                  work_dir='data/portable_rsi/worker', workers=2, dll=None)
    (app/'data/portable_rsi/config.json').write_text(json.dumps(config))
    (app/'local.json').write_text(json.dumps({'ssh':dict(host='operator@server.example.com',key='../../operator_key',remote_dir='/srv/pvz-harness')}))
    renamed = tmp_path / 'Renamed workspace'
    workspace.rename(renamed)
    app = renamed / 'pvz-harness/app'
    monkeypatch.chdir(tmp_path)
    loaded = load_runtime_config('data/portable_rsi/config.json', root=app)
    assert Path(loaded['game_executable']).is_file()
    assert Path(loaded['work_dir']) == app/'data/portable_rsi/worker'
    assert loaded['dll'] is None and loaded['workers'] == 2
    assert Path(ssh_settings(config_path=app/'local.json')['key']).is_file()
    assert json.loads((app/'data/portable_rsi/config.json').read_text()) == config


def test_absolute_external_installation_stays_absolute(tmp_path):
    app=tmp_path/'workspace/app'
    external_game=tmp_path/'Steam/Game'
    config={'resources':str(external_game),'repository':'../external/Portable','workers':2}
    resolved=resolve_runtime_paths(config,root=app)
    assert Path(resolved['resources']) == external_game
    assert portable_path(external_game,root=app) == str(external_game)
    assert portable_path(app.parent/'external/Portable',root=app) == '../external/Portable'
    assert config['repository'] == '../external/Portable'
