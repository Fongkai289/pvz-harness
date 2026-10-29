"""Explicit controls for the latest code-learning run. Never auto-resumes it."""

import argparse
import json
from pathlib import Path

from rsi.code_store import DOMAIN


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["status", "pause", "resume", "stop"])
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "code_rsi")
    args = parser.parse_args()
    root = args.data_dir.resolve() / DOMAIN
    latest = json.loads((root / "LATEST_RUN.json").read_text(encoding="utf-8"))
    run = Path(latest["path"]).resolve()
    if not run.is_relative_to(root / "runs"):
        raise ValueError("latest run is outside the learning directory")
    if args.action == "pause":
        (run / "PAUSE").touch()
    elif args.action == "resume":
        (run / "PAUSE").unlink(missing_ok=True)
    elif args.action == "stop":
        (run / "STOP").touch()
    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
    status.update(pause_requested=(run / "PAUSE").exists(), stop_requested=(run / "STOP").exists())
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
