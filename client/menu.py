"""Read-only recognition of game menus; the client never operates menu buttons."""

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np


# Static artwork: exclude the player's name, level number and hoverable buttons.
MENU_REGIONS = ((35, 185, 300, 235), (110, 280, 330, 410))


@lru_cache(maxsize=5)
def _reference(name: str = "main_menu.png") -> np.ndarray:
    path = Path(__file__).resolve().parents[1] / "docs/screens" / name
    frame = cv2.imread(str(path))
    if frame is None:
        raise RuntimeError(f"缺少菜单参考图 docs/screens/{name}")
    return frame


def _matches(frame: np.ndarray, name: str, regions: tuple) -> bool:
    if frame.shape != (600, 800, 3):
        return False
    reference = _reference(name)
    for x0, y0, x1, y1 in regions:
        patch = frame[y0:y1, x0:x1]
        expected = reference[y0:y1, x0:x1]
        score = cv2.matchTemplate(
            patch, expected, cv2.TM_CCOEFF_NORMED
        )[0, 0]
        # Correlation alone would accept a darkened menu behind a modal dialog.
        if not np.isfinite(score) or score < 0.9 or cv2.absdiff(patch, expected).mean() > 25:
            return False
    return True


def is_main_menu(frame: np.ndarray) -> bool:
    return _matches(frame, "main_menu.png", MENU_REGIONS)


def is_continue_dialog(frame: np.ndarray) -> bool:
    return _matches(frame, "continue_game.jpg", ((260, 210, 540, 243),))


def is_pause_panel(frame: np.ndarray) -> bool:
    # The saved-game confirmation has the same slate background, but a different title.
    return _matches(frame, "game_paused.jpg", ((290, 175, 510, 205),))


def is_game_over(frame: np.ndarray) -> bool:
    return _matches(frame, "game_over.jpg", ((280, 270, 520, 330),))


def is_playing(frame: np.ndarray) -> bool:
    if not _matches(frame, "day_1-6_start.png", ((10, 2, 86, 56),)):
        return False
    return not is_blocking_menu(frame)


def is_blocking_menu(frame: np.ndarray) -> bool:
    """Detect positive menu evidence; a changing sun icon is not evidence of a pause."""
    if frame.shape != (600, 800, 3):
        return True
    if is_main_menu(frame) or is_pause_panel(frame) or is_continue_dialog(frame) or is_game_over(frame):
        return True
    # The in-game Options/Menu dialog also uses this slate panel.
    b, g, r = frame[200:380, 260:540].reshape(-1, 3).mean(0)
    return bool(g < 90 and b > g and (b + g + r) / 3 < 70)
