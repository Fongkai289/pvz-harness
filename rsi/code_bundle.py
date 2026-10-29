"""Editable player modules compiled together under the existing restricted runtime.

Paths are capability names, never model-selected paths on the host filesystem.
The serialized source retains module boundaries through CURRENT/promotions.
"""
from __future__ import annotations

import ast
from pathlib import Path

from rsi.code_runtime import validate_source
from rsi.pixel_tools import HELPERS

FILES = (
    "player/perception.py", "player/state.py", "player/actions.py",
    "player/policy.py", "player/tools.py",
)
HEADER = "# pvz-player-bundle-v1\n"
MARKER = "# file: "


def compile_bundle(files: dict[str, str]) -> str:
    if set(files) != set(FILES):
        raise ValueError("only the five player modules are editable")
    for path, source in files.items():
        if not isinstance(source, str) or len(source.encode("utf-8")) > 24000:
            raise ValueError(f"module too large: {path}")
        if any(line.startswith(MARKER) or line == HEADER.strip() for line in source.splitlines()):
            raise ValueError("reserved bundle marker in module")
    result = HEADER + "".join(f"{MARKER}{path}\n{files[path].rstrip()}\n\n" for path in FILES)
    if len(result) > 24000:
        raise ValueError("combined player source exceeds 24000 characters")
    validate_source(result, HELPERS)
    return result


def unpack_source(source: str) -> dict[str, str]:
    if source.startswith(HEADER):
        files = {}
        current = None
        lines = []
        for line in source[len(HEADER):].splitlines(keepends=True):
            if line.startswith(MARKER):
                if current is not None:
                    files[current] = "".join(lines).rstrip() + "\n"
                current = line[len(MARKER):].strip()
                if current not in FILES or current in files:
                    raise ValueError("invalid or repeated player module")
                lines = []
            else:
                if current is None:
                    raise ValueError("bundle content outside module")
                lines.append(line)
        if current is not None:
            files[current] = "".join(lines).rstrip() + "\n"
        compile_bundle(files)
        return files
    # Migrate legacy candidates mechanically; do not change their decisions.
    validate_source(source, HELPERS)
    parts = {path: [] for path in FILES}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef):
            path = {"perceive": FILES[0], "decide": FILES[3],
                    "free": FILES[2], "ready": FILES[2], "place": FILES[2]}.get(node.name, FILES[4])
            parts[path].append(ast.get_source_segment(source, node))
    result = {path: "\n\n".join(parts[path]) + "\n" for path in FILES}
    compile_bundle(result)
    return result


def snapshot(root: Path, files: dict[str, str]) -> None:
    """Only callers' fixed module names reach disk; validation can follow edits."""
    if set(files) != set(FILES):
        raise ValueError("invalid player module set")
    for name in FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(files[name], encoding="utf-8")
