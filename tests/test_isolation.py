"""The player side must never see ground truth: it may not import the judge or StateMsg."""

import ast
from pathlib import Path

PLAYER_MODULES = ["server/perception.py", "server/policy.py", "server/planner.py", "server/decider.py",
                  "server/scene.py"]
FORBIDDEN = {"server.judge", "StateMsg"}
ROOT = Path(__file__).resolve().parent.parent


def imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
    return names


def test_player_side_does_not_import_ground_truth():
    for rel in PLAYER_MODULES:
        leaked = imported_names(ROOT / rel) & FORBIDDEN
        assert not leaked, f"{rel} imports {leaked}"
