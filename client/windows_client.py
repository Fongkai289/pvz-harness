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
import base64
import asyncio
import ctypes
import logging
import os
import threading
import time

import cv2
import httpx
import mss
import numpy as np
import psutil
import pydirectinput
import win32api
import win32con
import win32event
import win32gui
import win32process
import websockets

from client import app_window
from client.link import ServerLink
from client.model_activity import ModelActivity, activity_lines
from client.mower_state import MowerMonitor
from client.operator_view import OperatorView
from client.menu import is_playing, is_blocking_menu, is_seed_chooser
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


NO_GAME = "游戏还没打开：可以先看复盘、策略和测试连接；打开游戏（窗口模式）后客户端会自动接上"


class MissingGame:
    """Stands in for PvZ until it is opened, so the control center works without the game.

    Never ready, never captures, never clicks; main() swaps in the real GameWindow once it appears.
    """
    hwnd = 0
    process = None

    def ready(self) -> bool:
        return False

    def has_focus(self) -> bool:
        return False

    def owns(self, x: int, y: int) -> bool:
        return False

    def set_topmost(self, on: bool) -> None:
        pass

    def client_rect(self):
        raise GamePaused(NO_GAME)

    def capture(self):
        raise GamePaused(NO_GAME)

    def grab(self):
        raise GamePaused(NO_GAME)

    async def execute(self, cmd):
        raise GamePaused(NO_GAME)


def find_game() -> GameWindow | MissingGame:
    try:
        return GameWindow()
    except RuntimeError:  # not open yet, or minimized
        return MissingGame()


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
        self.choosing = False  # 选植物 in progress
        self.testing = False  # 测试连接 in progress
        self.diagnostics = None  # last 测试连接 result for the control center
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

    # ---- control window side ----

    def _run(self) -> None:
        """Publishes state to the control center page and runs its button clicks; the page is the only UI."""
        from client.app_window import AppWindow, ensure_control_center
        from client.panel_bridge import PanelBridge
        try:
            self._web_bridge = PanelBridge()
        except OSError:
            log.exception("控制入口（127.0.0.1:8767）启动失败；客户端退出")
            self._on_close()
            return
        window = None
        try:
            if not ensure_control_center():
                log.error("控制中心 http://127.0.0.1:8766 没有启动，见 logs/dashboard.err")
            if self.game.hwnd:
                mon = win32api.GetMonitorInfo(win32api.MonitorFromWindow(self.game.hwnd))["Work"]
                rect = self.game.client_rect()
            else:  # no game yet: the primary screen's left edge, where the game usually opens beside it
                mon = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY))["Work"]
                rect = (mon[0], mon[1], 0, 0)
            window = AppWindow.open(self.game.hwnd, rect, mon)
        except Exception:
            log.exception("控制窗口打不开；请在浏览器打开 http://127.0.0.1:8766/client")
        try:
            while not self.stop.is_set():
                self._tick()
                if window and not window.is_open():
                    log.info("控制窗口已关闭；客户端退出")
                    self._on_close()
                time.sleep(.3)
        finally:
            self._web_bridge.close()
            if window:
                window.close()

    def close(self) -> None:
        self._on_close()
        self._thread.join(timeout=6)

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
                "结束": available and active,
                # Not part of a session: never while a connection may click, never twice at once.
                "选植物": available and settled and not self.choosing and self.operator_view is not None and bool(self.game.hwnd),
                # Before a game: never adds model calls or traffic to a running one.
                "测试连接": available and settled and not self.testing and self.operator_view is not None}

    def _tick(self) -> None:
        bridge = getattr(self, '_web_bridge', None)
        if bridge:
            bridge.drain(self)
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
            if self.message == "游戏画面已就绪；正在连接客户端" or self.message.startswith("网络中断"):
                self.message = "已连接；正在控制真实游戏"
        message = self.message
        if self.link and self.held() and self.link_active.is_set():
            message += " · 正在清理旧连接，输入已停止"
        elif self.link and not self.held():
            sent = self.link.upload_fps()
            message += f" · 截图上传 {sent:.1f}/{self.link.fps:g} 帧/秒"
            if self.link.frame_times and sent < self.link.fps * .6:
                message += "（网络慢，已自动降帧，画面不积压）"
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
        if bridge:
            bridge.snapshot = dict(online=True, updated=time.time(), state=self.client_state(),
                                   generation=self.generation, buttons=self.button_states(),
                                   metrics=metrics, samples=samples, message=message, coach=coach,
                                   details=details, retained=retained, diagnostics=self.diagnostics,
                                   review=self.operator_view.review_view() if self.operator_view else None,
                                   reviews=self.operator_view.review_views() if self.operator_view else [],
                                   terminal=(self.judge_state or {}).get('outcome'))

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

    def _on_choose_seeds(self) -> None:
        with self._input_lock:
            if not self.button_states()["选植物"]:
                return
            self.choosing = True
            self.message = "正在选植物：读取选卡界面，请 Qwen 挑选…"
        threading.Thread(target=self._choose_seeds, name="choose-seeds", daemon=True).start()

    def _choose_seeds(self) -> None:
        try:
            self.message = choose_seeds(self.game, self.operator_view, self._input_lock, self.link_active)
        except Exception as exc:
            log.exception("seed choice failed")
            self.message = f"选植物失败：{type(exc).__name__}"
        finally:
            self.choosing = False

    def _on_test_links(self) -> None:
        with self._input_lock:
            if not self.button_states()["测试连接"]:
                return
            self.testing = True
            self.diagnostics = {"time": time.time(), "running": True, "tiles": {}}
            self.message = "正在测试连接：SSH 隧道、上传带宽、Qwen、OneJev…"
        threading.Thread(target=self._test_links, name="test-links", daemon=True).start()

    def _test_links(self) -> None:
        try:
            self.diagnostics = run_diagnostics(self.operator_view)
            failed = [k for k, v in self.diagnostics["tiles"].items() if v[2] == "failed"]
            self.message = ("连接测试通过，可以开始游戏" if not failed else
                            "连接测试有问题：" + "、".join(DIAGNOSTIC_NAMES[k] for k in failed))
        except Exception as exc:
            log.exception("connection test failed")
            self.diagnostics = None
            self.message = f"连接测试出错：{type(exc).__name__}"
        finally:
            self.testing = False

    def _on_close(self) -> None:
        with self._input_lock:
            self.generation += 1
            self.stop.set()
            self.started.clear()
            self.paused.set()
            self.wait_for_game("正在退出控制窗口")


DIAGNOSTIC_NAMES = {"tunnel": "SSH 隧道", "upload": "上传带宽", "qwen": "Qwen", "jev": "OneJev"}
UPLOAD_PROBE = 256 * 1024  # bytes; a live screenshot is about 100 KiB
FRAME_KIB = 100


def run_diagnostics(view: OperatorView) -> dict:
    """测试连接 before a game: tunnel round trip and upload from here; each model is timed by the server.

    Tiles are {key: [value, note, ok|failed]}; a failed tunnel skips the rest (nothing else can pass).
    """
    auth = {"Authorization": f"Bearer {view.token}"}
    tiles = {}
    with httpx.Client(timeout=30, trust_env=False) as client:
        try:
            times = []
            for _ in range(3):
                started = time.monotonic()
                client.get(view.base + "/health").raise_for_status()
                times.append((time.monotonic() - started) * 1000)
            times.sort()
            tiles["tunnel"] = [f"{times[1]:.0f} ms", f"本机 ↔ 服务器往返 · 3 次中位数，最慢 {times[-1]:.0f} ms", "ok"]
        except httpx.HTTPError as exc:
            tiles["tunnel"] = ["失败", f"连不上服务器：{type(exc).__name__}；检查 SSH 隧道", "failed"]
            for key in ("upload", "qwen", "jev"):
                tiles[key] = ["—", "隧道不通，未测试", "failed"]
            return {"time": time.time(), "running": False, "tiles": tiles}
        try:
            started = time.monotonic()
            client.post(view.base + "/diagnostics/upload", content=os.urandom(UPLOAD_PROBE),
                        headers=auth).raise_for_status()
            rate = UPLOAD_PROBE / 1024 / max(time.monotonic() - started, 0.001)
            tiles["upload"] = [f"{rate:.0f} KiB/s", f"上传 256 KiB · 约 {rate / FRAME_KIB:.1f} 张截图/秒", "ok"]
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            tiles["upload"] = ["失败", "令牌不对（401）" if status == 401 else f"{type(exc).__name__}", "failed"]
        try:
            response = client.post(view.base + "/diagnostics", headers=auth, timeout=90)
            response.raise_for_status()
            for key, result in response.json().items():
                tiles[key] = ([f"{result['ms']} ms", f"一次最小文本调用 · {result['detail']}", "ok"] if result["ok"]
                              else ["失败", result["detail"], "failed"])
        except httpx.HTTPError as exc:
            for key in ("qwen", "jev"):
                tiles[key] = ["失败", f"服务器测试未返回：{type(exc).__name__}", "failed"]
    return {"time": time.time(), "running": False, "tiles": tiles}


CHOOSER_GRID = (22, 128, 446, 491)  # x0, y0, x1, y1 of the seed chooser's packets (server/seed_choice.py)
CHOOSER_TITLE = (230, 103)  # inside the "Choose your plants" band (client/menu.is_seed_chooser): not a button


def focus_for_operator(hwnd) -> None:
    """Bring the game forward for an explicit operator action (选植物); gameplay never takes focus.

    The operator just clicked the control window, so Windows refuses a plain SetForegroundWindow
    from this process; sharing the foreground thread's input state for the call lets it through.
    """
    foreground = win32gui.GetForegroundWindow()
    if foreground == hwnd:
        return
    theirs = win32process.GetWindowThreadProcessId(foreground)[0] if foreground else 0
    ours = win32api.GetCurrentThreadId()
    user32 = ctypes.windll.user32
    attached = bool(theirs) and theirs != ours and bool(user32.AttachThreadInput(theirs, ours, True))
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as exc:
        log.info("could not focus the game: %s", exc)
    finally:
        if attached:
            user32.AttachThreadInput(theirs, ours, False)
    time.sleep(0.3)


def choose_seeds(game: GameWindow, view: OperatorView, lock, link_active: threading.Event) -> str:
    """选植物, outside any game session: the server reads the chooser and Qwen picks; this clicks the packets.

    Every click re-checks the chooser screen and focus first. The operator starts the level.
    """
    shot = game.capture()  # the game window is topmost, so the screen shows it even without focus
    if not is_seed_chooser(shot):
        return "选植物：游戏现在不是选卡界面"
    focus_for_operator(game.hwnd)
    if not game.has_focus():
        # Windows keeps the foreground on the control window the operator just clicked; a click on the
        # chooser's title band, which does nothing in the game, activates the game the way a person would.
        x0, y0, _, _ = game.client_rect()
        if not game.owns(x0 + CHOOSER_TITLE[0], y0 + CHOOSER_TITLE[1]):
            return "选植物：有别的窗口挡住了游戏；请把游戏露出来，再点选植物"
        click_screen(x0 + CHOOSER_TITLE[0], y0 + CHOOSER_TITLE[1])
        time.sleep(0.4)
    if not game.has_focus():
        return "选植物：游戏窗口没能切到前台；请点一下游戏窗口，再点选植物"
    shot = game.capture()
    if not is_seed_chooser(shot):
        return "选植物：游戏现在不是选卡界面"
    jpeg = cv2.imencode(".jpg", shot, [cv2.IMWRITE_JPEG_QUALITY, 90])[1]
    response = httpx.post(view.base + "/seed_choice", json={"jpeg": base64.b64encode(jpeg).decode()},
                          headers={"Authorization": f"Bearer {view.token}"}, timeout=90, trust_env=False)
    if response.status_code in (409, 502, 503):
        return "选植物：" + str(response.json().get("detail", response.status_code))
    response.raise_for_status()
    choice = response.json()
    x0, y0, _, _ = game.client_rect()
    for click in choice["clicks"]:
        x, y = int(click["x"]), int(click["y"])
        if not (CHOOSER_GRID[0] <= x < CHOOSER_GRID[2] and CHOOSER_GRID[1] <= y < CHOOSER_GRID[3]):
            return f"选植物：服务器给的位置 ({x},{y}) 不在选卡区，已停止"
        with lock:
            if link_active.is_set() or not game.has_focus() or not is_seed_chooser(game.capture()):
                return "选植物：游戏离开了选卡界面或失去焦点，已停止"
            if not game.owns(x0 + x, y0 + y):
                return "选植物：有别的窗口挡住了选卡区，已停止"
            click_screen(x0 + x, y0 + y)
        time.sleep(0.6)  # the packet flies to the seed bank
    zombies = "、".join(choice.get("zombies") or []) or "—"
    return (f"已选：{'、'.join(choice['plants'])}（看到的僵尸：{zombies}）。{choice.get('reason', '')}"
            " 检查后请自己点“一起摇滚吧”开始。")


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


def needs_operator(error: Exception) -> bool:
    """Only a refused hello or handshake needs a person; any other drop is the network and retries itself."""
    close = getattr(error, "rcvd", None)
    return getattr(close, "code", None) == 1008 or isinstance(error, websockets.exceptions.InvalidStatus)


def disconnect_message(error: Exception) -> str:
    """A policy close (1008) is the server refusing the hello, usually a wrong PVZ_TOKEN; say so."""
    close = getattr(error, "rcvd", None)
    if getattr(close, "code", None) == 1008:
        reason = getattr(close, "reason", "") or "hello rejected"
        hint = "：令牌不对，请用“启动客户端”重新启动（会从服务器读取令牌）" if "token" in reason else ""
        return f"服务器拒绝连接（{reason}）{hint}"
    return f"连接中断（{type(error).__name__}）；检查服务后点“继续”"


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
        log.warning("客户端已经运行，请切回已有的控制窗口（PvZ 控制中心）")
        existing = win32gui.FindWindow(None, app_window.TITLE)
        if existing:
            try:
                win32gui.SetForegroundWindow(existing)
            except Exception:
                pass
        return

    make_dpi_aware()
    game = find_game()
    mower_monitor = None
    monitor_attempted = False
    monitor_run = None
    if not args.no_topmost:
        game.set_topmost(True)
    panel = ControlPanel(game)
    if not game.hwnd:
        panel.message = NO_GAME
    game_checked = 0.0
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

    retries, reconnect_at = 0, 0.0
    try:
        while True:
            if panel.stop.is_set() or expired():
                break
            if not panel.game.hwnd and time.monotonic() - game_checked >= 1:
                game_checked = time.monotonic()
                found = find_game()
                if found.hwnd:  # the game was opened after the client: attach to it
                    with panel._input_lock:
                        panel.game = found
                        if panel.message == NO_GAME:
                            panel.message = "游戏已打开，客户端已接上；进入关卡后点开始"
                    if not args.no_topmost:
                        found.set_topmost(True)
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
                    mower_monitor = MowerMonitor(panel.game.process.pid)
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
            if time.monotonic() < reconnect_at:  # backing off after a network drop; pause/stop still work
                await asyncio.sleep(0.1)
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
                    if needs_operator(e):
                        panel._on_pause()
                        panel.message = disconnect_message(e)
                    else:
                        retries = 0 if link.session_id else retries
                        retries += 1
                        delay = min(15, 2 ** retries)
                        reconnect_at = time.monotonic() + delay
                        panel.message = f"网络中断（{type(e).__name__}）；{delay} 秒后自动重连（第 {retries} 次）"
                log.warning("session stopped: %r", e)
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
            panel.game.set_topmost(False)
        instance.Close()
    print("sessions:", ", ".join(panel.sessions))


if __name__ == "__main__":
    asyncio.run(main())
