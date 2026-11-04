"""knowledge/seed_packets.npz from the harvested crops (scripts/harvest_seed_packets.py).

Near-duplicate crops of the same plant are dropped so the file stays small; every distinct look
(ready, dimmed, recharging, empty slot, shovel box) keeps at least one example.

    .venv/Scripts/python.exe scripts/build_seed_templates.py
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.seed_packets import PATH, feature  # noqa: E402

DUPLICATE = 0.985


def main():
    raw = ROOT / "data/seed_packets_raw"
    labels = json.loads((raw / "labels.json").read_text(encoding="utf-8"))
    kept: dict[str, list] = {}
    crops, names = [], []
    for row in labels:
        crop = cv2.imread(str(raw / row["file"]))
        vector = feature(crop)
        same = kept.setdefault(row["plant"], [])
        if any(float(vector @ other) >= DUPLICATE for other in same):
            continue
        same.append(vector)
        crops.append(crop)
        names.append(row["plant"])
    PATH.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(PATH, crops=np.stack(crops), labels=np.array(names))
    print({name: len(v) for name, v in kept.items()}, f"{PATH.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
