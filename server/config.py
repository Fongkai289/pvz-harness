"""Server settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    token: str
    planner: str  # "mock" | "qwen" | "claude"
    planner_model: str  # Claude model id
    planner_effort: str
    planner_send_image: bool  # qwen: False = text-only, reads the perception summary
    planner_include_summary: bool  # append the perception summary to the planner prompt
    planner_timeout: float
    planner_mode: str  # "end_to_end" (one call looks and decides) | "split" (look step, then decide)
    loadout: list[str]  # plants in the seed bank, slot 0 first: the agent chose them at seed selection
    look_interval: float  # split mode: seconds between look steps (Jev acts on the latest scene)
    plants_interval: float  # split mode: seconds between reads of the plants on the lawn
    qwen_url: str
    qwen_model: str
    decider: str  # "rules" (explicit baseline) | "onejev" (model chooses plant, row, column or wait)
    onejev_url: str
    onejev_model: str
    decider_timeout: float
    plan_interval: float
    fast_interval: float
    runs_dir: Path
    data_dir: Path  # playbooks, CURRENT, ledger; survives deploys
    frame_log_interval: float
    max_run_bytes: int
    zombies_interval: float = 4.0
    auto_native_rsi: bool = False
    native_evolution: str = "strategy"
    native_coach_actions: int = 20  # tool actions per research round for the single coach role
    native_coach_tests: int = 2  # development test runs per round
    coach_provider: str = "qwen"
    coach_model: str = "gpt-6-astra"
    coach_queue_dir: Path = ROOT / "data/codex_coach"
    coach_timeout: float = 240
    system0_actions: bool = True  # live master switch: off stops every System0 plant (reflex and fallback)


def load_settings(dotenv_path: Path | None = None) -> Settings:
    _load_dotenv(dotenv_path if dotenv_path is not None else ROOT / ".env")
    token = os.environ.get("PVZ_TOKEN", "")
    if not token:
        raise RuntimeError("PVZ_TOKEN is not set; put it in .env (see .env.example)")
    return Settings(
        # Bound to loopback by default: the Windows client reaches it through an SSH tunnel.
        host=os.environ.get("PVZ_HOST", "127.0.0.1"),
        port=int(os.environ.get("PVZ_PORT", "8765")),
        token=token,
        planner=os.environ.get("PVZ_PLANNER", "mock"),
        planner_model=os.environ.get("PVZ_PLANNER_MODEL", "claude-opus-5-5"),
        planner_effort=os.environ.get("PVZ_PLANNER_EFFORT", "low"),
        planner_send_image=_bool("PVZ_PLANNER_SEND_IMAGE", True),
        planner_include_summary=_bool("PVZ_PLANNER_INCLUDE_SUMMARY", False),
        planner_timeout=float(os.environ.get("PVZ_PLANNER_TIMEOUT", "60")),
        planner_mode=os.environ.get("PVZ_PLANNER_MODE", "split"),
        # Research pixel games' public card choice and tests only: live play recognizes the cards on
        # screen (server/seed_packets.py), whatever the operator picked.
        loadout=[p.strip() for p in os.environ.get(
            "PVZ_LOADOUT", "peashooter,sunflower,cherry bomb,wall-nut,potato mine,snow pea").split(",") if p.strip()],
        look_interval=float(os.environ.get("PVZ_LOOK_INTERVAL", "4")),
        plants_interval=float(os.environ.get("PVZ_PLANTS_INTERVAL", "4")),
        zombies_interval=float(os.environ.get("PVZ_ZOMBIES_INTERVAL", "4")),
        # Shared model pools on this host (owned by another team; keep traffic low).
        qwen_url=os.environ.get("PVZ_QWEN_URL", "http://127.0.0.1:18201/v1/chat/completions"),
        qwen_model=os.environ.get("PVZ_QWEN_MODEL", "Qwen3.8-27B"),
        decider=os.environ.get("PVZ_DECIDER", "rules"),
        # 27B bare decision API; legacy 0.8B /v1/systemone remains supported explicitly.
        onejev_url=os.environ.get("PVZ_ONEJEV_URL", "http://127.0.0.1:18222/v1/completions"),
        onejev_model=os.environ.get("PVZ_ONEJEV_MODEL", "AutoTrust-JEV-27B-v1.1"),
        decider_timeout=float(os.environ.get("PVZ_DECIDER_TIMEOUT", "1.0")),
        plan_interval=float(os.environ.get("PVZ_PLAN_INTERVAL", "6")),
        fast_interval=float(os.environ.get("PVZ_FAST_INTERVAL", "1.0")),
        runs_dir=Path(os.environ.get("PVZ_RUNS_DIR", str(ROOT / "runs"))),
        data_dir=Path(os.environ.get("PVZ_DATA_DIR", str(ROOT / "data"))),
        frame_log_interval=float(os.environ.get("PVZ_FRAME_LOG_INTERVAL", "4.0")),
        # This host evicts the container past 160 GiB of local writes; keep runs small.
        max_run_bytes=int(os.environ.get("PVZ_MAX_RUN_BYTES", str(2 * 1024**3))),
        auto_native_rsi=_bool("PVZ_AUTO_NATIVE_RSI", True),
        native_evolution=os.environ.get("PVZ_NATIVE_EVOLUTION", "code"),
        native_coach_actions=int(os.environ.get("PVZ_NATIVE_COACH_ACTIONS", "20")),
        native_coach_tests=int(os.environ.get("PVZ_NATIVE_COACH_TESTS", "2")),
        coach_provider=os.environ.get("PVZ_COACH_PROVIDER", "qwen"),
        coach_model=os.environ.get("PVZ_COACH_MODEL", "gpt-6-astra"),
        coach_queue_dir=Path(os.environ.get("PVZ_COACH_QUEUE_DIR", str(ROOT / "data/codex_coach"))),
        coach_timeout=float(os.environ.get("PVZ_COACH_TIMEOUT", "240")),
        system0_actions=_bool("PVZ_SYSTEM0_ACTIONS", True),
    )


def effective_settings(settings: Settings, playbook) -> Settings:
    fields = {"resources_seconds": "look_interval", "zombies_seconds": "zombies_interval",
              "plants_seconds": "plants_interval", "planner_seconds": "plan_interval",
              "frame_log_seconds": "frame_log_interval"}
    return replace(settings, **{target: getattr(playbook.timing, source)
                               for source, target in fields.items()
                               if getattr(playbook.timing, source) is not None})


def timing_values(settings: Settings) -> dict[str, float]:
    return {"resources_seconds": settings.look_interval, "zombies_seconds": settings.zombies_interval,
            "plants_seconds": settings.plants_interval, "planner_seconds": settings.plan_interval,
            "frame_log_seconds": settings.frame_log_interval}
