"""Screen geometry of the 800x600 game window.

Seed bank numbers were measured on official Steam screenshots (1440x1080 = exactly 1.8x the
game's 800x600) on 2026-10-28. Lawn numbers are still rough and must be calibrated during
environment verification (step 3, clicking the corners of the lawn).
"""

from __future__ import annotations

WIDTH, HEIGHT = 800, 600

COLS = 9
LAWNS = {  # rows, top edge of row 0, row height. Pool and fog levels have six rows.
    5: (80, 100),
    6: (76, 86),
}
ROWS = 5  # default lawn (day, night, roof)
LAWN_X0, CELL_W = 40, 80

# Seed bank (measured).
SEED_CARD_X0 = 96  # left edge of slot 0
SEED_CARD_DX = 59.1  # slot pitch
SEED_CARD_W = 48
SEED_CARD_Y0, SEED_CARD_Y1 = 8, 78
SEED_PICTURE_Y0, SEED_PICTURE_Y1 = 12, 58  # card picture, above the cost text
SUN_COUNTER = (22, 58, 78, 81)  # x0, y0, x1, y1 of the sun number
TOP_BAR = (0, 0, 600, 84)  # x0, y0, x1, y1: sun counter + up to 9 cards
# Shovel box, right of the seed bank: Board::GetShovelButtonRect is 456 + GetSeedBankExtraWidth()
# (0 up to 6 slots, then 60/76/112/153 for 7-10), 70x72; checked on a live 6-slot frame (2026-11-03).
SHOVEL_XS = (456, 516, 532, 568, 609)
SHOVEL_W, SHOVEL_H = 70, 72


def cell_center(row: int, col: int, rows: int = ROWS) -> tuple[int, int]:
    if rows not in LAWNS or not (0 <= row < rows and 0 <= col < COLS):
        raise ValueError(f"cell out of range: row={row} col={col} rows={rows}")
    y0, h = LAWNS[rows]
    return LAWN_X0 + CELL_W * col + CELL_W // 2, y0 + h * row + h // 2


def seed_card_rect(slot: int) -> tuple[int, int, int, int]:
    if not 0 <= slot < 10:
        raise ValueError(f"seed slot out of range: {slot}")
    x0 = round(SEED_CARD_X0 + SEED_CARD_DX * slot)
    return x0, SEED_CARD_Y0, x0 + SEED_CARD_W, SEED_CARD_Y1


def seed_slot_center(slot: int) -> tuple[int, int]:
    x0, y0, x1, y1 = seed_card_rect(slot)
    return (x0 + x1) // 2, (y0 + y1) // 2


def shovel_center(x0: int) -> tuple[int, int]:
    if x0 not in SHOVEL_XS:
        raise ValueError(f"no shovel box at x={x0}")
    return x0 + SHOVEL_W // 2, SHOVEL_H // 2
