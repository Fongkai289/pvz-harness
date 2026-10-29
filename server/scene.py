"""The "look" step: screenshot -> structured scene, kept separate from deciding.

With game state, seeing is free and exact. From pixels it is its own problem, so it is its own
step with its own output, which the judge can check against ground truth:

- code reads what can be measured exactly: whether each seed card is usable (brightness) and the
  lawn type (water / darkness / green), which also fixes the number of rows; on day lawns also
  where the zombies stand (row and front column), see measure_zombies
- the vision model reads what needs understanding: the sun number (from an enlarged crop of the
  top bar) and zombie types per row, from a named list with appearance hints, "unknown" allowed

Must never import server.judge or read StateMsg (tests/test_isolation.py).
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Literal

import cv2
import httpx
import numpy as np
from pydantic import BaseModel, Field, ValidationError

from server import geometry

log = logging.getLogger(__name__)

ZombieType = Literal[
    "normal", "flag", "conehead", "buckethead", "pole vaulting", "newspaper", "screen door", "football",
    "dancing", "backup dancer", "ducky tube", "snorkel", "zomboni", "bobsled", "dolphin rider",
    "jack-in-the-box", "balloon", "digger", "pogo", "bungee", "ladder", "catapult", "gargantuar", "imp",
    "unknown",
]
CARD_READY_MIN_V = 130  # mean HSV value of the card picture; measured: usable 169-222, greyed 47-95

# Shown to the model in the prompt. A type list that exists only in the answer schema constrains the
# words but not their meaning: on 2026-10-28 that gave 11/38 recall and 17 invented types.
ZOMBIE_LOOKS = {
    "normal": "plain zombie in a brown suit and tie",
    "flag": "carries a red flag with a brain on it",
    "conehead": "orange traffic cone on its head",
    "buckethead": "metal bucket on its head",
    "pole vaulting": "carries a long pole; red and white tank top",
    "newspaper": "reading a newspaper, glasses",
    "screen door": "holds a metal screen door in front of it",
    "football": "red football helmet and shoulder pads",
    "dancing": "disco outfit, afro hair",
    "ducky tube": "in the water inside a yellow duck-shaped float",
    "snorkel": "diving mask and snorkel, in the water",
    "zomboni": "drives an ice-resurfacing machine marked Z",
    "dolphin rider": "rides a dolphin in the water",
    "balloon": "floats under a balloon",
    "pogo": "bounces on a pogo stick",
    "gargantuar": "giant zombie carrying a pole or sign",
}


class Zombie(BaseModel):
    type: ZombieType
    row: int = Field(ge=0, le=5, description="0 = top row")
    col: int | None = Field(default=None, ge=0, le=9, description="0 = left (house side), 9 = just off the lawn")


class RowReport(BaseModel):
    zombies: list[ZombieType] = Field(description="types of the zombies seen in this row")
    nearest_col: int | None = Field(description="column of the zombie closest to the house, null if none")


class VisionReport(BaseModel):
    """What the vision model is asked for. Short on purpose: answer length is most of the latency."""

    sun: int | None = Field(description="the number under the sun icon, null if unreadable")
    rows: list[RowReport] = Field(description="one entry per lawn row, top row first")
    other: str = Field(default="", max_length=150, description="anything unusual, a few words")


def classify_lawn(image: np.ndarray) -> str:
    """Lawn type from colour, measured on 2026-10-28 screenshots: pool rows are ~41% bright blue water
    (0% elsewhere); night lawns are dark (mean V 93 vs 160-182) with almost no green. Fog and roof rules
    are not yet checked on real images."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    band = hsv[250:380, 60:620]
    water = ((band[:, :, 0] > 85) & (band[:, :, 0] < 110) & (band[:, :, 1] > 80) & (band[:, :, 2] > 150)).mean()
    lawn = hsv[100:580, 60:620]
    bright = lawn[:, :, 2].mean()
    green = ((lawn[:, :, 0] > 35) & (lawn[:, :, 0] < 85) & (lawn[:, :, 1] > 80)).mean()
    if water > 0.2:
        return "pool" if bright > 130 else "fog"
    if bright < 120:
        return "night"
    return "day" if green > 0.5 else "roof"


class Card(BaseModel):
    slot: int
    plant: str  # known from the agent's own seed selection, not read from the picture
    ready: bool  # bright card = can be bought now (affordable and not recharging)
    brightness: float


class Scene(BaseModel):
    frame_id: int
    t_capture: float = 0.0
    lawn: str | None
    rows: int | None
    sun: int | None
    cards: list[Card]
    zombies: list[Zombie]
    nearest_col: dict[int, int] = {}  # row -> column of its zombie closest to the house
    positions: str = "model"  # who placed the zombies: "model" (rows as the model said) or "code"
    other: str = ""

    def describe(self) -> str:
        """Compact text for the decision step and for OneJev."""
        cards = ", ".join(f"{c.slot} {c.plant} ({'usable' if c.ready else 'not usable'})" for c in self.cards)
        by_row: dict[int, list[str]] = {}
        for z in self.zombies:
            by_row.setdefault(z.row, []).append(z.type + (f"@col{z.col}" if z.col is not None else ""))
        rows = "; ".join(
            f"row {r}: {', '.join(v)}" + (f" (nearest at col {self.nearest_col[r]})" if r in self.nearest_col else "")
            for r, v in sorted(by_row.items())) or "none seen"
        return (f"Lawn: {self.lawn or 'unknown'} ({self.rows or '?'} rows). Sun: "
                f"{self.sun if self.sun is not None else 'unknown'}. Seed slots: {cards or 'none'}. "
                f"Zombies: {rows}." + (f" Other: {self.other}" if self.other else ""))

    def most_threatened_row(self) -> tuple[int, int] | None:
        """(row, column) of the zombie closest to the house, if any zombie was seen."""
        if not self.nearest_col:
            return None
        row = min(self.nearest_col, key=lambda r: self.nearest_col[r])
        return row, self.nearest_col[row]


def read_cards(image: np.ndarray, loadout: list[str]) -> list[Card]:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    cards = []
    for slot, plant in enumerate(loadout):
        x0, _, x1, _ = geometry.seed_card_rect(slot)
        v = float(hsv[geometry.SEED_PICTURE_Y0:geometry.SEED_PICTURE_Y1, x0 + 4:x1 - 4, 2].mean())
        cards.append(Card(slot=slot, plant=plant, ready=v >= CARD_READY_MIN_V, brightness=round(v, 1)))
    return cards


ZOMBIE_MIN_AREA = 400  # px of grey per zombie; potato mine stalks and pea splats stay below
ZOMBIE_MAX_W = 140  # wider grey areas are dialogs or the stone edge, not a zombie
STACK_GAP = 30  # px between head+shirt and legs that still count as one zombie


def measure_zombies(image: np.ndarray, n_rows: int) -> list[tuple[int, int]]:
    """(row, front column) of each zombie on a day lawn, nearest the house first.

    On the real day lawn the model put zombies one or more rows off (2026-10-29 run: rows 1 and 2
    read as 3 and 4); columns it mostly got right. Code measures instead: zombie skin and shirt are
    the only large unsaturated grey areas on day grass (plants, suns, cards and mowers are
    saturated). That grey part is head + shirt; its centre stands ~85 px above the feet, and feet
    stand ~40 px above the bottom of their row, which leaves ~35 px of margin each way. Checked on
    16 frames of that run (docs/env.md). Zombies dimmed by a tutorial banner or still off the lawn
    are missed; look() keeps the model's reading for rows code saw nothing in.
    """
    y0, h = geometry.LAWNS[n_rows]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = ((hsv[..., 1] < 70) & (hsv[..., 2] > 50) & (hsv[..., 2] < 230)).astype(np.uint8)
    mask[:y0 + 5] = 0
    mask[y0 + n_rows * h - 10:] = 0  # the stone edge below the lawn is grey too
    mask[:, :geometry.LAWN_X0] = 0  # mowers, house
    mask[:, 755:] = 0  # stone path right of the lawn
    mask[570:, 500:] = 0  # level progress bar
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    boxes = [[int(v) for v in stats[i]] for i in range(1, count) if stats[i][cv2.CC_STAT_AREA] >= 150]
    merged = True
    while merged:  # one zombie can fall apart into head + shirt and legs
        merged = False
        for i, j in ((i, j) for i in range(len(boxes)) for j in range(i + 1, len(boxes))):
            a, b = boxes[i], boxes[j]
            x_overlap = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]) > -10
            if x_overlap and max(a[1], b[1]) - min(a[1] + a[3], b[1] + b[3]) < STACK_GAP:
                x0, top = min(a[0], b[0]), min(a[1], b[1])
                x1, bottom = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
                boxes[i] = [x0, top, x1 - x0, bottom - top, a[4] + b[4]]
                del boxes[j]
                merged = True
                break
    found = []
    for x, y, w, bh, area in boxes:
        if area < ZOMBIE_MIN_AREA or bh < 40 or w > ZOMBIE_MAX_W:
            continue
        row = min(n_rows - 1, max(0, (y + bh // 2 + 45 - y0) // h))
        col = min(9, max(0, (x - geometry.LAWN_X0) // geometry.CELL_W))
        found.append((row, col))
    return sorted(found, key=lambda rc: rc[1])


def place_zombies(rows: list[RowReport], measured: list[tuple[int, int]]) -> tuple[list[Zombie], dict[int, int]]:
    """Positions from code, types from the model. Each measured row takes the model's types from the
    same row, else from a neighbouring row (the model's usual error is one row), else "unknown".
    A row the model reported that no measured zombie took is kept as the model said only while the
    model counted more zombies than code found: code misses zombies under a banner or still off
    the lawn, but a leftover row is usually the same zombie reported rows away (f66: two zombies
    in rows 1 and 2 read as rows 3 and 4)."""
    by_row: dict[int, list[int]] = {}
    for r, c in measured:
        by_row.setdefault(r, []).append(c)
    taken: set[int] = set()
    zombies: list[Zombie] = []
    nearest: dict[int, int] = {}
    for r, cols in sorted(by_row.items(), key=lambda kv: min(kv[1])):
        source = next((s for s in (r, r - 1, r + 1)
                       if 0 <= s < len(rows) and s not in taken and rows[s].zombies), None)
        types: list[str] = list(rows[source].zombies) if source is not None else []
        if source is not None:
            taken.add(source)
        for i, c in enumerate(sorted(cols)):
            zombies.append(Zombie(type=types[i] if i < len(types) else "unknown", row=r, col=c))
        zombies += [Zombie(type=t, row=r) for t in types[len(cols):]]
        nearest[r] = min(cols)
    unaccounted = sum(len(row.zombies) for row in rows) - len(measured)
    leftover = [r for r, row in enumerate(rows) if row.zombies and r not in taken and r not in by_row]
    for r in sorted(leftover, key=lambda r: rows[r].nearest_col if rows[r].nearest_col is not None else 9):
        if unaccounted <= 0:
            break
        zombies += [Zombie(type=t, row=r) for t in rows[r].zombies]
        unaccounted -= len(rows[r].zombies)
        if rows[r].nearest_col is not None:
            nearest[r] = min(max(rows[r].nearest_col, 0), 9)
    return zombies, nearest


def look_prompt(n_rows: int) -> str:
    looks = "\n".join(f"- {name}: {desc}" for name, desc in ZOMBIE_LOOKS.items())
    return (
        "Image 1 is a Plants vs. Zombies screenshot. Image 2 is its top bar, enlarged: the number under the "
        f"sun icon is the sun count.\nThe lawn has {n_rows} rows and 9 columns; row numbers are written at the "
        "left and column numbers along the top of the lawn (0 = next to the house, 8 = right edge; use 9 for "
        "just off the lawn). For each row, top to bottom, list the types of the zombies you "
        "can clearly see in that row, one entry per zombie, using only these names (\"unknown\" if you see a "
        f"zombie but cannot tell the type):\n{looks}\n"
        "Also give the column of the row's zombie closest to the house. Plants are not zombies. Do not guess: "
        "leave a row empty (nearest_col null) if you see no zombie in it. Answer with compact JSON only."
    )


OTHER_PLANT = "other plant"


def plants_prompt(n_rows: int, names: list[str]) -> str:
    return (
        f"This is a Plants vs. Zombies screenshot. The lawn has {n_rows} rows and 9 columns; row numbers are "
        "written at the left and column numbers along the top of the lawn. For each row, top to bottom, list the plants standing on the lawn with their column. "
        f"Use only these names: {', '.join(names)}. A plant on a lily pad or flower pot is listed as both. Include "
        "plants partly hidden by zombies or suns, and small ones such as potato mines. Ignore zombies, projectiles, "
        "suns and the seed bar at the top. Answer with compact JSON only."
    )


@dataclass
class PlantsResult:
    plants: dict[tuple[int, int], list[str]] | None  # (row, col) -> plants seen there, bottom first
    latency: float
    error: str | None = None
    output_tokens: int = 0


@dataclass
class LookResult:
    scene: Scene | None
    latency: float
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def with_grid(image: np.ndarray, n_rows: int) -> np.ndarray:
    """A copy with faint column dividers and boxed row / column numbers (harness ladder L2).

    Measured 2026-10-28 at temperature 0: small digits alone left the plant read one column off on
    the real day screenshot (2/18 types right); boxed digits plus dividers gave 14/18, 17/18 cells,
    nothing invented, and fixed the zombie columns on the fake lawn (5/5).
    """
    out = image.copy()
    y0, h = geometry.LAWNS[n_rows]
    y1 = y0 + n_rows * h
    lines = out.copy()
    for c in range(1, geometry.COLS):
        x = geometry.LAWN_X0 + c * geometry.CELL_W
        cv2.line(lines, (x, y0), (x, y1), (255, 255, 255), 1)
    out = cv2.addWeighted(lines, 0.5, out, 0.5, 0)
    for c in range(geometry.COLS):
        x = geometry.LAWN_X0 + c * geometry.CELL_W + geometry.CELL_W // 2
        for y in (y0 + 2, y1 - 22):
            cv2.rectangle(out, (x - 10, y), (x + 10, y + 20), (0, 0, 0), -1)
            cv2.putText(out, str(c), (x - 6, y + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    for r in range(n_rows):
        y = y0 + r * h + h // 2
        cv2.rectangle(out, (0, y - 11), (22, y + 11), (0, 0, 0), -1)
        cv2.putText(out, str(r), (5, y + 7), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    return out


def _b64(image: np.ndarray, scale: float = 1.0) -> str:
    if scale != 1.0:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return base64.standard_b64encode(cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])[1]).decode()


class QwenLooker:
    def __init__(self, url: str, model: str, timeout: float, client: httpx.AsyncClient | None = None) -> None:
        self.url, self.model = url, model
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def look_plants(self, image: np.ndarray, loadout: list[str]) -> PlantsResult:
        """Plants on the lawn. Its own call, so the slower plant read never delays the zombie read.

        Names are limited to the agent's loadout plus "other plant". On the day screenshot of
        eval/vision_labels.json this found 14/18 plants with nothing invented; it missed plants
        covered by a zombie or a falling sun, and two potato mines (2026-10-28).
        """
        start = time.monotonic()
        n_rows = 6 if classify_lawn(image) in ("pool", "fog") else 5
        names = sorted(set(loadout) - {"?"}) + [OTHER_PLANT]
        schema = {"type": "object", "required": ["rows"], "properties": {"rows": {"type": "array", "items": {
            "type": "array", "items": {"type": "object", "required": ["col", "plant"], "properties": {
                "col": {"type": "integer", "minimum": 0, "maximum": 8},
                "plant": {"type": "string", "enum": names}}}}}}}
        body = {
            "model": self.model,
            "max_tokens": 1200,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(with_grid(image, n_rows))}"}},
                {"type": "text", "text": plants_prompt(n_rows, names)},
            ]}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "plants", "schema": schema}},
        }
        try:
            response = await self.client.post(self.url, json=body)
            response.raise_for_status()
            payload = response.json()
            rows = json.loads(payload["choices"][0]["message"]["content"])["rows"]
            grid: dict[tuple[int, int], list[str]] = {}
            for r, row in enumerate(rows[:n_rows]):
                for p in row:
                    cell = grid.setdefault((r, int(p["col"])), [])
                    if p["plant"] not in cell:
                        cell.append(p["plant"])
        except httpx.HTTPStatusError as e:
            return PlantsResult(None, time.monotonic() - start, f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except httpx.HTTPError as e:
            return PlantsResult(None, time.monotonic() - start, f"{type(e).__name__}: {e}")
        except (KeyError, IndexError, TypeError, ValueError) as e:
            return PlantsResult(None, time.monotonic() - start, f"bad response: {e!r}")
        usage = payload.get("usage") or {}
        return PlantsResult(grid, time.monotonic() - start, None, usage.get("completion_tokens", 0))

    async def look(self, image: np.ndarray, frame_id: int, loadout: list[str], t_capture: float = 0.0) -> LookResult:
        start = time.monotonic()
        lawn = classify_lawn(image)  # code, not the model
        n_rows = 6 if lawn in ("pool", "fog") else 5
        x0, y0, x1, y1 = geometry.TOP_BAR
        body = {
            "model": self.model,
            "max_tokens": 800,
            "temperature": 0,  # recognition should not be sampled; vLLM's default is 1.0
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(with_grid(image, n_rows))}"}},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64(image[y0:y1, x0:x1], 2.0)}"}},
                {"type": "text", "text": look_prompt(n_rows)},
            ]}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "vision_report", "schema": VisionReport.model_json_schema()}},
        }
        try:
            response = await self.client.post(self.url, json=body)
            response.raise_for_status()
            payload = response.json()
            report = VisionReport.model_validate_json(payload["choices"][0]["message"]["content"])
        except httpx.HTTPStatusError as e:
            return LookResult(None, time.monotonic() - start, f"HTTP {e.response.status_code}: {e.response.text[:200]}")
        except httpx.HTTPError as e:
            return LookResult(None, time.monotonic() - start, f"{type(e).__name__}: {e}")
        except (KeyError, IndexError, ValidationError) as e:
            return LookResult(None, time.monotonic() - start, f"bad response: {e}")
        rows = report.rows[:n_rows]
        if lawn == "day":  # grey zombies on green grass: code measures where they stand
            zombies, nearest = place_zombies(rows, measure_zombies(image, n_rows))
            positions = "code"
        else:  # night graves, roof tiles and water are grey or blue too; not checked yet
            zombies = [Zombie(type=t, row=r) for r, row in enumerate(rows) for t in row.zombies]
            nearest = {r: min(max(row.nearest_col, 0), 9) for r, row in enumerate(rows)
                       if row.zombies and row.nearest_col is not None}
            positions = "model"
        scene = Scene(frame_id=frame_id, t_capture=t_capture, lawn=lawn, rows=n_rows, sun=report.sun,
                      cards=read_cards(image, loadout), zombies=zombies, nearest_col=nearest,
                      positions=positions, other=report.other)
        usage = payload.get("usage") or {}
        return LookResult(scene, time.monotonic() - start, None, usage.get("prompt_tokens", 0),
                          usage.get("completion_tokens", 0))
