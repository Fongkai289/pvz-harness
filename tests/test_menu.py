from pathlib import Path

import cv2
import numpy as np
import pytest

from client.menu import is_blocking_menu, is_continue_dialog, is_main_menu, is_pause_panel, is_playing
from client.menu import is_game_over


ROOT = Path(__file__).resolve().parents[1]


def test_failure_reference_is_blocked_but_playing_and_pause_are_not_failure():
    lost = cv2.imread(str(ROOT / "docs/screens/game_over.jpg"))
    assert is_game_over(lost) and is_blocking_menu(lost) and not is_playing(lost)
    for name in ("game_paused.jpg", "continue_game.jpg", "day_1-6_start.png", "main_menu.png"):
        assert not is_game_over(cv2.imread(str(ROOT / "docs/screens" / name)))


def test_main_menu_with_updated_name_level_and_hover():
    frame = cv2.imread(str(ROOT / "docs/screens/main_menu.png"))
    frame[40:125, 30:310] = 0
    frame[40:185, 405:730] = 255
    assert is_main_menu(frame)
    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    assert ok and is_main_menu(cv2.imdecode(jpeg, cv2.IMREAD_COLOR))


@pytest.mark.parametrize("path", sorted((ROOT / "docs/screens").glob("day_*.png"))
                         + sorted((ROOT / "tests/fixtures").glob("*.jpg")))
def test_gameplay_and_pause_are_not_main_menu(path):
    assert not is_main_menu(cv2.imread(str(path)))


def test_darkened_modal_background_and_wrong_size_are_not_menu():
    frame = cv2.imread(str(ROOT / "docs/screens/main_menu.png"))
    assert not is_main_menu((frame.astype(float) * 0.5).astype(np.uint8))
    assert not is_main_menu(cv2.resize(frame, (640, 480)))
    assert not is_main_menu(np.zeros_like(frame))


def test_real_saved_game_confirmation_is_not_a_pause_or_playing():
    frame = cv2.imread(str(ROOT / "docs/screens/continue_game.jpg"))
    assert is_continue_dialog(frame)
    assert not is_pause_panel(frame)
    assert not is_playing(frame)
    assert not is_main_menu(frame)


def test_real_pause_is_distinguished_from_playing():
    frame = cv2.imread(str(ROOT / "tests/fixtures/day_1-6_paused.jpg"))
    assert is_pause_panel(frame)
    assert not is_continue_dialog(frame)
    assert not is_playing(frame)
    lawn = cv2.imread(str(ROOT / "docs/screens/day_1-6_start.png"))
    assert is_playing(lawn)
    assert not is_pause_panel(lawn)
    assert not is_continue_dialog(lawn)


def test_sun_collection_animation_does_not_pause_a_running_game():
    lawn = cv2.imread(str(ROOT / "docs/screens/day_1-6_start.png"))
    lawn[0:85, 0:90] = (0, 240, 255)  # animated sun overlapping the icon/counter
    assert not is_playing(lawn)  # strict startup detector cannot identify the altered icon
    assert not is_blocking_menu(lawn)  # this must NOT pause a session already in progress
    for name in ("main_menu.png", "game_paused.jpg", "continue_game.jpg"):
        assert is_blocking_menu(cv2.imread(str(ROOT / "docs/screens" / name)))
