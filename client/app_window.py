"""The client's only window: the unified control center page in a chromeless Edge window pinned beside the game.

The page (scripts/training_dashboard.py on 127.0.0.1:8766) shows the client and training, and reaches the
client's buttons through the loopback panel bridge. Edge ships with Windows, so nothing extra is installed.
It opens as an app window of the user's own Edge profile: no new profile, sign-in or sync prompt.
"""
import ctypes
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import win32con
import win32gui

ROOT = Path(__file__).resolve().parents[1]
PORT = 8766
URL = f"http://127.0.0.1:{PORT}/client"
TITLE = "PvZ 控制中心 · 真实游戏客户端"  # the page's <title>; Edge app windows carry it without a suffix
SIZE = (760, 900)  # device-independent pixels, scaled by the game monitor's DPI
EDGE = [Path(os.environ.get(name, "")) / "Microsoft/Edge/Application/msedge.exe"
        for name in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]

log = logging.getLogger("pvz.window")


def listening(port=PORT) -> bool:
    with socket.socket() as probe:
        probe.settimeout(.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def ensure_control_center(wait_s=15.0) -> bool:
    """Start the control center in the background unless it already runs; it outlives this client."""
    if listening():
        return True
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(logs / "dashboard.log", "ab") as out, open(logs / "dashboard.err", "ab") as err:
        subprocess.Popen([sys.executable, "-X", "utf8", "-u", str(ROOT / "scripts/training_dashboard.py")],
                         cwd=ROOT, stdout=out, stderr=err, stdin=subprocess.DEVNULL, creationflags=flags)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if listening():
            return True
        time.sleep(.25)
    return False


def placement(game_rect, work_area, scale=1.0, size=SIZE, min_width=420):
    """Right of the game if it fits, else left; narrower on the roomier side rather than covering the game."""
    x0, y0, w, _ = game_rect
    left, top, right, bottom = work_area
    full, smallest = round(size[0] * scale), round(min_width * scale)
    beside_right, beside_left = right - (x0 + w + 16), x0 - 16 - left
    if beside_right >= full or beside_right >= beside_left:
        width = max(smallest, min(full, beside_right))
        x = min(x0 + w + 16, right - width)
    else:
        width = max(smallest, min(full, beside_left))
        x = max(left, x0 - 16 - width)
    height = min(round(size[1] * scale), bottom - top)
    y = max(top, min(y0, bottom - height))
    return x, y, width, height


def windows_titled(title=TITLE):
    found = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == title:
            found.append(hwnd)
    win32gui.EnumWindows(visit, None)
    return found


class AppWindow:
    def __init__(self, hwnd):
        self.hwnd = hwnd

    @classmethod
    def open(cls, game_hwnd, game_rect, work_area, wait_s=20.0):
        edge = next((path for path in EDGE if path.is_file()), None)
        if edge is None:
            raise FileNotFoundError("Microsoft Edge not found")
        before = set(windows_titled())
        for stale in before:  # left by a client that was killed; only one client runs at a time
            win32gui.PostMessage(stale, win32con.WM_CLOSE, 0, 0)
        subprocess.Popen([str(edge), f"--app={URL}", "--no-first-run", "--no-default-browser-check"])
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            new = [hwnd for hwnd in windows_titled() if hwnd not in before]
            if new:
                user32 = ctypes.windll.user32
                scale = (user32.GetDpiForWindow(game_hwnd) if game_hwnd else user32.GetDpiForSystem()) / 96 or 1.0
                x, y, width, height = placement(game_rect, work_area, scale)
                win32gui.SetWindowPos(new[0], win32con.HWND_TOPMOST, x, y, width, height, 0)
                return cls(new[0])
            time.sleep(.2)
        raise TimeoutError("control window did not appear")

    def is_open(self) -> bool:
        return bool(win32gui.IsWindow(self.hwnd))

    def close(self) -> None:
        if self.is_open():
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
