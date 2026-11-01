import zipfile

import pytest

from scripts.prepare_pvz1065 import extract_checked


@pytest.mark.parametrize('name', ['../escaped.txt', '/absolute.txt', 'C:/absolute.txt', 'safe\\..\\escaped.txt'])
def test_archive_paths_rejected_before_any_extraction(tmp_path, name):
    archive = tmp_path/'resources.zip'
    with zipfile.ZipFile(archive, 'w') as zipped:
        zipped.writestr('main.pak', b'fixture')
        zipped.writestr(name, b'bad')
    with pytest.raises(ValueError, match='Unsafe archive'):
        extract_checked(archive, tmp_path/'game')
    assert not (tmp_path/'game/main.pak').exists()
    assert not (tmp_path/'escaped.txt').exists()


def test_nested_resource_layout_is_preserved(tmp_path):
    archive = tmp_path/'resources.zip'
    with zipfile.ZipFile(archive, 'w') as zipped:
        zipped.writestr('game/main.pak', b'fixture')
        zipped.writestr('game/bass.dll', b'fixture')
    extract_checked(archive, tmp_path/'output')
    assert (tmp_path/'output/game/main.pak').read_bytes() == b'fixture'
