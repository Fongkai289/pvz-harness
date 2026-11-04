"""Windows-only pvz_rl adapter. No DQN, no Emulator, no personal-game attachment."""
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

from rsi.native_jobs import UPSTREAM
from server.scene import Scene, Card, Zombie

PLANTS = ("peashooter", "sunflower", "cherry bomb", "wall-nut", "potato mine", "snow pea", "chomper", "repeater")
ZOMBIES = ("normal", "flag", "conehead", "pole vaulting", "buckethead", "newspaper", "screen door", "football",
           "dancing", "backup dancer", "ducky tube", "snorkel", "zomboni", "bobsled", "dolphin rider",
           "jack-in-the-box", "balloon", "digger", "pogo", "unknown", "bungee", "ladder", "catapult", "gargantuar", "imp")


def preflight(config):
    """Only inspect source identity, executable bytes and file presence; no DLL loading."""
    from common.project_paths import resolve_runtime_paths
    config = resolve_runtime_paths(config)
    if config.get('backend')=='pvz_portable':
        from rsi.portable_runtime import preflight as portable_preflight
        return portable_preflight(config)
    repo = Path(config["repository"]).resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if revision != UPSTREAM:
        raise RuntimeError("pvz_rl revision differs from the reviewed adapter contract")
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo, text=True).strip()
    if dirty:
        raise RuntimeError("pvz_rl tracked files changed; adapter needs revalidation")
    if not config.get("game_executable"):
        raise RuntimeError("Missing complete retail 1.2.0.1065 game; GOTY is unsupported")
    path = repo / "python/pvz_gym/runtime/game_validation.py"
    spec = importlib.util.spec_from_file_location("_native_pvz_validation", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.validate_game(config["game_executable"])
    dll = Path(config.get("dll") or repo / "bin/pvz_gym_native.dll").resolve()
    if not dll.is_file():
        raise RuntimeError("pvz_rl native DLL has not been built/installed")
    if module.read_pe(dll).machine != 0x8664:
        raise RuntimeError("pvz_rl native DLL must be x64")
    checksum = hashlib.sha256(dll.read_bytes()).hexdigest()
    metadata = json.loads(dll.with_name("metadata.json").read_text(encoding="utf-8"))
    if (metadata.get("source_commit") != UPSTREAM or metadata.get("dll_sha256") != checksum
            or metadata.get("runtime_interface_version") != 1 or metadata.get("game_version") != "1.2.0.1065"):
        raise RuntimeError("pvz_rl DLL manifest does not match reviewed source/game/interface")
    return {"upstream": revision, "game_sha256": hashlib.sha256(Path(config["game_executable"]).read_bytes()).hexdigest(),
            "dll_sha256": checksum}


def player_state(raw, mask, tick):
    """Whitelist public battle facts; no spawn plans, seed or judge diagnostics."""
    if raw.get('backend')=='pvz_portable':
        from rsi.portable_runtime import player_state as portable_state
        return portable_state(raw,mask,tick)
    g = raw["global_state"]
    if g.get("scene") != 0 or g.get("grid_rows") != 5:
        raise ValueError("Initial adapter supports ordinary five-row daytime levels only")
    cards = sorted(raw["cards"], key=lambda c:c["slot"])
    if [c["slot"] for c in cards] != list(range(len(cards))) or not 0 < len(cards) <= 10:
        raise ValueError("unsupported/noncontiguous seed bank")
    if any(type(c["plant_type"]) is not int or not 0 <= c["plant_type"] < len(PLANTS) for c in cards):
        raise ValueError("seed type outside validated day-level loadout")
    if len(mask) != 757 or not all(type(x) is bool for x in mask):
        raise ValueError("wrong native action-mask contract")
    planted = {}
    for plant in raw["plants"]:
        kind = plant["type"]
        if not 0 <= kind < len(PLANTS):
            raise ValueError("unsupported plant on lawn")
        planted.setdefault((plant["row"],plant["col"]), []).append(PLANTS[kind])
    zs, nearest = [], {}
    for zombie in raw["zombies"]:
        kind, row = zombie["type"], zombie["row"]
        if not 0 <= kind < len(ZOMBIES) or not 0 <= row < 5:
            raise ValueError("unsupported zombie in day-level evaluation")
        col = max(0, min(9, int((float(zombie["x"]) - 40) // 80)))
        zs.append(Zombie(type=ZOMBIES[kind], row=row, col=col))
        nearest[row] = min(nearest.get(row, 9), col)
    scene = Scene(frame_id=tick, t_capture=tick/100, lawn="day", rows=5, sun=int(g["sun"]),
                  cards=[Card(slot=c["slot"],plant=PLANTS[c["plant_type"]],ready=c["ready"],brightness=0.0) for c in cards],
                  zombies=zs, nearest_col=nearest, positions="native_training_state")
    allowed = [[slot,row,col] for slot in range(len(cards)) for row in range(5) for col in range(9)
               if mask[1 + slot*54 + row*9 + col]]
    return dict(scene=scene.model_dump(), plants=[{"row":r,"col":c,"plants":ps} for (r,c),ps in planted.items()],
                allowed_positions=allowed)


class NativeArena:
    def __new__(cls,config,mode):
        if config.get('backend')=='pvz_portable':
            from rsi.portable_runtime import PortableArena
            return PortableArena(config,mode)
        return super().__new__(cls)

    def __init__(self, config, mode):
        from common.project_paths import resolve_runtime_paths
        config = resolve_runtime_paths(config)
        if sys.platform != "win32":
            raise RuntimeError("pvz_rl game executor must run on Windows")
        self.identity = preflight(config)
        repo = Path(config["repository"]).resolve()
        sys.path.insert(0, str(repo / "python"))
        from pvz_gym.runtime.save_template import prepare_training_save
        from pvz_gym.runtime.instances import GameInstance
        from pvz_gym.runtime import RuntimeConfig, TurboConfig
        from pvz_gym.client import PvZClient
        from pvz_gym.env import PvZEnv
        root = Path(config["work_dir"]).resolve()
        instance_id = config.get("instance_id", 0)
        save = prepare_training_save(config["game_executable"], root / "save_templates" / f"worker-{instance_id}")
        self.instance = GameInstance(config["game_executable"], root / "instances", instance_id, save_template=save)
        self.env = None
        client = None
        try:
            pid = self.instance.start()
            options = RuntimeConfig(process_id=pid)
            client = PvZClient(dll_path=config.get("dll") or str(repo / "bin/pvz_gym_native.dll"), runtime_config=options,
                               save_dir=self.instance.save_dir, profile_id=self.instance.active_profile_id,
                               instance_id=self.instance.instance_id, template_dir=self.instance.template_dir, clean_save=True)
            deadline = time.monotonic() + 60
            while not client.is_game_on() or client.get_raw_scene() == 0:
                if self.instance.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("private game did not initialize")
                time.sleep(.1)
            self.env = PvZEnv(client, level_id=4, playthrough=1, ticks_per_step=25,
                              max_episode_ticks=60000, simulation_mode="turbo", runtime_config=options,
                              turbo_config=TurboConfig(suppress_render=mode != "pixels"))
            self.mode = mode
        except BaseException:
            if client is not None:
                client.close()
            self.instance.close()
            raise

    def reset(self, level, seed):
        self.env.level_id = level
        _, info = self.env.reset(seed=seed)
        return info

    def step(self, placement):
        action = 0 if placement is None else 1 + placement["slot"] * 54 + placement["row"] * 9 + placement["col"]
        _, _, terminal, truncated, info = self.env.step(action)
        return terminal, truncated, info

    def shovel(self, cell):
        raise RuntimeError("the pvz_rl executor has no shovel action")

    def prepare(self, level, seed):
        return None, self.reset(level, seed)  # pvz_rl has no seed chooser: its fixed loadout, already playing

    def pixels(self):
        """Capture ONLY the owned private game client area; never arbitrary desktop pixels."""
        import ctypes
        import cv2
        import numpy as np
        import win32gui, win32ui, win32process
        windows = []
        pid = self.instance.process.pid
        def visit(hwnd, _):
            if win32process.GetWindowThreadProcessId(hwnd)[1] == pid and win32gui.GetClassName(hwnd) == "MainWindow":
                windows.append(hwnd)
        win32gui.EnumWindows(visit, None)
        if len(windows) != 1:
            raise RuntimeError("private game window is ambiguous or missing")
        hwnd = windows[0]
        _, _, width, height = win32gui.GetClientRect(hwnd)
        if (width,height) != (800,600) or win32gui.IsIconic(hwnd):
            raise RuntimeError("pixel audit requires a restored 800x600 private game window")
        dc_handle = win32gui.GetDC(hwnd)
        source = win32ui.CreateDCFromHandle(dc_handle)
        target = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source, width, height)
            target.SelectObject(bitmap)
            # Render into our own bitmap without activating or clicking a desktop window.
            from ctypes import wintypes
            print_window = ctypes.windll.user32.PrintWindow
            print_window.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
            print_window.restype = wintypes.BOOL
            if not print_window(hwnd, target.GetSafeHdc(), 3):
                raise RuntimeError("private-window capture failed")
            frame = np.frombuffer(bitmap.GetBitmapBits(True), dtype=np.uint8).reshape(height,width,4)[:,:,:3].copy()
            if float(frame.std()) < 2:
                raise RuntimeError("empty private-window frame")
            ok, jpeg = cv2.imencode(".jpg", frame)
            if not ok:
                raise RuntimeError("JPEG encoding failed")
            return jpeg.tobytes()
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            target.DeleteDC(); source.DeleteDC(); win32gui.ReleaseDC(hwnd, dc_handle)

    def close(self):
        try:
            if self.env is not None:
                self.env.close()
        finally:
            self.instance.close()


def raw_info(info):
    raw = info["raw_observation"]
    return asdict(raw) if hasattr(raw, "__dataclass_fields__") else dict(raw)
