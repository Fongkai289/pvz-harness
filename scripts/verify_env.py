"""Environment verification on the Windows game machine (docs/HANDOFF_windows.md, section 4).

    .venv\\Scripts\\python scripts\\verify_env.py               # all items
    .venv\\Scripts\\python scripts\\verify_env.py --items 1,2
    .venv\\Scripts\\python scripts\\verify_env.py --items 2 --shot docs/screens/day_1-1.png

Each item prints PASS / FAIL / SKIP / MANUAL with its numbers. Item 7 (model latency)
was done on the server; item 8 (server round trip) runs through client.windows_client.
Memory access here is read-only: nothing is written to or injected into the game.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import re
import sys
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import mss  # noqa: E402
import numpy as np  # noqa: E402
import psutil  # noqa: E402
import win32api  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

EXPECTED_VERSION = "1.2.0.1096"
STEAM_APP_ID = 3590  # Plants vs. Zombies: Game of the Year
GAME_EXES = {"plantsvszombies.exe", "popcapgame1.exe"}
CLIENT_SIZE = (800, 600)
MIN_FPS = 20.0
OCCLUSION_GRID = 5


@dataclass
class Result:
    item: int
    name: str
    status: str  # PASS | FAIL | SKIP | MANUAL
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


def file_version(path: str | Path) -> tuple[str, str]:
    """(string FileVersion, fixed numeric FileVersion). The Steam GOTY exe disagrees with
    itself: the string (what Explorer and the community call the version) says 1.2.0.1096,
    the numeric field says 1.2.0.1095. The string one is authoritative here."""
    info = win32api.GetFileVersionInfo(str(path), "\\")
    ms, ls = info["FileVersionMS"], info["FileVersionLS"]
    fixed = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    lang, cp = win32api.GetFileVersionInfo(str(path), "\\VarFileInfo\\Translation")[0]
    string = win32api.GetFileVersionInfo(str(path), f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileVersion")
    return (string or fixed).strip(), fixed


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def steam_install_dir() -> Path | None:
    """The game's folder from the Steam library manifests, whether or not it is running."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        return None
    libraries = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        libraries += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8"))]
    for lib in libraries:
        manifest = lib / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf"
        if manifest.exists():
            m = re.search(r'"installdir"\s+"([^"]+)"', manifest.read_text(encoding="utf-8"))
            if m:
                return lib / "steamapps" / "common" / m.group(1)
    return None


def find_game_window() -> tuple[int, psutil.Process] | None:
    """Top-level visible window owned by the game process (title-independent)."""
    found: list[tuple[int, psutil.Process]] = []

    def visit(hwnd: int, _: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetParent(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            proc = psutil.Process(pid)
            if proc.name().lower() in GAME_EXES and win32gui.GetWindowText(hwnd):
                found.append((hwnd, proc))
        except psutil.Error:
            pass
        return True

    win32gui.EnumWindows(visit, None)
    return found[0] if found else None


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    x0, y0 = win32gui.ClientToScreen(hwnd, (left, top))
    return x0, y0, right - left, bottom - top


def occluded_points(hwnd: int, x0: int, y0: int, w: int, h: int) -> int:
    """How many points of a grid over the client area belong to some other window."""
    covered = 0
    for i in range(OCCLUSION_GRID):
        for j in range(OCCLUSION_GRID):
            x = x0 + int((i + 0.5) * w / OCCLUSION_GRID)
            y = y0 + int((j + 0.5) * h / OCCLUSION_GRID)
            top = win32gui.WindowFromPoint((x, y))
            if win32gui.GetAncestor(top, win32con.GA_ROOT) != hwnd:
                covered += 1
    return covered


# ---- items ----


def item1_version() -> Result:
    name = "游戏版本"
    game = find_game_window()
    # The Steam exe is a DRM launcher; the game itself runs as its child popcapgame1.exe,
    # unpacked under ProgramData, which carries no version resource. Version the launcher.
    install = steam_install_dir()
    exe = install / "PlantsVsZombies.exe" if install else None
    if game and game[1].name().lower() == "plantsvszombies.exe":
        exe = Path(game[1].exe())
    elif game and (parent := game[1].parent()) and parent.name().lower() == "plantsvszombies.exe":
        exe = Path(parent.exe())
    if exe is None or not exe.exists():
        return Result(1, name, "FAIL", "找不到游戏 exe（Steam 清单里没有 appid 3590，游戏也没在运行）")
    version, fixed = file_version(exe)
    data = {"exe": str(exe), "version": version, "version_fixed_field": fixed, "sha256": sha256(exe),
            "running": game is not None}
    if game:
        data |= {"window_title": win32gui.GetWindowText(game[0]), "window_class": win32gui.GetClassName(game[0]),
                 "game_process": game[1].name(), "game_exe": game[1].exe(), "pid": game[1].pid}
    if version != EXPECTED_VERSION:
        return Result(1, name, "FAIL", f"版本 {version}，预期 {EXPECTED_VERSION}", data)
    toolkit = "；PvZ Toolkit 能否连上需手动确认" if game else "；游戏未运行，窗口标题和 PvZ Toolkit 待确认"
    return Result(1, name, "PASS", f"{exe.name} {version}{toolkit}", data)


def item2_capture(shot: Path | None = None, seconds: float = 3.0) -> Result:
    name = "截图"
    game = find_game_window()
    if not game:
        return Result(2, name, "FAIL", "游戏窗口没找到（先以窗口模式启动游戏）")
    hwnd = game[0]
    # Both modes are WS_POPUP; windowed adds a caption. Full screen minimizes itself on focus loss.
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
    fullscreen = bool(style & win32con.WS_POPUP) and not style & win32con.WS_CAPTION
    if win32gui.IsIconic(hwnd):
        hint = "游戏是全屏模式，失去焦点就会最小化；在游戏选项里取消“全屏”" if fullscreen else "把窗口还原"
        return Result(2, name, "FAIL", f"游戏窗口最小化了：{hint}", {"fullscreen": fullscreen})
    dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
    x0, y0, w, h = client_rect(hwnd)
    data: dict[str, Any] = {"client_origin": (x0, y0), "client_size": (w, h), "window_dpi": dpi,
                            "scale_percent": round(dpi / 96 * 100), "fullscreen": fullscreen}
    # mss copies screen pixels, so any window on top of the game ends up in the frame.
    covered = occluded_points(hwnd, x0, y0, w, h)
    data["occluded_points"] = f"{covered}/{OCCLUSION_GRID ** 2}"
    with mss.mss() as sct:
        region = {"left": x0, "top": y0, "width": w, "height": h}
        frame = np.asarray(sct.grab(region))[:, :, :3]
        # Black borders: an outer 4-px strip that is almost entirely black.
        strips = {"top": frame[:4], "bottom": frame[-4:], "left": frame[:, :4], "right": frame[:, -4:]}
        black = {k: float((s.max(axis=2) < 16).mean()) for k, s in strips.items()}
        data["black_border_fraction"] = {k: round(v, 3) for k, v in black.items()}
        # Same work per frame as the real client: grab + JPEG encode.
        n, start = 0, time.perf_counter()
        while time.perf_counter() - start < seconds:
            img = np.asarray(sct.grab(region))[:, :, :3]
            cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            n += 1
        fps = n / (time.perf_counter() - start)
    data["fps"] = round(fps, 1)
    if shot:
        shot.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(shot), frame)
        data["saved"] = str(shot)

    problems = []
    if fullscreen:
        problems.append("游戏是全屏模式，要求窗口模式")
    if (w, h) != CLIENT_SIZE:
        problems.append(f"客户区 {w}×{h}，不是 800×600（窗口 DPI {dpi} = {data['scale_percent']}%）")
    if bad := [k for k, v in black.items() if v > 0.95]:
        problems.append(f"疑似黑边：{', '.join(bad)}")
    if covered:
        problems.append(f"游戏窗口被别的窗口挡住（{data['occluded_points']} 个采样点），截图里会混进别的画面")
    if fps < MIN_FPS:
        problems.append(f"帧率 {fps:.1f} < {MIN_FPS:.0f}")
    summary = f"{w}×{h}，{fps:.1f} 帧/秒，窗口 DPI {data['scale_percent']}%"
    return Result(2, name, "FAIL" if problems else "PASS", "；".join(problems) or summary, data)


def not_yet(item: int, name: str) -> Callable[[], Result]:
    return lambda: Result(item, name, "SKIP", "尚未实现")


def item3_clicks() -> Result:
    # Needs a live level, sun and planting; done by playing adventure 1-1 to 1-4 on 2026-10-28.
    return Result(3, "点击", "MANUAL", "已手动验证：1-4 中 20/20 落在预期格子（含四个角），见 docs/env.md 第 3 项")


ITEMS: dict[int, Callable[..., Result]] = {
    1: item1_version,
    2: item2_capture,
    3: item3_clicks,
    4: not_yet(4, "读状态（只读）"),
    5: not_yet(5, "暂停"),
    6: not_yet(6, "变速 / AvZ"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", default=",".join(map(str, ITEMS)), help="comma-separated, e.g. 1,2")
    parser.add_argument("--shot", type=Path, default=None, help="item 2: save the captured frame here")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    # Must come before any window or capture call, or display scaling shifts every coordinate.
    ctypes.windll.shcore.SetProcessDpiAwareness(2)

    results = []
    for i in (int(s) for s in args.items.split(",")):
        try:
            r = ITEMS[i](args.shot) if i == 2 else ITEMS[i]()
        except Exception as e:  # one broken item must not hide the others
            r = Result(i, f"#{i}", "FAIL", f"{type(e).__name__}: {e}")
        results.append(r)
        print(f"[{r.status:6}] {r.item}. {r.name}: {r.detail}")
        for k, v in r.data.items():
            print(f"           {k} = {v}")
    return 0 if all(r.status in ("PASS", "SKIP", "MANUAL") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
