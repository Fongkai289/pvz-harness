"""Versioned playbooks on disk.

    data/playbooks/vNNN.json   one file per version, never edited after it is written
    data/CURRENT               the promoted version
    data/ledger.jsonl          every candidate: what changed, scores, promoted or not, why

data/ lives only on the server and survives deploys. The first load seeds it from playbook_seed/.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from rules.playbook_rules import Playbook

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "playbook_seed"


class PlaybookStore:
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "playbooks"
        self.current_file = data_dir / "CURRENT"
        self.ledger_file = data_dir / "ledger.jsonl"
        self.dir.mkdir(parents=True, exist_ok=True)
        if not any(self.dir.glob("v*.json")):
            for seed in SEED_DIR.glob("v*.json"):
                shutil.copy(seed, self.dir / seed.name)
        if not self.current_file.exists():
            self.current_file.write_text("v001\n")

    def current(self) -> str:
        return self.current_file.read_text().strip()

    def set_current(self, version: str) -> None:
        self.load(version)  # refuse to point CURRENT at something invalid
        self.current_file.write_text(version + "\n")

    def load(self, version: str | None = None) -> Playbook:
        version = version or self.current()
        path = self.dir / f"{version}.json"
        if not path.exists():
            raise FileNotFoundError(f"no playbook {version}")
        return Playbook.model_validate_json(path.read_text())

    def versions(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("v*.json"))

    def next_version(self) -> str:
        return f"v{int(self.versions()[-1][1:]) + 1:03d}"

    def write(self, playbook: Playbook) -> None:
        path = self.dir / f"{playbook.version}.json"
        if path.exists():
            raise FileExistsError(f"{playbook.version} already exists; versions are immutable")
        path.write_text(playbook.model_dump_json(indent=2) + "\n")

    def log(self, **entry: Any) -> None:
        with self.ledger_file.open("a") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}, default=str) + "\n")

    def ledger(self) -> list[dict[str, Any]]:
        if not self.ledger_file.exists():
            return []
        return [json.loads(line) for line in self.ledger_file.read_text().splitlines() if line]
