"""Immutable source candidates and a simulation-only CURRENT pointer.

This store deliberately cannot update data/CURRENT (the live game's playbook).
Promotion in a procedural simulator is not evidence of real-screen accuracy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

DOMAIN = "pvzemu-symbolic-v1"


def digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def write_json(path: Path, value: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class CodeStore:
    def __init__(self, root: Path):
        self.root = root.resolve() / DOMAIN
        self.candidates = self.root / "candidates"
        self.candidates.mkdir(parents=True, exist_ok=True)
        self.current_file = self.root / "CURRENT"
        self.ledger_file = self.root / "ledger.jsonl"

    def add(self, source: str, *, parent: str | None, rationale: str, origin: str) -> str:
        version = digest(source)
        target = self.candidates / f"{version}.py"
        if target.exists():
            if target.read_text(encoding="utf-8") != source:
                raise ValueError("candidate hash collision")
            return version
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(source)
        write_json(target.with_suffix(".json"), {"version": version, "parent": parent,
                   "rationale": rationale, "origin": origin, "domain": DOMAIN, "created": time.time()})
        return version

    def source(self, version: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", version):
            raise ValueError("invalid version id")
        source = (self.candidates / f"{version}.py").read_text(encoding="utf-8")
        if digest(source) != version:
            raise ValueError("candidate was modified after creation")
        return source

    def current(self) -> str | None:
        if not self.current_file.exists():
            return None
        version = self.current_file.read_text(encoding="utf-8").strip()
        self.source(version)
        return version

    def initialize(self, source: str) -> str:
        if self.current() is not None:
            return self.current()
        version = self.add(source, parent=None, rationale="initial hand-written policy", origin="seed")
        self._point(version)
        self.log(kind="initialized", version=version)
        return version

    def promote(self, version: str, parent: str, evidence: list[dict]) -> None:
        self.source(version)
        if self.current() != parent:
            raise RuntimeError("current version changed during evaluation")
        if [r.get("stage") for r in evidence] != ["development", "confirmation", "audit"]:
            raise ValueError("all three promotion stages are required")
        if not all(r.get("passed") is True for r in evidence):
            raise ValueError("candidate did not pass every stage")
        self.log(kind="promotion_prepared", parent=parent, version=version, evidence=evidence)
        self._point(version)
        try:
            self.log(kind="promoted", parent=parent, version=version, evidence=evidence)
        except BaseException:
            self._point(parent)
            raise

    def _point(self, version: str) -> None:
        temporary = self.current_file.with_suffix(".tmp")
        temporary.write_text(version + "\n", encoding="utf-8")
        os.replace(temporary, self.current_file)

    def log(self, **entry) -> None:
        with self.ledger_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": time.time(), **entry}, ensure_ascii=False, allow_nan=False) + "\n")

    def coach_history(self) -> list[dict]:
        """Only development feedback; never copy confirmation/audit scores into prompts."""
        if not self.ledger_file.exists():
            return []
        entries = [json.loads(line) for line in self.ledger_file.read_text(encoding="utf-8").splitlines() if line]
        return [{k: row[k] for k in ("version", "rationale", "development", "error") if k in row}
                for row in entries if row["kind"] in {"proposal", "development", "invalid"}][-8:]
