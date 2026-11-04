"""Editable player modules compiled together under the existing restricted runtime.

Paths are capability names, never model-selected paths on the host filesystem.
The serialized source retains module boundaries through CURRENT/promotions.
"""
from __future__ import annotations

import ast
import json
import re
import stat
from pathlib import Path

from rsi.code_runtime import validate_source

def _helpers(names):
    if names is None:
        from rsi.pixel_tools import HELPERS
        return HELPERS
    return names

# Legacy seed layout only; this is not the editable capability list.
FILES = (
    "player/perception.py", "player/state.py", "player/actions.py",
    "player/policy.py", "player/tools.py",
)
HEADER = "# pvz-player-bundle-v1\n"
DYNAMIC_HEADER = "# pvz-player-bundle-v2\n"
MARKER = "# file: "
MODULE_SCOPE = "player/**/*.py (lowercase identifier names; no imports; paths cannot escape player/)"


def validate_module_path(path: str) -> None:
    if (not isinstance(path,str) or len(path)>160 or
            not re.fullmatch(r"player/(?:[a-z][a-z0-9_]{0,47}/)*[a-z][a-z0-9_]{0,47}\.py",path)):
        raise ValueError("only normalized Python modules under player/ are writable")
    reserved = {"con","prn","aux","nul"} | {f"{p}{n}" for p in ("com","lpt") for n in range(1,10)}
    if any(part in reserved for part in path[:-3].split('/')[1:]):
        raise ValueError("Windows reserved module path is not writable")


def module_order(files):
    # Preserve old serialized sources exactly; new names have deterministic order.
    return [p for p in FILES if p in files] + sorted(set(files)-set(FILES))


def validate_layout(files):
    if type(files) is not dict:
        raise ValueError("modules must be a path/source mapping")
    total = len(DYNAMIC_HEADER)
    for path,source in files.items():
        validate_module_path(path)
        if not isinstance(source,str) or len(source.encode('utf-8'))>24000:
            raise ValueError(f"module too large: {path}")
        total += len(MARKER) + len(path) + len(source) + 3
    if total > 24000:
        raise ValueError("combined player source exceeds 24000 characters")


def compile_bundle(files: dict[str, str], helper_names=None) -> str:
    validate_layout(files)
    for path, source in files.items():
        if not isinstance(source, str) or len(source.encode("utf-8")) > 24000:
            raise ValueError(f"module too large: {path}")
        if any(line.startswith(MARKER) or line in {HEADER.strip(),DYNAMIC_HEADER.strip()} for line in source.splitlines()):
            raise ValueError("reserved bundle marker in module")
    header = HEADER if set(files)==set(FILES) else DYNAMIC_HEADER
    result = header + "".join(f"{MARKER}{path}\n{files[path].rstrip()}\n\n" for path in module_order(files))
    if len(result) > 24000:
        raise ValueError("combined player source exceeds 24000 characters")
    validate_source(result, _helpers(helper_names))
    return result


def unpack_source(source: str, helper_names=None) -> dict[str, str]:
    header = next((h for h in (HEADER,DYNAMIC_HEADER) if source.startswith(h)), None)
    if header:
        files = {}
        current = None
        lines = []
        for line in source[len(header):].splitlines(keepends=True):
            if line.startswith(MARKER):
                if current is not None:
                    files[current] = "".join(lines).rstrip() + "\n"
                current = line[len(MARKER):].strip()
                validate_module_path(current)
                if current in files or (header==HEADER and current not in FILES):
                    raise ValueError("invalid or repeated player module")
                lines = []
            else:
                if current is None:
                    raise ValueError("bundle content outside module")
                lines.append(line)
        if current is not None:
            files[current] = "".join(lines).rstrip() + "\n"
        if header==HEADER and set(files)!=set(FILES):
            raise ValueError("incomplete legacy module bundle")
        compile_bundle(files, helper_names)
        return files
    # Migrate legacy candidates mechanically; do not change their decisions.
    validate_source(source, _helpers(helper_names))
    parts = {path: [] for path in FILES}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef):
            path = {"perceive": FILES[0], "decide": FILES[3],
                    "free": FILES[2], "ready": FILES[2], "place": FILES[2],
                    "options": FILES[2], "act": FILES[2], "reflex": FILES[2]}.get(node.name, FILES[4])
            parts[path].append(ast.get_source_segment(source, node))
    result = {path: "\n\n".join(parts[path]) + "\n" for path in FILES}
    compile_bundle(result, helper_names)
    return result


def _target(root, name):
    path = root/name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("snapshot path escaped workspace")
    for part in [root, *[root.joinpath(*Path(name).parts[:i]) for i in range(1,len(Path(name).parts)+1)]]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
            raise ValueError("snapshot cannot follow links or junctions")
    return path


def snapshot(root: Path, files: dict[str, str]) -> None:
    """Synchronize only this scratch snapshot's recorded modules; no recursive removal."""
    validate_layout(files)
    manifest = _target(root,'.modules.json')
    previous = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else [p for p in FILES if (root/p).exists()]
    if not isinstance(previous,list):
        raise ValueError("invalid snapshot manifest")
    for name in previous:
        validate_module_path(name)
    # Resolve/check all final targets before writing or removing anything.
    targets = {name:_target(root,name) for name in set(previous)|set(files)}
    root.mkdir(parents=True,exist_ok=True)
    for name in module_order(files):
        path = targets[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(files[name], encoding="utf-8")
    for name in set(previous)-set(files):
        targets[name].unlink(missing_ok=True)
    from rsi.code_store import write_json
    write_json(manifest,module_order(files))
