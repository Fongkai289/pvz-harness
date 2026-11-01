"""Real client for the Windows game machine (checked in docs/env.md, items 1-3 and 8).

    pip install -e ".[client]"   (or: uv sync --extra client)
    ssh -N -L 8765:127.0.0.1:8765 -p 22 -i <key> root@<server>    # tunnel, keep open
    python -m client.windows_client                   # token: --token or $env:PVZ_TOKEN

Captures the game client area and executes Jev-authorized gameplay clicks only.
Start / Pause / Resume / Stop control this client, never the game menus or process.
The client starts disabled. Once enabled, it waits for the operator to bring the game
to the foreground and manually enter/resume play. Menu/focus holds clear automatically
when a stable playable screen returns; an explicit client pause never clears itself.
Game win/loss still closes the session and requests coach review. The operator alone
handles rewards, seed selection and retries. No game UI click or focus stealing is used.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import os
import threading
import time

import cv2
import mss
import numpy as np
import psutil
import pydirectinput
import win32api
import win32con
import win32event
import win32gui
import win32process

from client.link import ServerLink
from client.model_activity import ModelActivity, activity_lines
from client.mower_state import MowerMonitor
from client.operator_view import OperatorView
from client.menu import is_playing, is_blocking_menu
from common.protocol import Command

WINDOW_TITLE = "Plants vs. Zombies"
# The same process also owns a hidden "InvisWindow" with this title; the game draws in MainWindow.
WINDOW_CLASS = "MainWindow"
JPEG_QUALITY = 80
CLICK_SETTLE_S = 0.03  # enough for cards and the lawn


log = logging.getLogger("pvz.windows")


class GamePaused(RuntimeError):
    """Gameplay unavailable; observe and wait without changing the game."""


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

    def operator_has_focus(self) -> bool:
        foreground = win32gui.GetForegroundWindow()
        if not foreground:
            return False
        _, pid = win32process.GetWindowThreadProcessId(foreground)
        return pid == os.getpid()

    def has_focus(self) -> bool:
        return win32gui.GetForegroundWindow() == self.hwnd

    def capture(self) -> np.ndarray:
        x0, y0, w, h = self.client_rect()
        return np.asarray(self.sct.grab({"left": x0, "top": y0, "width": w, "height": h}))[:, :, :3]

    def ready(self) -> bool:
        """Positive gameplay check used only while waiting; never takes focus or clicks."""
        return self.has_focus() and is_playing(self.capture())

    def grab(self) -> tuple[bytes, int, int]:
        if not self.has_focus():
            raise GamePaused("等待你将游戏置于前台并手动恢复对局")
        shot = self.capture()
        if is_blocking_menu(shot):
            raise GamePaused("等待你手动进入或恢复游戏")
        ok, buf = cv2.imencode(".jpg", shot, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            raise RuntimeError("jpeg encode failed")
        return buf.tobytes(), shot.shape[1], shot.shape[0]

    async def execute(self, cmd: Command) -> dict | None:  # outcome unknown on the real game: None
        if cmd.action != "click":
            raise ValueError("real client accepts gameplay clicks only; game controls are manual")
        if not self.has_focus() or is_blocking_menu(self.capture()):
            raise GamePaused("游戏尚未就绪；已拦截输入，等待你手动恢复游戏")
        if cmd.action == "click":
            x0, y0, w, h = self.client_rect()
            if not (0 <= cmd.x < w and 0 <= cmd.y < h):
                raise ValueError(f"click ({cmd.x},{cmd.y}) outside the {w}x{h} game area")
            if not self.owns(x0 + cmd.x, y0 + cmd.y):
                raise RuntimeError(f"click ({cmd.x},{cmd.y}) refused: another window covers the game there")
            click_screen(x0 + cmd.x, y0 + cmd.y)


class ControlPanel:
    """Client lifecycle controls, independent of manual game controls."""

    def __init__(self, game: GameWindow) -> None:
        self.game = game
        self.paused = threading.Event()
        self.started = threading.Event()
        self.waiting = threading.Event()
        self.waiting.set()
        self.paused.set()
        self._ready_samples = 0
        self.stop = threading.Event()  # window/application shutdown only
        self.ended = False
        self.generation = 0
        self.run_id = 0
        self.link_active = threading.Event()
        self.message = "点开始启用客户端；游戏开始、暂停、重打由你手动操作"
        self._stats_process = psutil.Process(os.getpid())
        self._stats_time = time.monotonic()
        self._stats_link = None
        self._stats_bytes = (0, 0)
        self._upload = self._download = 0.0
        self._memory_mb = 0.0
        self._idle_activity = ModelActivity()
        self._input_lock = threading.RLock()
        self.link: ServerLink | None = None
        self.sessions: list[str] = []
        self.operator_view = None
        self.judge_state = None  # operator display + terminal guard only; never sent to player models
        self.awaiting_new_round = False
        self._thread = threading.Thread(target=self._run, name="control-panel", daemon=True)

    def start(self) -> None:
        self._thread.start()

    # ---- asyncio side ----

    def held(self) -> bool:
        return (not self.started.is_set() or self.paused.is_set()
                or self.waiting.is_set() or self.stop.is_set())

    def wait_for_game(self, reason="等待你手动进入或恢复游戏") -> None:
        self.waiting.set()
        self._ready_samples = 0
        if self.link:
            self.link.model_activity.freeze()
        self.message = reason

    def check_ready(self) -> bool:
        with self._input_lock:
            if not self.started.is_set() or self.paused.is_set() or self.stop.is_set():
                return False
            # The same confirmed outcome already stops ServerLink; never reconnect to an award lawn.
            if ((self.judge_state or {}).get("outcome") or
                    (self.awaiting_new_round and self.judge_state is None)):
                self._ready_samples = 0
                return False
            if self.game.ready():
                self._ready_samples += 1
            else:
                self._ready_samples = 0
            if self._ready_samples < 2:
                return False
            self.waiting.clear()
            self.awaiting_new_round = False
            self.message = "游戏画面已就绪；正在连接客户端"
            return True

    def grab(self, generation=None) -> tuple[bytes, int, int]:
        with self._input_lock:
            if self.held() or (generation is not None and generation != self.generation):
                raise GamePaused("capture held by operator")
            try:
                return self.game.grab()
            except GamePaused as e:
                log.info("capture hold reason: %s", e)
                self.wait_for_game(str(e))
                raise

    async def execute(self, cmd: Command, generation=None) -> dict | None:
        with self._input_lock:
            if self.held() or (generation is not None and generation != self.generation):
                raise RuntimeError("client held (not started, paused, or waiting for game)")
            try:
                return await self.game.execute(cmd)
            except GamePaused as e:
                log.info("input hold reason: %s", e)
                self.wait_for_game(str(e))
                raise

    # ---- Tk side ----

    def _run(self) -> None:
        import tkinter as tk
        from client.control_view import ControlView

        root = tk.Tk()
        root.title("PvZ 控制")
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._view = ControlView(root, {"开始": self._on_start, "暂停": self._on_pause,
                                        "继续": self._on_resume, "结束": self._on_stop})
        self._buttons = self._view.buttons
        root.update_idletasks()
        x0, y0, w, _ = self.game.client_rect()
        mon = win32api.GetMonitorInfo(win32api.MonitorFromWindow(self.game.hwnd))["Work"]
        x = x0 + w + 16 if x0 + w + 16 + root.winfo_width() <= mon[2] else max(mon[0], x0 - root.winfo_width() - 16)
        y = max(mon[1], min(y0, mon[3] - root.winfo_height()))
        root.geometry(f"+{x}+{y}")
        self._root = root
        self._tick()
        root.mainloop()
        self._root = self._view = self._buttons = None
        del root
        import gc
        gc.collect()

    def close(self) -> None:
        self._on_close()
        self._thread.join(timeout=3)

    def client_state(self) -> str:
        return ("已结束" if self.ended else "未开始" if not self.started.is_set()
                else "已暂停" if self.paused.is_set()
                else "等待游戏就绪" if self.waiting.is_set() else "运行中")

    def button_states(self) -> dict[str, bool]:
        active, paused = self.started.is_set(), self.paused.is_set()
        available = not self.stop.is_set()
        settled = not self.link_active.is_set()
        return {"开始": available and not active and settled,
                "暂停": available and active and not paused,
                "继续": available and active and paused and settled,
                "结束": available and active}

    def _tick(self) -> None:
        if self.stop.is_set():
            self._root.destroy()
            return
        now = time.monotonic()
        if self.link is not self._stats_link:
            self._stats_link = self.link
            self._stats_bytes = (0, 0)
            self._stats_time = now
            self._upload = self._download = 0.0
        if now - self._stats_time >= 1:
            counts = (self.link.bytes_sent, self.link.bytes_received) if self.link else (0, 0)
            self._upload, self._download = [(b-a)/(now-self._stats_time)/1024 for a,b in zip(self._stats_bytes, counts)]
            self._stats_bytes, self._stats_time = counts, now
            self._memory_mb = self._stats_process.memory_info().rss / 1024**2
        stats = self.link.latency_summary() if self.link else {"commands": 0}
        rtt = self.link.rtt_ms if self.link else None
        retained = bool(self.link and self.held())
        metrics = {
            "rtt": (f"{rtt:.0f} ms" if rtt is not None else "—", "上次网络往返" if retained else "网络往返延迟"),
            "latency": (f"{stats['latency_median_ms']:.0f} ms" if stats['commands'] else "—",
                        f"p95 {stats['latency_p95_ms']:.0f} ms · {stats['commands']}次点击" if stats['commands'] else "端到端 · 等待点击样本"),
            "memory": (f"{self._memory_mb:.1f} MiB", "控制客户端 RSS"),
            "traffic": (f"{self._upload:.1f} / {self._download:.1f}", "KiB/s · 对局应用数据"),
        }
        if self.link and self.link.session_id and self.link_active.is_set() and not self.held():
            if self.message == "游戏画面已就绪；正在连接客户端":
                self.message = "已连接；正在控制真实游戏"
        message = self.message
        if self.link and self.held() and self.link_active.is_set():
            message += " · 正在清理旧连接，输入已停止"
        elif self.link and not self.held():
            message += f" · 截图上传目标 {self.link.fps:g} 帧/秒"
        activity = self.link.model_activity if self.link else self._idle_activity
        samples = {stage: activity.snapshot(stage) for stage in ("scene", "zombies", "plants", "plan", "jev")}
        details = {}
        coach = "Coach · 胜负后自动复盘"
        if self.operator_view:
            details = {"decisions": self.operator_view.decisions_text(), "review": self.operator_view.review_text(),
                       "playbook": self.operator_view.playbook_text(), "mechanics": self.operator_view.mechanics_text()}
            coach = {"queued": "Coach · 等待复盘", "reviewing": "Coach · 正在复盘",
                     "completed": "Coach · 复盘已完成，可在赛后复盘页查看", "failed": "Coach · 复盘失败"}.get(self.round_review_status(), coach)
            coach += "\n" + self.operator_view.collection_text()
        self._view.update(self.client_state(), metrics, samples, message, coach, details,
                          retained, (self.judge_state or {}).get("outcome"))
        self._view.set_buttons(self.button_states())
        self._root.after(300, self._tick)

    def round_review_status(self):
        if not self.operator_view:
            return "not_triggered"
        review = self.operator_view.review
        if (self.link and getattr(self.link, "game_outcome", None)
                and review.get("session") != self.link.session_id):
            return "queued"
        return review.get("status", "not_triggered")

    def _on_pause(self) -> None:
        with self._input_lock:
            if not self.button_states()["暂停"]:
                return
            self.generation += 1
            self.paused.set()
            self.wait_for_game("客户端已暂停；点继续恢复客户端，游戏由你手动控制")

    def _on_start(self) -> None:
        with self._input_lock:
            if not self.button_states()["开始"]:
                return
            self.generation += 1
            self.ended = False
            self.run_id += 1
            self.started.set()
            self.wait_for_game()
            self.paused.clear()

    def _on_resume(self) -> None:
        with self._input_lock:
            if not self.button_states()["继续"]:
                return
            self.generation += 1
            self.wait_for_game()
            self.paused.clear()

    def _on_stop(self) -> None:
        with self._input_lock:
            if not self.button_states()["结束"]:
                return
            self.generation += 1
            self.started.clear()
            self.paused.set()
            self.ended = True
            self.wait_for_game("客户端已结束；窗口和记录保留，点开始可重新运行")

    def _on_close(self) -> None:
        with self._input_lock:
            self.generation += 1
            self.stop.set()
            self.started.clear()
            self.paused.set()
            self.wait_for_game("正在退出控制窗口")


async def run_link_until_held(link, done) -> None:
    """Also interrupt a connection/handshake that has not reached run's done loop."""
    task = asyncio.create_task(link.run(done=done))
    try:
        while not task.done() and not done():
            await asyncio.wait({task}, timeout=.05)
        if task.done():
            await task
            return
        # Give an established connection time to send its final episode report.
        await asyncio.wait({task}, timeout=.5)
        if task.done():
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    parser.add_argument("--token", default=os.environ.get("PVZ_TOKEN"),
                        help="defaults to $env:PVZ_TOKEN, so it stays out of the command line")
    parser.add_argument("--seconds", type=float, default=None, help="exit after this long; default: keep panel open until closed")
    parser.add_argument("--fps", type=float, default=5)
    parser.add_argument("--game-version", default="1.2.0.1096")
    parser.add_argument("--no-topmost", action="store_true", help="leave the window's z-order alone")
    args = parser.parse_args()
    if not args.token:
        parser.error("no token: pass --token or set $env:PVZ_TOKEN")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    instance = win32event.CreateMutex(None, False, "Local\\PvZHarnessWindowsClient")
    if win32api.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        instance.Close()
        log.warning("客户端已经运行，请切回已有的“PvZ 控制”窗口")
        existing = win32gui.FindWindow(None, "PvZ 控制")
        if existing:
            try:
                win32gui.SetForegroundWindow(existing)
            except Exception:
                pass
        return

    make_dpi_aware()
    game = GameWindow()
    mower_monitor = None
    monitor_attempted = False
    monitor_run = None
    if not args.no_topmost:
        game.set_topmost(True)
    panel = ControlPanel(game)
    panel.operator_view = OperatorView(args.url, args.token)
    operator_task = asyncio.create_task(panel.operator_view.run())
    async def update_judge_display():
        while True:
            if mower_monitor:
                panel.judge_state = mower_monitor.read_state()
            await asyncio.sleep(.1)
    judge_display_task = asyncio.create_task(update_judge_display())
    # Never take over the mouse or resume the game merely because the client launched.
    panel.paused.set()
    panel.start()
    deadline = time.monotonic() + args.seconds if args.seconds else None

    def expired() -> bool:
        return deadline is not None and time.monotonic() > deadline

    try:
        while True:
            if panel.stop.is_set() or expired():
                break
            if not panel.started.is_set():
                if mower_monitor:
                    mower_monitor.close()
                    mower_monitor = None
                monitor_attempted = False
                await asyncio.sleep(0.1)
                continue
            if monitor_run != panel.run_id:
                if mower_monitor:
                    mower_monitor.close()
                    mower_monitor = None
                monitor_attempted = False
                monitor_run = panel.run_id
            if not monitor_attempted:
                monitor_attempted = True
                try:
                    mower_monitor = MowerMonitor(game.process.pid)
                except (OSError, ValueError) as e:
                    log.warning("小推车裁判采集不可用：%s", e)
            if panel.paused.is_set():
                await asyncio.sleep(0.1)
                continue
            if panel.waiting.is_set():
                try:
                    ready = await asyncio.to_thread(panel.check_ready)
                except Exception as exc:
                    panel.message = f"等待游戏画面：{type(exc).__name__}"
                    ready = False
                if not ready:
                    await asyncio.sleep(0.2)
                    continue
            with panel._input_lock:
                if panel.held():
                    continue
                generation = panel.generation
                panel.link_active.set()
            link = ServerLink(args.url, args.token, "windows",
                              lambda g=generation: panel.grab(g),
                              lambda cmd, g=generation: panel.execute(cmd, g),
                              read_state=mower_monitor.read_state if mower_monitor else None,
                              fps=args.fps, game_version=args.game_version)
            panel.link = link
            try:
                await run_link_until_held(link, lambda: panel.held() or expired() or panel.generation != generation)
            except GamePaused as e:
                if panel.generation == generation:
                    panel.wait_for_game(str(e))
            except Exception as e:
                if panel.generation == generation:
                    panel._on_pause()
                    panel.message = f"连接中断（{type(e).__name__}）；检查服务后点“继续”"
                log.warning("session stopped: %s", e)
            finally:
                panel.link_active.clear()
            panel.sessions.append(link.session_id or "?")
            if getattr(link, "game_outcome", None):
                panel.awaiting_new_round = True
                if panel.generation == generation:
                    panel.wait_for_game("本局结束；Coach自动复盘。请手动领奖或重打，客户端等待游戏就绪")
            print(link.session_id, link.latency_summary(), flush=True)
            if panel.stop.is_set() or expired():
                break
    finally:
        for task in (operator_task, judge_display_task):
            task.cancel()
        await asyncio.gather(operator_task, judge_display_task, return_exceptions=True)
        if mower_monitor:
            mower_monitor.close()
        panel.close()
        if not args.no_topmost:
            game.set_topmost(False)
        instance.Close()
    print("sessions:", ", ".join(panel.sessions))


if __name__ == "__main__":
    asyncio.run(main())
