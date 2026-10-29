"""A person edits the playbook: new version from the current one, validated and logged.

    uv run python -m rsi.human_edit --planner-knowledge knowledge/pvz_strategy.md --note "seed guides"

Human edits go through the same fixed rules as coach edits and are recorded in the ledger
as kind="human_edit", so every later score can be traced back to who changed what.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from rules.playbook_rules import PlannerKnowledge
from server.config import load_settings
from server.playbook import PlaybookStore

BLOCK = re.compile(r"<!-- PLANNER KNOWLEDGE START -->\n(.*?)<!-- PLANNER KNOWLEDGE END -->", re.S)


def planner_block(path: Path) -> str:
    match = BLOCK.search(path.read_text())
    if not match:
        raise SystemExit(f"{path}: no PLANNER KNOWLEDGE block")
    return match.group(1).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--planner-knowledge", type=Path, required=True)
    parser.add_argument("--note", required=True)
    parser.add_argument("--no-promote", action="store_true", help="write the version but keep CURRENT")
    args = parser.parse_args()

    store = PlaybookStore(load_settings().data_dir)
    current = store.load()
    knowledge = PlannerKnowledge(knowledge=planner_block(args.planner_knowledge))
    new = current.model_copy(update={"version": store.next_version(), "parent": current.version,
                                     "planner": knowledge, "notes": f"human edit: {args.note}"})
    new = type(new).model_validate(new.model_dump())  # re-run the fixed rules on the result
    store.write(new)
    if not args.no_promote:
        store.set_current(new.version)
    store.log(kind="human_edit", parent=current.version, version=new.version, promoted=not args.no_promote,
              changes={"planner.knowledge": [len(current.planner.knowledge), len(knowledge.knowledge)]},
              source=str(args.planner_knowledge), note=args.note)
    print(f"{current.version} -> {new.version} ({len(knowledge.knowledge)} chars of planner knowledge); "
          f"CURRENT = {store.current()}")


if __name__ == "__main__":
    main()
