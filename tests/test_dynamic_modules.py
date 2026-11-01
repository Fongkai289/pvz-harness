"""Topology changes preserve interfaces, version gates and filesystem boundaries."""
import json

import pytest

from rsi.code_bundle import FILES, HEADER, DYNAMIC_HEADER, compile_bundle, snapshot, unpack_source
from rsi.code_engineer import EngineerAction, EngineerWorkspace
from rsi.code_runtime import CandidateProgram
from rsi.native_player import SEED


def action(name, **kwargs):
    return EngineerAction(action=name, explanation='test topology change', **kwargs)


@pytest.fixture
def workspace(tmp_path):
    return EngineerWorkspace(SEED, tmp_path/'engineer', {'docs/contract.txt': 'immutable'},
                             [], tmp_path, helper_names=(), deferred=True)


def test_create_merge_split_and_rename_preserve_executable_interfaces(workspace):
    helper = 'def inspect_rows(image):\n    return {"rows": [4]}\n'
    perception = 'def perceive(image):\n    return inspect_rows(image)\n'
    workspace.dispatch(action('edit_modules', writes={
        'player/perception.py': perception, 'player/vision/rows.py': helper}))
    assert len(workspace.files) == 6
    assert CandidateProgram(compile_bundle(workspace.files, ())).perceive({}) == {'rows': [4]}
    assert 'player/vision/rows.py' in workspace.dispatch(action('list_files'))['editable']
    combined = '\n'.join(workspace.files.values())
    workspace.dispatch(action('edit_modules', writes={'player/all.py': combined}, deletes=list(workspace.files)))
    assert list(workspace.files) == ['player/all.py']
    assert not (workspace.root/'work/player/vision/rows.py').exists()
    workspace.dispatch(action('edit_modules', deletes=['player/all.py'], writes={
        'player/input.py': perception + '\n' + helper,
        'player/decision/main.py': unpack_source(SEED, ())['player/policy.py']}))
    source = compile_bundle(workspace.files, ())
    assert len(workspace.files) == 2
    assert CandidateProgram(source).perceive({}) == {'rows': [4]}
    assert CandidateProgram(source).decide({}, {})['request_replan'] is False
    assert compile_bundle(unpack_source(source, ()), ()) == source
    assert compile_bundle(dict(reversed(list(workspace.files.items()))), ()) == source


def test_legacy_hash_layout_is_unchanged_and_single_module_roundtrips():
    files = unpack_source(SEED, ())
    expected = HEADER + ''.join(f'# file: {p}\n{files[p].rstrip()}\n\n' for p in FILES)
    assert compile_bundle(files, ()) == expected
    source = compile_bundle({'player/core.py': SEED}, ())
    assert source.startswith(DYNAMIC_HEADER)
    assert compile_bundle(unpack_source(source, ()), ()) == source


@pytest.mark.parametrize('path', [
    '../judge.py', 'rules/code_gate.py', 'C:/Windows/x.py', '/player/x.py',
    'player/../judge.py', 'player//x.py', 'player/X.py', 'player/x.py:stream',
    'player/x\\y.py', 'player/x.txt', 'player/con.py', 'player/nul/x.py',
    'player/com1.py', 'player/x./y.py', 'player/x/../../judge.py',
])
def test_invalid_batch_has_no_partial_effect(workspace, path):
    before = dict(workspace.files)
    original_file = (workspace.root/'work/player/perception.py').read_bytes()
    with pytest.raises(ValueError):
        workspace.dispatch(action('edit_modules', writes={
            'player/new.py': 'def added():\n    return 1\n', path: 'bad'},
            deletes=['player/perception.py']))
    assert workspace.files == before
    assert (workspace.root/'work/player/perception.py').read_bytes() == original_file
    assert not (workspace.root/'work/player/new.py').exists()


def test_deletion_manifest_version_gate_and_resume(workspace):
    workspace.dispatch(action('edit_modules', writes={'player/core.py': SEED}, deletes=list(workspace.files)))
    result = workspace.dispatch(action('test_candidate'))
    workspace.tested_version = result['version']  # Represents authenticated development feedback.
    finish = action('finish', diagnosis='consolidate', rationale='development evidence')
    assert workspace.dispatch(finish)['finished']
    manifest = json.loads((workspace.root/'manifest.json').read_text())
    assert manifest['added_files'] == ['player/core.py']
    assert manifest['deleted_files'] == sorted(FILES)
    tested_source = (workspace.root/'test1-candidate.py').read_bytes()
    (workspace.root/'work/keep.txt').write_text('not a module')
    workspace.dispatch(action('edit_modules', writes={'player/renamed.py': SEED}, deletes=['player/core.py']))
    with pytest.raises(ValueError, match='exact current'):
        workspace.dispatch(finish)
    assert (workspace.root/'test1-candidate.py').read_bytes() == tested_source
    assert (workspace.root/'work/keep.txt').read_text() == 'not a module'
    assert (workspace.root/'before/player/perception.py').exists()
    restored = EngineerWorkspace(SEED, workspace.root, workspace.readonly, [], workspace.controls,
                                 helper_names=(), deferred=True, working_files=workspace.files)
    assert restored.files == workspace.files
    assert not (restored.root/'work/player/core.py').exists()
    assert not (restored.root/'work/player/perception.py').exists()


@pytest.mark.parametrize('duplicate', [False, True])
def test_entry_interfaces_still_required_exactly_once(workspace, duplicate):
    if duplicate:
        workspace.dispatch(action('write_file', path='player/extra.py', content=SEED))
    else:
        workspace.dispatch(action('delete_file', path='player/perception.py'))
    with pytest.raises(ValueError):
        workspace.dispatch(action('check'))
    with pytest.raises(ValueError):
        workspace.dispatch(action('test_candidate'))
    assert workspace.tests == 0


def test_snapshot_rejects_link_before_touching_any_module(tmp_path):
    root, outside = tmp_path/'work', tmp_path/'outside'
    snapshot(root, {'player/core.py': SEED})
    outside.mkdir()
    try:
        (root/'player/link').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('directory symlinks unavailable on this host')
    with pytest.raises(ValueError, match='escaped|links|junctions'):
        snapshot(root, {'player/core.py': 'changed', 'player/link/new.py': SEED})
    assert (root/'player/core.py').read_text() == SEED
    assert not list(outside.iterdir())
