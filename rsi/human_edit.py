"""A person edits the playbook: new version from the current one, validated and logged.

    uv run python -m rsi.human_edit --planner-knowledge knowledge/pvz_strategy.md --note "seed guides"
    uv run python -m rsi.human_edit --set perception.sun_hsv_lo=[24,120,180] --set jev.economy_first=5 \
        --note "real run fixes"

Human edits go through the same fixed rules as coach edits and are recorded in the ledger
as kind="human_edit", so every later score can be traced back to who changed what.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from rules.playbook_rules import EDITABLE_SECTIONS, Playbook
from server.config import load_settings
from server.playbook import PlaybookStore

BLOCK = re.compile(r"<!-- PLANNER KNOWLEDGE START -->\n(.*?)<!-- PLANNER KNOWLEDGE END -->", re.S)


def planner_block(path: Path) -> str:
    match = BLOCK.search(path.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"{path}: no PLANNER KNOWLEDGE block")
    return match.group(1).strip()


def edit(current: Playbook, version: str, note: str, knowledge: str | None = None,
         sets: list[str] = ()) -> tuple[Playbook, dict[str, Any]]:
    """The new version and its changes as {field: [old, new]}. `sets` items look like
    "section.field=<JSON>" for an editable section; the result passes the fixed rules or raises."""
    data = current.model_dump()
    changes: dict[str, Any] = {}
    if knowledge is not None:
        changes["planner.knowledge"] = [len(data["planner"]["knowledge"]), len(knowledge)]
        data["planner"]["knowledge"] = knowledge
    for item in sets:
        key, sep, raw = item.partition("=")
        section, _, name = key.partition(".")
        if not sep or section not in EDITABLE_SECTIONS or not name:
            raise ValueError(f"--set wants section.field=<JSON> with a section in {EDITABLE_SECTIONS}: {item!r}")
        value = json.loads(raw)
        changes[key] = [data[section].get(name), value]
        data[section][name] = value
    data.update(version=version, parent=current.version, notes=f"human edit: {note}")
    return Playbook.model_validate(data), changes  # re-runs the fixed rules on the result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--planner-knowledge", type=Path)
    parser.add_argument("--set", action="append", default=[], metavar="SECTION.FIELD=JSON",
                        help="change one field, e.g. jev.economy_first=5; repeatable")
    parser.add_argument("--note", required=True)
    parser.add_argument("--no-promote", action="store_true", help="write the version but keep CURRENT")
    args = parser.parse_args()
    if args.planner_knowledge is None and not args.set:
        parser.error("nothing to change: give --planner-knowledge and/or --set")

    store = PlaybookStore(load_settings().data_dir)
    current = store.load()
    knowledge = planner_block(args.planner_knowledge) if args.planner_knowledge else None
    new, changes = edit(current, store.next_version(), args.note, knowledge, args.set)
    store.write(new)
    if not args.no_promote:
        store.set_current(new.version)
    store.log(kind="human_edit", parent=current.version, version=new.version, promoted=not args.no_promote,
              changes=changes, source=str(args.planner_knowledge) if args.planner_knowledge else "--set",
              note=args.note)
    print(f"{current.version} -> {new.version}: {json.dumps(changes)}; CURRENT = {store.current()}")


if __name__ == "__main__":
    main()
