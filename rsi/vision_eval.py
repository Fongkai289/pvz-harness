"""Score the look step on labelled screenshots (eval/vision_labels.json, images in data/vision_eval/).

    uv run python -m rsi.vision_eval [--repeats 2]

Reports, per image and in total: sun read exactly, lawn type, card readiness (code), zombie types
(recall = labelled types found, invented = types reported that are not there), and latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path
from typing import Any

import cv2

from server.config import ROOT, load_settings
from server.scene import QwenLooker, Scene, read_cards


def score_scene(scene: Scene, label: dict[str, Any]) -> dict[str, Any]:
    seen = {z.type for z in scene.zombies} - {"unknown"}
    truth = set(label["zombie_types"])
    scored = [c for c in scene.cards if c.slot not in label["occluded"]]
    return {
        "sun_ok": scene.sun == label["sun"],
        "lawn_ok": scene.lawn == label["lawn"],
        "cards_ok": sum(c.ready == (c.slot not in label["greyed"]) for c in scored),
        "cards_scored": len(scored),
        "types_found": len(seen & truth),
        "types_total": len(truth),
        "types_invented": sorted(seen - truth),
        "types_missed": sorted(truth - seen),
    }


def score_plants(seen: dict[tuple[int, int], list[str]], label: dict[str, str]) -> dict[str, int]:
    truth = {tuple(int(x) for x in cell.split(",")): plant for cell, plant in label.items()}
    found = sum(cell in seen for cell in truth)
    right = sum(cell in seen and plant in seen[cell] for cell, plant in truth.items())
    invented = sum(cell not in truth for cell in seen)
    return {"cells_found": found, "types_right": right, "cells_total": len(truth), "invented_cells": invented}


def load_image(path: Path):
    image = cv2.imread(str(path))
    return cv2.resize(image, (800, 600), interpolation=cv2.INTER_AREA)  # what the game window shows


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    s = load_settings()
    labels = json.loads((ROOT / "eval" / "vision_labels.json").read_text())["images"]
    looker = QwenLooker(s.qwen_url, s.qwen_model, timeout=s.planner_timeout)
    totals = {"sun_ok": 0, "lawn_ok": 0, "cards_ok": 0, "cards_scored": 0, "types_found": 0,
              "types_total": 0, "invented": 0, "runs": 0, "errors": 0}
    latencies = []
    for label in labels:
        image = load_image(s.data_dir / "vision_eval" / label["file"])
        loadout = ["?"] * label["slots"]  # plant names are not scored
        cards = read_cards(image, loadout)  # code part: same answer every run
        for k in range(args.repeats):
            result = await looker.look(image, frame_id=k, loadout=loadout)
            latencies.append(result.latency)
            totals["runs"] += 1
            if result.scene is None:
                totals["errors"] += 1
                print(f"{label['level']} run{k}: ERROR {result.error}")
                continue
            sc = score_scene(result.scene, label)
            for key in ("sun_ok", "lawn_ok", "cards_ok", "cards_scored", "types_found", "types_total"):
                totals[key] += sc[key]
            totals["invented"] += len(sc["types_invented"])
            print(f"{label['level']} run{k}: {result.latency:.1f}s out={result.output_tokens} sun={result.scene.sun} "
                  f"lawn={result.scene.lawn} cards {sc['cards_ok']}/{sc['cards_scored']} "
                  f"types {sc['types_found']}/{sc['types_total']} missed={sc['types_missed']} "
                  f"invented={sc['types_invented']}")
        print(f"   card brightness: {[c.brightness for c in cards]}")
        if "plants" in label:
            for k in range(args.repeats):
                seen = await looker.look_plants(image, label["loadout"])
                if seen.plants is None:
                    print(f"   plants run{k}: ERROR {seen.error}")
                    continue
                sp = score_plants(seen.plants, label["plants"])
                print(f"   plants run{k}: {seen.latency:.1f}s cells {sp['cells_found']}/{sp['cells_total']} "
                      f"types {sp['types_right']}/{sp['cells_total']} invented cells {sp['invented_cells']}")
    ok = totals["runs"] - totals["errors"]
    print(f"\nTOTAL over {totals['runs']} runs ({totals['errors']} errors): sun {totals['sun_ok']}/{ok}, "
          f"lawn {totals['lawn_ok']}/{ok}, cards {totals['cards_ok']}/{totals['cards_scored']}, "
          f"zombie types recall {totals['types_found']}/{totals['types_total']}, invented {totals['invented']}, "
          f"latency median {statistics.median(latencies):.1f}s max {max(latencies):.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
