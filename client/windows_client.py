"""Real client for the Windows game machine (checked in docs/env.md, items 1-3 and 8).

    pip install -e ".[client]"   (or: uv sync --extra client)
    ssh -N -L 8765:127.0.0.1:8765 -p 32420 -i <key> root@<server>    # tunnel, keep open
    python -m client.windows_client                   # token: --token or $env:PVZ_TOKEN

Captures the game window's client area, streams it, and executes clicks relative
to that area. Pause/resume commands suspend and resume the game process
(popcapgame1.exe, the child of the Steam launcher).

A small control window next to the game lets the operator pause, resume, restart
the level (a new server session per attempt) and stop; without --seconds the
client runs until Stop.

The game must run windowed (full screen minimizes itself on focus loss), stay
uncovered (mss copies screen pixels, so anything on top lands in the frame and
takes the clicks), and keep focus (it opens its own pause menu when it loses focus;
unless the operator paused, the client closes that menu again). The window is made
topmost for the run; clicks on a point the game does not own are refused.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import os
import statistics
import threading
import time

import cv2
import mss
import numpy as np
import psutil
import pydirectinput
import win32api
import win32con
import win32gui
import win32process

from client.link import ServerLink
from common.protocol import Command

WINDOW_TITLE = "Plants vs. Zombies"
# The same process also owns a hidden "InvisWindow" with this title; the game draws in MainWindow.
WINDOW_CLASS = "MainWindow"
JPEG_QUALITY = 80
CLICK_SETTLE_S = 0.03  # enough for cards and the lawn
BUTTON_HOVER_S = 0.3  # menu buttons only react after the pointer has hovered over them
AUTO_RESUME_GAP_S = 1.5

# Game screens, 800x600 client coordinates (docs/env.md).
RESUME_BUTTON = (390, 448)  # "Resume Game" on the pause menu
RESTART_CLICKS = [  # (x, y, wait after): Menu -> Restart Level -> confirm Restart
    (738, 14, 1.0),
    (398, 355, 1.0),
    (303, 393, 1.0),
]

log = logging.getLogger("pvz.windows")


def make_dpi_aware() -> None:
    # Without this, Windows display scaling shifts every click coordinate.
    ctypes.windll.shcore.SetProcessDpiAwareness(2)


def click_screen(x: int, y: int, hover: float = CLICK_SETTLE_S) -> None:
    # pydirectinput.click(x, y) normalizes absolute moves to the primary display only, so it
    # misses a game window on another monitor. SetCursorPos takes virtual-desktop pixels.
    win32api.SetCursorPos((x, y))
    time.sleep(hover)  # let the game see the move first, or the press lands at the old spot
    pydirectinput.mouseDown()
    pydirectinput.mouseUp()


def is_pause_panel(frame: np.ndarray) -> bool:
    """The game's dark slate pause menu. Its in-game Menu dialog looks the same, so this is
    only consulted while that dialog cannot be open (not during a restart)."""
    b, g, r = frame[200:380, 260:540].reshape(-1, 3).mean(0)
    return g < 90 and b > g and (b + g + r) / 3 < 70


class GameWindow:
    def __init__(self, title: str = WINDOW_TITLE) -> None:
        self.hwnd = win32gui.FindWindow(WINDOW_CLASS, title)
        if not self.hwnd:
            raise RuntimeError(f"window not found: {WINDOW_CLASS} {title!r}")
        if win32gui.IsIconic(self.hwnd):
            raise RuntimeError("game window is minimized (full screen mode?); run it windowed and restore it")
        _, pid = win32process.GetWindowThreadProcessId(self.hwnd)
        self.process = psutil.Process(pid)
        self._local = threading.local()  # mss handles are per thread
        pydirectinput.PAUSE = 0  # no built-in delay between input events
        self.auto_resume = True
        self.auto_resumes = 0
        self._last_auto_resume = 0.0
        _, _, w, h = self.client_rect()
        if (w, h) != (800, 600):
            log.warning("client area is %dx%d, not 800x600: check the game's high-DPI override", w, h)

    @property
    def sct(self) -> mss.base.MSSBase:
        if not hasattr(self._local, "sct"):
            self._local.sct = mss.mss()
        return self._local.sct

    def client_rect(self) -> tuple[int, int, int, int]:
        left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
        x0, y0 = win32gui.ClientToScreen(self.hwnd, (left, top))
        return x0, y0, right - left, bottom - top

    def set_topmost(self, on: bool) -> None:
        after = win32con.HWND_TOPMOST if on else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(self.hwnd, after, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)

    def owns(self, x: int, y: int) -> bool:
        """True if the screen point (x, y) shows the game, not a window on top of it."""
        return win32gui.GetAncestor(win32gui.WindowFromPoint((x, y)), win32con.GA_ROOT) == self.hwnd

    def capture(self) -> np.ndarray:
        x0, y0, w, h = self.client_rect()
        return np.asarray(self.sct.grab({"left": x0, "top": y0, "width": w, "height": h}))[:, :, :3]

    def press_button(self, x: int, y: int) -> None:
        """Click a game UI button (client coordinates), hovering first so the button arms."""
        x0, y0, _, _ = self.client_rect()
        if not self.owns(x0 + x, y0 + y):
            raise RuntimeError(f"button ({x},{y}) refused: another window covers the game there")
        click_screen(x0 + x, y0 + y, hover=BUTTON_HOVER_S)

    def take_focus(self) -> None:
        """Bring the game forward and close its own pause menu (it opens one on every focus loss)."""
        try:
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception as e:  # Windows may refuse; the first click still focuses the game
            log.warning("could not focus the game: %s", e)
        time.sleep(0.3)
        if is_pause_panel(self.capture()):
            log.info("game is on its pause menu; resuming")
            self.press_button(*RESUME_BUTTON)
            time.sleep(0.5)

    def restart_level(self) -> None:
        self.take_focus()
        for x, y, wait in RESTART_CLICKS:
            self.press_button(x, y)
            time.sleep(wait)

    def grab(self) -> tuple[bytes, int, int]:
        shot = self.capture()
        if self.auto_resume and is_pause_panel(shot) and time.monotonic() - self._last_auto_resume > AUTO_RESUME_GAP_S:
            # Focus went elsewhere and the game paused itself; the operator did not ask for it.
            self._last_auto_resume = time.monotonic()
            self.auto_resumes += 1
            log.info("game paused itself (focus lost); resuming")
            try:
                self.press_button(*RESUME_BUTTON)
            except RuntimeError as e:
                log.warning("auto-resume failed: %s", e)
        ok, buf = cv2.imencode(".jpg", shot, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            raise RuntimeError("jpeg encode failed")
        return buf.tobytes(), shot.shape[1], shot.shape[0]

    async def execute(self, cmd: Command) -> dict | None:  # outcome unknown on the real game: None
        if cmd.action == "click":
            x0, y0, w, h = self.client_rect()
            if not (0 <= cmd.x < w and 0 <= cmd.y < h):
                raise ValueError(f"click ({cmd.x},{cmd.y}) outside the {w}x{h} game area")
            if not self.owns(x0 + cmd.x, y0 + cmd.y):
                raise RuntimeError(f"click ({cmd.x},{cmd.y}) refused: another window covers the game there")
            click_screen(x0 + cmd.x, y0 + cmd.y)
        elif cmd.action == "pause":
            self.process.suspend()
        elif cmd.action == "resume":
            self.process.resume()
        elif cmd.action == "set_speed":
            raise NotImplementedError("speed control is decided in environment verification step 6")


class ControlPanel:
    """Operator window next to the game: pause / resume / restart level / stop.

    Tk runs in its own thread; the asyncio side only reads the events below. While held
    (paused or restarting) no frames go up and server commands are refused."""

    def __init__(self, game: GameWindow) -> None:
        self.game = game
        self.paused = threading.Event()
        self.restart = threading.Event()  # asks the main loop to end the session and restart
        self.restarting = threading.Event()
        self.stop = threading.Event()
        self.link: ServerLink | None = None
        self.sessions: list[str] = []
        self._thread = threading.Thread(target=self._run, name="control-panel", daemon=True)

    def start(self) -> None:
        self._thread.start()

    # ---- asyncio side ----

    def held(self) -> bool:
        return self.paused.is_set() or self.restarting.is_set()

    def grab(self) -> tuple[bytes, int, int]:
        while self.held() and not (self.stop.is_set() or self.restart.is_set()):
            time.sleep(0.05)
        return self.game.grab()

    async def execute(self, cmd: Command) -> dict | None:
        if self.held():
            raise RuntimeError("held by the operator (paused or restarting)")
        return await self.game.execute(cmd)

    def do_restart(self) -> None:
        self.restarting.set()
        self.game.auto_resume = False  # the Menu dialog would look like the pause menu
        try:
            self.game.restart_level()
        finally:
            self.game.auto_resume = True
            self.restart.clear()
            self.paused.clear()
            self.restarting.clear()

    # ---- Tk side ----

    def _run(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        root.title("PvZ 控制")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._on_stop)
        font = ("Microsoft YaHei UI", 10)
        self._status = tk.StringVar(value="连接中…")
        tk.Label(root, textvariable=self._status, font=font, justify="left", anchor="w", width=30).pack(
            padx=8, pady=(8, 4), fill="x")
        row = tk.Frame(root)
        row.pack(padx=8, pady=(0, 8))
        for text, cmd in (("暂停", self._on_pause), ("继续", self._on_resume),
                          ("重玩本关", self._on_restart), ("结束", self._on_stop)):
            tk.Button(row, text=text, font=font, width=7, command=cmd).pack(side="left", padx=2)
        root.update_idletasks()
        # Beside the game, never over it: a covered game point refuses clicks.
        x0, y0, w, _ = self.game.client_rect()
        mon = win32api.GetMonitorInfo(win32api.MonitorFromWindow(self.game.hwnd))["Monitor"]
        x = x0 + w + 16 if x0 + w + 16 + root.winfo_width() <= mon[2] else max(mon[0], x0 - root.winfo_width() - 16)
        root.geometry(f"+{x}+{y0}")
        self._root = root
        self._tick()
        root.mainloop()
        # Tk objects must die in the thread that made them, not at interpreter exit.
        self._status = self._root = None
        del root
        import gc
        gc.collect()

    def close(self) -> None:
        self.stop.set()
        self._thread.join(timeout=3)

    def _tick(self) -> None:
        if self.stop.is_set():
            self._root.destroy()
            return
        state = ("重玩中…" if self.restarting.is_set() or self.restart.is_set()
                 else "已暂停" if self.paused.is_set() else "运行中")
        lines = [f"状态：{state}"]
        if self.link and self.link.session_id:
            lines.append(f"会话：{self.link.session_id}")
            lat = self.link.latencies
            lines.append(f"命令：{len(lat)}" + (f"  延迟中位数 {statistics.median(lat) * 1000:.0f} ms" if lat else ""))
        lines.append(f"自动解除暂停：{self.game.auto_resumes} 次")
        self._status.set("\n".join(lines))
        self._root.after(300, self._tick)

    def _on_pause(self) -> None:
        # Clicking this window already took focus from the game, which pauses itself.
        self.paused.set()

    def _on_resume(self) -> None:
        def resume() -> None:
            self.game.take_focus()  # allowed: this process owns the foreground right now
            self.paused.clear()
        threading.Thread(target=resume, daemon=True).start()

    def _on_restart(self) -> None:
        self.paused.set()  # hold frames and commands until the restart is done
        self.restart.set()

    def _on_stop(self) -> None:
        self.stop.set()


def read_state() -> dict | None:
    # Read-only memory reads (pymem) go here once offsets for the installed game
    # version are confirmed in environment verification step 4.
    return None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    parser.add_argument("--token", default=os.environ.get("PVZ_TOKEN"),
                        help="defaults to $env:PVZ_TOKEN, so it stays out of the command line")
    parser.add_argument("--seconds", type=float, default=None, help="stop after this long; default: run until Stop")
    parser.add_argument("--fps", type=float, default=20)
    parser.add_argument("--game-version", default="1.2.0.1096")
    parser.add_argument("--no-topmost", action="store_true", help="leave the window's z-order alone")
    args = parser.parse_args()
    if not args.token:
        parser.error("no token: pass --token or set $env:PVZ_TOKEN")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    make_dpi_aware()
    game = GameWindow()
    if not args.no_topmost:
        game.set_topmost(True)
    panel = ControlPanel(game)
    panel.start()
    game.take_focus()
    deadline = time.monotonic() + args.seconds if args.seconds else None

    def done() -> bool:
        return panel.stop.is_set() or panel.restart.is_set() or (deadline is not None and time.monotonic() > deadline)

    try:
        while True:
            link = ServerLink(args.url, args.token, "windows", panel.grab, panel.execute,
                              read_state=read_state, fps=args.fps, game_version=args.game_version)
            panel.link = link
            await link.run(done=done)
            panel.sessions.append(link.session_id or "?")
            print(link.session_id, link.latency_summary(), flush=True)
            if not panel.restart.is_set() or panel.stop.is_set():
                break
            log.info("restarting the level")
            await asyncio.to_thread(panel.do_restart)
    finally:
        panel.close()
        if game.process.status() == psutil.STATUS_STOPPED:
            game.process.resume()  # never leave the game suspended
        if not args.no_topmost:
            game.set_topmost(False)
    print("sessions:", ", ".join(panel.sessions))


if __name__ == "__main__":
    asyncio.run(main())
