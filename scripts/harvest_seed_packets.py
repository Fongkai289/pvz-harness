"""Labeled seed-packet crops from Portable renders (the real game's art), for live card recognition.

Portable knows every slot's plant, so each rendered frame yields correctly labeled crops: ready,
dimmed (unaffordable) and recharging cards, plus empty slots. Output goes to data/seed_packets_raw/
(labels.json + crops); scripts/build_seed_templates.py turns it into knowledge/seed_packets/.

    .venv/Scripts/python.exe scripts/harvest_seed_packets.py
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rsi.portable_runtime import PortableArena, NAMES  # noqa: E402
from server import geometry  # noqa: E402

LEVELS = (1, 2, 3, 4, 6, 7, 8, 9)  # 1-5 and 1-10 are conveyor-belt levels: no fixed seed bank
SEEDS = (101, 202)
MAX_SLOTS = 8


def main():
    config = json.loads((ROOT / 'data/portable_rsi/config.json').read_text(encoding='utf-8-sig'))
    out = ROOT / 'data/seed_packets_raw'
    out.mkdir(parents=True, exist_ok=True)
    labels = []
    arena = PortableArena(config, 'pixels')
    try:
        for level in LEVELS:
            for seed in SEEDS:
                info = arena.reset(level, seed)
                for step in range(0, 241):  # 25 ticks per step: one minute of game time
                    if step % 16 == 0:
                        frame = cv2.imdecode(np.frombuffer(arena.pixels(), np.uint8), cv2.IMREAD_COLOR)
                        cards = info['raw_observation']['cards']
                        for slot in range(MAX_SLOTS):
                            x0, y0, x1, y1 = geometry.seed_card_rect(slot)
                            if x1 > frame.shape[1]:
                                break
                            card = next((c for c in cards if c['slot'] == slot), None)
                            plant = NAMES[card['plant_type']] if card else 'empty'
                            name = f'{level}-{seed}-{step:03d}-{slot}.png'
                            cv2.imwrite(str(out / name), frame[y0:y1, x0:x1])
                            labels.append(dict(file=name, level=level, seed=seed, step=step, slot=slot, plant=plant,
                                               ready=bool(card and card['ready'])))
                    placement = None
                    if step == 40:  # one planting puts a card into recharge for the later frames
                        mask = info['action_mask']
                        allowed = [i for i in range(1, len(mask)) if mask[i]]
                        if allowed:
                            i = allowed[len(allowed) // 2] - 1
                            placement = dict(slot=i // 54, row=(i % 54) // 9, col=i % 9)
                    terminal, truncated, info = arena.step(placement)
                    if terminal:
                        break
                print(level, seed, len(labels), flush=True)
    finally:
        arena.close()
    (out / 'labels.json').write_text(json.dumps(labels, indent=1), encoding='utf-8')
    counts = {}
    for row in labels:
        counts[row['plant']] = counts.get(row['plant'], 0) + 1
    print(json.dumps(counts, indent=1))


if __name__ == '__main__':
    main()
