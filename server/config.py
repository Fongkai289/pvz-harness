"""Server settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
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
    decider: str  # "rules" (tactics only) | "onejev" (tactics + OneJev yes/no judgments)
    onejev_url: str
    onejev_model: str
    decider_timeout: float
    plan_interval: float
    fast_interval: float
    pause_for_planner: bool
    runs_dir: Path
    data_dir: Path  # playbooks, CURRENT, ledger; survives deploys
    frame_log_interval: float
    max_run_bytes: int


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
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
        # Until seed selection is automated, the loadout is configured (default: level 1-7's cards).
        loadout=[p.strip() for p in os.environ.get(
            "PVZ_LOADOUT", "peashooter,sunflower,cherry bomb,wall-nut,potato mine,snow pea").split(",") if p.strip()],
        look_interval=float(os.environ.get("PVZ_LOOK_INTERVAL", "4")),
        plants_interval=float(os.environ.get("PVZ_PLANTS_INTERVAL", "8")),
        # Shared model pools on this host (owned by another team; keep traffic low).
        qwen_url=os.environ.get("PVZ_QWEN_URL", "http://127.0.0.1:18201/v1/chat/completions"),
        qwen_model=os.environ.get("PVZ_QWEN_MODEL", "Qwen3.8-27B"),
        decider=os.environ.get("PVZ_DECIDER", "rules"),
        # 0.8B: ~6 ms per call on 2026-10-28; the 27B pool (18200) ranged 0.03-5.7 s under shared load.
        onejev_url=os.environ.get("PVZ_ONEJEV_URL", "http://127.0.0.1:18202/v1/systemone"),
        onejev_model=os.environ.get("PVZ_ONEJEV_MODEL", "OneJev-0.8B"),
        decider_timeout=float(os.environ.get("PVZ_DECIDER_TIMEOUT", "0.5")),
        plan_interval=float(os.environ.get("PVZ_PLAN_INTERVAL", "20")),
        fast_interval=float(os.environ.get("PVZ_FAST_INTERVAL", "1.0")),
        pause_for_planner=_bool("PVZ_PAUSE_FOR_PLANNER", False),
        runs_dir=Path(os.environ.get("PVZ_RUNS_DIR", str(ROOT / "runs"))),
        data_dir=Path(os.environ.get("PVZ_DATA_DIR", str(ROOT / "data"))),
        frame_log_interval=float(os.environ.get("PVZ_FRAME_LOG_INTERVAL", "1.0")),
        # This host evicts the container past 160 GiB of local writes; keep runs small.
        max_run_bytes=int(os.environ.get("PVZ_MAX_RUN_BYTES", str(2 * 1024**3))),
    )
