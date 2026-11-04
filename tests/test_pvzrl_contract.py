"""Optional upstream Gym contract test; deterministic facts, NOT a real game result."""
from dataclasses import replace
import importlib.util
from pathlib import Path

import pytest

from rsi.native_loop import run_episode
from rsi.native_runtime import NativeArena


def test_pinned_upstream_gym_runs_through_our_episode_adapter(tmp_path):
    pytest.importorskip("pvz_gym")
    from pvz_gym.env import PvZEnv
    from pvz_gym.runtime import RuntimeCapabilities
    source = Path(__file__).resolve().parents[2] / "external/pvz_rl/tests/fake_backend.py"
    if not source.exists():
        pytest.skip("optional pinned upstream fixture is not installed on this host")
    spec = importlib.util.spec_from_file_location("native_fact_fixture", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Client(module.FactClient):
        def _observation(self):
            value = super()._observation()
            return replace(value, global_state={**value.global_state, "scene":0, "grid_rows":5,
                           "level_id":self.last_reset_config.get("level_id",2)})
        def get_runtime_capabilities(self):
            return RuntimeCapabilities(modes=frozenset({"stepped","turbo"}),
                features=frozenset({"attach_pid","resident_executor","binary_transport","shared_snapshot"}))
        def configure_runtime(self, options): pass
        def begin_turbo_mode(self, options=None): return True
        def end_turbo_mode(self): return True
        def step_turbo(self, action, ticks): return self.step(action, ticks)
    class RPC:
        def call(self, op, **payload):
            assert op == "decide"
            assert "allowed_positions" in payload and "raw_observation" not in payload
            return dict(placement=None,plan={"objective":"fixture"},plan_tick=0,spent=0,timings={},
                        scene=payload["scene"],plants=payload["plants"])
    client = Client()
    arena = object.__new__(NativeArena)  # skip NativeArena.__new__'s backend dispatch and the game launch
    arena.env = PvZEnv(client, level_id=2, playthrough=1, ticks_per_step=100,
                       simulation_mode="turbo", max_episode_ticks=60000)
    try:
        result = run_episode(arena, {"job_id":"fixture", "mode":"state"},
                             {"level":2,"seed":42}, RPC(), tmp_path)
        assert result["complete"] and result["outcome"] == "win"
        assert result["elapsed_game_s"] == 2 and result["score"] == 1
        assert client.last_reset_config["seed"] == 42
    finally:
        arena.env.close()
    assert client.closed
