"""Seed selection screen: code reads which plants can be taken and where, Qwen picks them.

Run only when the operator presses 选植物 in the control center while the game shows the seed
chooser; it is not part of a game session. The operator still starts the level ("Let's Rock").

Geometry is SeedChooserScreen::GetSeedPositionInChooser (PvZ-Portable), checked on a live
1.2.0.1096 chooser frame (2026-11-03): chooser index = SeedType, 8 per row, 50x70 packets at
x = 22 + 53*col, y = 128 + 73*row (saves with 7 chooser rows use 70; not supported yet).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import cv2
import numpy as np

from rules.hard_rules import PLANT_COSTS
from server import geometry
from server.scene import _b64
from server.seed_packets import identify

SEED_TYPES = (
    "peashooter", "sunflower", "cherry bomb", "wall-nut", "potato mine", "snow pea", "chomper", "repeater",
    "puff-shroom", "sun-shroom", "fume-shroom", "grave buster", "hypno-shroom", "scaredy-shroom", "ice-shroom",
    "doom-shroom", "lily pad", "squash", "threepeater", "tangle kelp", "jalapeno", "spikeweed", "torchwood",
    "tall-nut", "sea-shroom", "plantern", "cactus", "blover", "split pea", "starfruit", "pumpkin",
    "magnet-shroom", "cabbage-pult", "flower pot", "kernel-pult", "coffee bean", "garlic", "umbrella leaf",
    "marigold", "melon-pult", "gatling pea", "twin sunflower", "gloom-shroom", "cattail", "winter melon",
    "gold magnet", "spikerock", "cob cannon")
PACKET_W, PACKET_H = 50, 70
X0, DX, Y0, DY = 22, 53, 128, 73
MIN_CARD_VALUE = 110  # mean brightness: a packet is ~150-175, an empty or locked slot ~51
VISIBLE = 40  # five rows show above the "Let's Rock" button (154,545,156,42); upgrades need the 7-row layout


def packet_rect(index: int) -> tuple[int, int, int, int]:
    row, col = divmod(index, 8)
    x0, y0 = X0 + DX * col, Y0 + DY * row
    return x0, y0, x0 + PACKET_W, y0 + PACKET_H


def packet_center(index: int) -> tuple[int, int]:
    x0, y0, x1, y1 = packet_rect(index)
    return (x0 + x1) // 2, (y0 + y1) // 2


def _empty_bank_slot(image: np.ndarray, slot: int) -> bool:
    x0, y0, x1, y1 = geometry.seed_card_rect(slot)
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    # Live chooser: an empty slot is brown recess, hue 10 / sat 200 / value 118; a card has value ~195.
    return (5 <= hsv[..., 0].mean() <= 15 and hsv[..., 1].mean() > 150 and hsv[..., 2].mean() < 150
            and (hsv[..., 2] > 200).mean() < 0.05)


@dataclass
class Chooser:
    slots: int  # empty seed bank slots to fill
    available: list[str]  # plants shown in the chooser, chooser order


def read_chooser(image: np.ndarray) -> Chooser:
    """What the chooser offers; ValueError (operator-readable) when this is not a fresh chooser."""
    if image.shape[:2] != (geometry.HEIGHT, geometry.WIDTH):
        raise ValueError("截图尺寸不对")
    x0, y0, x1, y1 = packet_rect(0)
    if identify(image[y0:y1, x0:x1])[0] != "peashooter":  # always unlocked, always first
        raise ValueError("现在不是选卡界面，或卡已经选过（豌豆射手不在第一格）")
    if not _empty_bank_slot(image, 0):
        raise ValueError("卡槽里已经有卡；请先把卡槽清空")
    slots = 0
    while slots < 10 and _empty_bank_slot(image, slots):
        slots += 1
    available = []
    for index, name in enumerate(SEED_TYPES[:VISIBLE]):
        x0, y0, x1, y1 = packet_rect(index)
        if image[y0:y1, x0:x1].mean() >= MIN_CARD_VALUE:
            available.append(name)
    return Chooser(slots, available)


GOAL = ("Bring an economy plant, enough attackers for every row, and answers to the toughest previewed "
        "zombies. Reason in one or two short sentences.\nStrategy notes:\n")


def _offer(available: list[str]) -> str:
    return ", ".join(f"{name} ({PLANT_COSTS[name]} sun)" if name in PLANT_COSTS else name for name in available)


def prompt(chooser: Chooser, count: int, knowledge: str) -> str:
    return (
        "This is the seed selection screen of Plants vs. Zombies (PC). The zombies standing on the street at "
        "the right are a preview of the zombie types in this level. First list the zombie types you see, then "
        f"choose exactly {count} different plants to bring, from: {_offer(chooser.available)}. " + GOAL
        + knowledge[:3000])


def text_prompt(level: int, preview: list[str], available: list[str], count: int, knowledge: str) -> str:
    """Training (Portable) has no chooser picture: the same public facts as words."""
    return (
        f"Plants vs. Zombies (PC), adventure level 1-{level}, seed selection. The level's preview shows these "
        f"zombie types: {', '.join(preview) or 'none'}. Repeat them as the zombies list, then choose exactly "
        f"{count} different plants to bring, from: {_offer(available)}. " + GOAL + knowledge[:3000])


async def _ask(looker, content: list, available: list[str], count: int) -> dict:
    """Qwen's pick, validated: exactly count distinct plants from available."""
    if count == 0:
        raise ValueError("没有可选的植物或空卡槽")
    schema = {"type": "object", "required": ["zombies", "plants", "reason"], "properties": {
        "zombies": {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 40}},
        # No uniqueItems: the vLLM grammar rejects it (400, 2026-11-03); distinctness is checked below.
        "plants": {"type": "array", "minItems": count, "maxItems": count,
                   "items": {"type": "string", "enum": available}},
        "reason": {"type": "string", "maxLength": 300}}}
    body = {"model": looker.model, "max_tokens": 500, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "seed_choice", "schema": schema}}}
    started = time.monotonic()
    response = await looker.client.post(looker.url, json=body)
    if response.status_code >= 400:
        raise RuntimeError(f"Qwen HTTP {response.status_code}: {response.text[:200]}")
    data = json.loads(response.json()["choices"][0]["message"]["content"])
    plants = data["plants"]
    if (type(plants) is not list or len(plants) != count or len(set(plants)) != count
            or not set(plants) <= set(available)):
        raise RuntimeError(f"Qwen 的选择不合法：{plants!r}")
    return {"plants": plants, "zombies": [str(z)[:40] for z in data.get("zombies", [])][:12],
            "reason": str(data.get("reason", ""))[:300], "latency_s": round(time.monotonic() - started, 2)}


async def choose(looker, image: np.ndarray, chooser: Chooser, knowledge: str) -> dict:
    """Live: Qwen reads the chooser screenshot; the result says which packets to click."""
    count = min(chooser.slots, len(chooser.available))
    result = await _ask(looker, [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(image)}"}},
        {"type": "text", "text": prompt(chooser, count, knowledge)}], chooser.available, count)
    result["clicks"] = [dict(zip(("x", "y"), packet_center(SEED_TYPES.index(p))), plant=p) for p in result["plants"]]
    return result


async def choose_text(looker, level: int, preview: list[str], available: list[str], slots: int,
                      knowledge: str) -> dict:
    """Training: the same choice from the level's public preview and offer, as text."""
    count = min(slots, len(available))
    return await _ask(looker, [{"type": "text", "text": text_prompt(level, preview, available, count, knowledge)}],
                      available, count)
