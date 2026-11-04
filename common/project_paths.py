"""Resolve local runtime paths against the app root, independent of the caller's cwd."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PATHS = ('repository', 'game_executable', 'dll', 'work_dir', 'resources', 'runtime_bin')


def project_path(value, *, root=ROOT):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(root) / path).resolve()


def resolve_runtime_paths(config, *, root=ROOT):
    result = dict(config)
    for name in RUNTIME_PATHS:
        if result.get(name):
            result[name] = str(project_path(result[name], root=root))
    return result


def load_runtime_config(path, *, root=ROOT):
    config = json.loads(project_path(path, root=root).read_text(encoding='utf-8-sig'))
    return resolve_runtime_paths(config, root=root)


def portable_path(value, *, root=ROOT):
    """Keep app and sibling dependency paths relative; external installations stay absolute."""
    import os
    path, root = Path(value).resolve(), Path(root).resolve()
    if path.is_relative_to(root.parent):
        return Path(os.path.relpath(path, root)).as_posix()
    return str(path)
