"""Which playbook version each Adventure level plays (data_dir/levels.json).

Levels differ (cards, conveyor belts, waves), so training keeps one playbook per level: a level
with no entry plays CURRENT. Versions themselves stay immutable in playbooks/. Live sessions read
the level number the game shows (also what a human player sees) only to choose the playbook.
"""
import json
import os
from pathlib import Path

from common.file_lock import file_lock


class LevelBooks:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "levels.json"

    def all(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def get(self, level) -> str | None:
        return self.all().get(str(level))

    def version(self, store, level) -> str:
        """The version this level plays: its own entry, else CURRENT."""
        return (self.get(level) if level is not None else None) or store.current()

    def set(self, store, level, version, expected=None):
        """Point a level at a stored version; with expected, only if it still plays that version."""
        store.load(version)  # must exist and validate
        with file_lock(self.path.with_suffix(".lock")):
            books = self.all()
            if expected is not None and books.get(str(level), store.current()) != expected:
                raise ValueError(f"level {level} changed during evaluation; refusing stale update")
            books[str(level)] = version
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(dict(sorted(books.items(), key=lambda kv: int(kv[0]))), indent=2) + "\n",
                                 encoding="utf-8")
            os.replace(temporary, self.path)
