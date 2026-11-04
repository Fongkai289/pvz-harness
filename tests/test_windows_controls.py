"""Client-only controls, tested without manipulating the real game or mouse."""
import asyncio
import time
import sys
from unittest.mock import Mock

import numpy as np
import pytest

pytest.importorskip("win32gui")
from client import windows_client as wc
from common.protocol import Command


def enabled_panel(game=None):
    game = game or Mock()
    game.ready.return_value = True
    panel = wc.ControlPanel(game)
    panel._on_start()
    assert not panel.check_ready()
    assert panel.check_ready()
    return panel


def test_four_buttons_control_only_client_lifecycle():
    game = Mock()
    panel = wc.ControlPanel(game)
    assert panel.held() and not panel.started.is_set()
    panel._on_resume()  # cannot continue a client that was never started
    assert not panel.started.is_set()
    panel._on_start()
    assert panel.started.is_set() and panel.waiting.is_set() and not panel.paused.is_set()
    panel._on_start()  # idempotent
    panel._on_pause()
    assert panel.paused.is_set()
    panel._on_resume()
    assert not panel.paused.is_set() and panel.waiting.is_set()
    panel._on_stop()
    assert panel.ended and not panel.started.is_set() and not panel.stop.is_set()
    panel._on_resume()  # ended clients must use Start, not Continue
    assert not panel.started.is_set()
    panel._on_start()
    assert panel.started.is_set() and not panel.ended
    panel._on_close()
    assert panel.stop.is_set()
    game.assert_not_called()
    assert not game.mock_calls  # no menu click, focus stealing, suspend or resume


def test_start_waits_for_stable_playing_after_manual_game_start():
    game = Mock()
    game.ready.side_effect = [False, True, False, True, True]
    panel = wc.ControlPanel(game)
    panel._on_start()
    for _ in range(4):
        assert not panel.check_ready() and panel.held()
    assert panel.check_ready() and not panel.held()
    assert not hasattr(wc.GameWindow, "start_from_menu")
    assert not hasattr(wc.GameWindow, "take_focus")
    assert not hasattr(wc.GameWindow, "retry_after_loss")


def test_explicit_client_pause_stays_latched_even_when_game_is_playable():
    panel = enabled_panel()
    panel.link = Mock()
    panel._on_pause()
    panel.game.reset_mock()
    assert not panel.check_ready()
    panel.game.ready.assert_not_called()
    panel.link.model_activity.freeze.assert_called_once()
    with pytest.raises(RuntimeError):
        panel.grab()
    with pytest.raises(RuntimeError):
        asyncio.run(panel.execute(Command(cmd_id=1, action="click", x=10, y=10)))
    panel._on_resume()
    assert not panel.check_ready()
    assert panel.check_ready()


@pytest.mark.parametrize("source", ["capture", "input"])
def test_game_pause_or_focus_loss_waits_and_resumes_only_after_manual_recovery(source):
    panel = enabled_panel()
    if source == "capture":
        panel.game.grab.side_effect = wc.GamePaused("menu")
        operation = panel.grab
    else:
        async def blocked(cmd):
            raise wc.GamePaused("focus lost")
        panel.game.execute = blocked
        operation = lambda: asyncio.run(panel.execute(Command(cmd_id=1, action="click", x=10, y=10)))
    with pytest.raises(wc.GamePaused):
        operation()
    assert panel.waiting.is_set() and not panel.paused.is_set()
    panel.game.ready.return_value = False
    assert not panel.check_ready()
    panel.game.ready.return_value = True
    assert not panel.check_ready()
    assert panel.check_ready()


@pytest.mark.parametrize("action", ["pause", "resume", "set_speed", "noop"])
def test_real_client_refuses_all_game_control_commands(action):
    game = wc.GameWindow.__new__(wc.GameWindow)
    game.process = Mock()
    game.capture = Mock()
    with pytest.raises(ValueError, match="game controls are manual"):
        asyncio.run(game.execute(Command(cmd_id=1, action=action)))
    assert not game.process.mock_calls
    game.capture.assert_not_called()


def test_gameplay_capture_and_readiness_never_take_focus_or_click(monkeypatch):
    game = wc.GameWindow.__new__(wc.GameWindow)
    game.capture = Mock(return_value=np.zeros((600, 800, 3), dtype=np.uint8))
    game.has_focus = Mock(return_value=False)
    assert not game.ready()
    game.capture.assert_not_called()
    game.has_focus.return_value = True
    monkeypatch.setattr(wc, "is_playing", lambda _: False)
    monkeypatch.setattr(wc, "is_blocking_menu", lambda _: True)
    click = Mock()
    monkeypatch.setattr(wc, "click_screen", click)
    assert not game.ready()
    with pytest.raises(wc.GamePaused):
        game.grab()
    click.assert_not_called()


@pytest.mark.parametrize("outcome", ["win", "lose"])
def test_terminal_lawn_cannot_reconnect_until_manual_new_round(outcome):
    panel = enabled_panel()
    panel.wait_for_game()
    panel.awaiting_new_round = True
    panel.judge_state = {"outcome": outcome}
    panel.game.reset_mock()
    assert not panel.check_ready()
    panel.game.ready.assert_not_called()
    panel.judge_state = None  # transient sampler failure does not release the terminal hold
    assert not panel.check_ready()
    panel.judge_state = {"outcome": None}
    assert not panel.check_ready()
    assert panel.check_ready() and not panel.awaiting_new_round


def test_main_reconnects_after_manual_game_recovery_without_game_commands(monkeypatch):
    game = Mock()
    game.ready.return_value = True
    calls = []
    class Panel(wc.ControlPanel):
        def start(self):
            self._on_start()
        def close(self):
            self._on_close()
    panel = Panel(game)
    class Link:
        game_outcome = None
        model_activity = Mock()
        def __init__(self, *args, **kwargs):
            self.session_id = str(len(calls))
        async def run(self, *, done):
            assert not panel.held()
            calls.append("session")
            if len(calls) == 1:
                panel.wait_for_game("manual pause then resume")
            else:
                panel._on_close()
            assert done()
        def latency_summary(self):
            return {}
    monkeypatch.setattr(sys, "argv", ["windows_client", "--token", "test-only"])
    monkeypatch.setattr(wc.win32event, "CreateMutex", Mock(return_value=Mock()))
    monkeypatch.setattr(wc.win32api, "GetLastError", lambda: 0)
    monkeypatch.setattr(wc, "make_dpi_aware", lambda: None)
    monkeypatch.setattr(wc, "GameWindow", lambda: game)
    monkeypatch.setattr(wc, "MowerMonitor", Mock(side_effect=OSError("not available")))
    monkeypatch.setattr(wc, "ControlPanel", lambda _: panel)
    monkeypatch.setattr(wc, "ServerLink", Link)
    asyncio.run(wc.main())
    assert calls == ["session", "session"]
    game.process.resume.assert_not_called()
    game.process.suspend.assert_not_called()
    game.take_focus.assert_not_called()


def test_button_states_and_end_preserve_window_and_results():
    panel = wc.ControlPanel(Mock())
    assert panel.client_state() == "未开始"
    assert panel.button_states() == {"开始": True, "暂停": False, "继续": False, "结束": False, "选植物": False, "测试连接": False}
    panel._on_start()
    assert panel.button_states() == {"开始": False, "暂停": True, "继续": False, "结束": True, "选植物": False, "测试连接": False}
    panel.link = Mock()
    panel.sessions.append("previous")
    panel.link_active.set()
    panel._on_pause()
    assert not panel.button_states()["暂停"] and not panel.button_states()["继续"]
    panel._on_resume()  # not until the previous connection has shut down
    assert panel.paused.is_set()
    panel.link_active.clear()
    assert panel.button_states()["继续"]
    panel._on_resume()
    panel._on_stop()
    assert panel.client_state() == "已结束" and panel.held()
    assert panel.button_states() == {"开始": True, "暂停": False, "继续": False, "结束": False, "选植物": False, "测试连接": False}
    assert panel.sessions == ["previous"] and panel.link is not None
    assert not panel.stop.is_set()
    panel._on_start()
    assert panel.client_state() == "等待游戏就绪"


@pytest.mark.parametrize("transition", ["pause", "end"])
def test_previous_connection_cannot_capture_or_click_after_restart(transition):
    panel = enabled_panel()
    previous = panel.generation
    if transition == "pause":
        panel._on_pause()
        panel._on_resume()
    else:
        panel._on_stop()
        panel._on_start()
    assert not panel.check_ready()
    assert panel.check_ready()
    panel.game.reset_mock()
    with pytest.raises(wc.GamePaused):
        panel.grab(previous)
    with pytest.raises(RuntimeError):
        asyncio.run(panel.execute(Command(cmd_id=9, action="click", x=10, y=10), previous))
    assert not panel.game.mock_calls


def test_end_cancels_a_stalled_connection_without_closing_window():
    async def check():
        panel = enabled_panel()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        class Link:
            async def run(self, *, done):
                entered.set()
                try:
                    await asyncio.Event().wait()  # simulates waiting for a server handshake
                finally:
                    cancelled.set()
        task = asyncio.create_task(wc.run_link_until_held(Link(), panel.held))
        await entered.wait()
        panel._on_stop()
        await asyncio.wait_for(task, timeout=2)
        assert cancelled.is_set() and not panel.stop.is_set() and panel.ended
    asyncio.run(check())


def test_main_can_end_and_restart_and_releases_old_judge_monitor(monkeypatch):
    game = Mock()
    game.ready.return_value = True
    monitors = [Mock(), Mock()]
    for monitor in monitors:
        monitor.read_state.return_value = None
    factory = Mock(side_effect=monitors)
    calls = []
    class Panel(wc.ControlPanel):
        def start(self): self._on_start()
        def close(self): self._on_close()
    panel = Panel(game)
    async def restart():
        while panel.link_active.is_set():
            await asyncio.sleep(.01)
        assert panel.client_state() == "已结束" and not panel.stop.is_set()
        panel._on_start()
    class Link:
        game_outcome = None
        model_activity = Mock()
        session_id = "test"
        def __init__(self, *args, **kwargs): pass
        async def run(self, *, done):
            calls.append("session")
            if len(calls) == 1:
                panel._on_stop()
                asyncio.create_task(restart())
                raise OSError("late disconnect after End")
            assert monitors[0].close.call_count == 1
            panel._on_close()
        def latency_summary(self): return {}
    monkeypatch.setattr(sys, "argv", ["windows_client", "--token", "test-only"])
    monkeypatch.setattr(wc.win32event, "CreateMutex", Mock(return_value=Mock()))
    monkeypatch.setattr(wc.win32api, "GetLastError", lambda: 0)
    monkeypatch.setattr(wc, "make_dpi_aware", lambda: None)
    monkeypatch.setattr(wc, "GameWindow", lambda: game)
    monkeypatch.setattr(wc, "MowerMonitor", factory)
    monkeypatch.setattr(wc, "ControlPanel", lambda _: panel)
    monkeypatch.setattr(wc, "ServerLink", Link)
    asyncio.run(wc.main())
    assert len(calls) == 2 and factory.call_count == 2
    for monitor in monitors:
        monitor.close.assert_called_once()


def test_second_client_exits_before_taking_over_game(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["windows_client", "--token", "test-only"])
    mutex = Mock()
    monkeypatch.setattr(wc.win32event, "CreateMutex", Mock(return_value=mutex))
    monkeypatch.setattr(wc.win32api, "GetLastError", lambda: 183)
    monkeypatch.setattr(wc.win32gui, "FindWindow", lambda *_: 0)
    game = Mock()
    monkeypatch.setattr(wc, "GameWindow", game)
    asyncio.run(wc.main())
    game.assert_not_called()
    mutex.Close.assert_called_once()


def test_a_rejected_token_is_named_instead_of_a_bare_disconnect():
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close
    rejected = ConnectionClosedError(Close(1008, "bad hello or token"), None)
    assert "令牌不对" in wc.disconnect_message(rejected)
    assert "连接中断（ConnectionClosedError）" in wc.disconnect_message(ConnectionClosedError(None, None))
    assert "连接中断（OSError）" in wc.disconnect_message(OSError())


def test_control_window_sits_beside_the_game_inside_the_work_area():
    from client.app_window import placement
    work = (0, 0, 1920, 1040)
    assert placement((100, 50, 800, 600), work) == (916, 50, 760, 900)  # right of the game
    assert placement((1100, 50, 800, 600), work) == (324, 50, 760, 900)  # no room on the right
    assert placement((100, 500, 800, 600), (0, 0, 1366, 728)) == (916, 0, 450, 728)  # clamped to a small screen
    assert placement((100, 50, 1400, 1050), (0, 0, 3840, 2100), 1.75) == (1516, 50, 1330, 1575)  # high DPI


def test_control_window_title_matches_the_page():
    from client.app_window import ROOT, TITLE
    assert f"<title>{TITLE}</title>" in (ROOT / "client/operator_dashboard.html").read_text(encoding="utf-8")


def test_a_network_drop_reconnects_by_itself_but_a_refused_token_waits_for_the_operator(monkeypatch):
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close
    game = Mock()
    game.ready.return_value = True
    calls, seen = [], []
    class Panel(wc.ControlPanel):
        def start(self):
            self._on_start()
        def close(self):
            self._on_close()
    panel = Panel(game)
    class Link:
        game_outcome = None
        model_activity = Mock()
        def __init__(self, *args, **kwargs):
            self.session_id = None
        async def run(self, *, done):
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise TimeoutError()  # handshake lost in a stalled tunnel
            seen.append((panel.paused.is_set(), panel.message))
            if len(calls) == 2:
                raise ConnectionClosedError(Close(1008, "bad hello or token"), None)
        def latency_summary(self):
            return {}
    async def stop_when_paused():
        while not panel.paused.is_set() or len(calls) < 2:
            await asyncio.sleep(.05)
        seen.append(panel.message)
        panel._on_close()
    async def run():
        task = asyncio.create_task(stop_when_paused())
        await wc.main()
        await task
    monkeypatch.setattr(sys, "argv", ["windows_client", "--token", "test-only"])
    monkeypatch.setattr(wc.win32event, "CreateMutex", Mock(return_value=Mock()))
    monkeypatch.setattr(wc.win32api, "GetLastError", lambda: 0)
    monkeypatch.setattr(wc, "make_dpi_aware", lambda: None)
    monkeypatch.setattr(wc, "GameWindow", lambda: game)
    monkeypatch.setattr(wc, "MowerMonitor", Mock(side_effect=OSError("not available")))
    monkeypatch.setattr(wc, "ControlPanel", lambda _: panel)
    monkeypatch.setattr(wc, "ServerLink", Link)
    asyncio.run(run())
    assert len(calls) == 2 and calls[1] - calls[0] >= 1.9  # retried after the 2 s backoff, no click needed
    assert seen[0] == (False, "网络中断（TimeoutError）；2 秒后自动重连（第 1 次）")
    assert "令牌不对" in seen[-1]  # paused for the operator, no automatic retry


def test_client_runs_without_the_game_and_attaches_when_it_opens(monkeypatch):
    game = Mock()
    game.hwnd = 42
    game.ready.return_value = False
    lookups, seen = [], []
    def window():
        lookups.append(time.monotonic())
        if len(lookups) < 3:
            raise RuntimeError("window not found: MainWindow 'Plants vs. Zombies'")
        return game
    class Panel(wc.ControlPanel):
        def start(self):
            seen.append((type(self.game).__name__, self.message, self.button_states()["选植物"]))
        def close(self):
            self._on_close()
    panels = []
    monkeypatch.setattr(sys, "argv", ["windows_client", "--token", "test-only"])
    monkeypatch.setattr(wc.win32event, "CreateMutex", Mock(return_value=Mock()))
    monkeypatch.setattr(wc.win32api, "GetLastError", lambda: 0)
    monkeypatch.setattr(wc, "make_dpi_aware", lambda: None)
    monkeypatch.setattr(wc, "GameWindow", window)
    monkeypatch.setattr(wc, "ControlPanel", lambda g: panels.append(Panel(g)) or panels[-1])
    async def run():
        async def close_once_attached():
            while not panels or panels[0].game is not game:
                await asyncio.sleep(.05)
            seen.append(panels[0].message)
            panels[0]._on_close()
        task = asyncio.create_task(close_once_attached())
        await asyncio.wait_for(wc.main(), timeout=10)
        await task
    asyncio.run(run())
    assert seen[0] == ("MissingGame", wc.NO_GAME, False)  # the control center runs; no seed clicks without a game
    assert panels[0].game is game and "已接上" in seen[1]
    game.set_topmost.assert_any_call(True)
