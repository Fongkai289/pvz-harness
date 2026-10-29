"""Real-engine checks, not a Python replacement for physics. Install pvzemu first."""
import numpy as np
import pytest

pytest.importorskip("pvzemu", reason="optional native engine: run scripts/install_emulator.py")

from sim import EmulatorEpisode
from sim.render import COLORS, DIGITS, PLANTS, READY


def test_native_required():
    import pvzemu
    assert pvzemu.training_patch == "pvz-agent-v1"


def test_install_reuses_existing_pybind_without_pip(monkeypatch):
    from scripts import install_emulator
    monkeypatch.setattr(install_emulator, "version", lambda name: "2.13.6")
    def unexpected_install(*args, **kwargs):
        raise AssertionError("Existing pybind11 must not require pip or network")
    monkeypatch.setattr(install_emulator, "run", unexpected_install)
    install_emulator.ensure_pybind11()


def test_install_uses_uv_for_environment_without_pip(monkeypatch):
    from scripts import install_emulator
    calls = []
    monkeypatch.setattr(install_emulator, "version", lambda name: "2.5.0")
    monkeypatch.setattr(install_emulator.shutil, "which", lambda name: "/tools/uv")
    monkeypatch.setattr(install_emulator, "run", lambda args: calls.append(args))
    install_emulator.ensure_pybind11()
    assert calls == [["/tools/uv", "pip", "install", "--python", install_emulator.sys.executable, "pybind11==2.13.6"]]


def test_pixels_and_native_economy_cooldown():
    ep = EmulatorEpisode(11, 1000)
    frame = ep.frame()
    assert frame.shape == (140, 180, 3) and frame.dtype == np.uint8
    assert tuple(frame[20, 60]) == READY
    # Actual native plant succeeds, spends 50 sun, starts its 750-tick cooldown.
    assert ep.step({"plant": "sunflower", "row": 2, "col": 0}, ticks=1) == {"advanced_ticks": 1, "done": False}
    t = ep.truth()
    assert t["sun"] == 0 and t["plants_placed"] == 1
    assert t["cards"][0]["cooldown"] == 749
    assert tuple(ep.frame()[70, 13]) == COLORS["sunflower"]
    ep.step({"plant": "sunflower", "row": 2, "col": 1}, ticks=749)
    assert ep.truth()["invalid_actions"] == 1
    assert ep.truth()["cards"][0]["cooldown"] == 0
    assert ep.truth()["sun"] >= 25  # native daylight sun production
    frame[:] = 0
    assert ep.frame().any()  # no image/state alias


def test_visible_sun_digits_match_judge_without_metadata():
    ep = EmulatorEpisode(7, 1000)
    image = ep.frame()
    decoded = ""
    for i in range(4):
        tile = image[4:14:2, 4+8*i:10+8*i:2]
        bits = tuple("".join("1" if tuple(pixel) == (255,255,255) else "0" for pixel in row) for row in tile)
        decoded += next(d for d, pattern in DIGITS.items() if pattern == bits)
    assert int(decoded) == ep.truth()["sun"] == 50


def test_replay_and_tick_chunking():
    a, b = EmulatorEpisode(19, 6000), EmulatorEpisode(19, 6000)
    action = {"plant": "sunflower", "row": 0, "col": 0}
    a.step(action, ticks=100)
    b.step(action, ticks=1)
    b.step(None, ticks=99)
    for _ in range(50):
        a.step(None)
        b.step(None, ticks=37)
        b.step(None, ticks=63)
    assert a.truth() == b.truth()
    assert np.array_equal(a.frame(), b.frame())
    assert a.truth()["spawned"] > 0


def test_native_combat_and_loss():
    # Fixture setup is privileged. Candidate API never exposes these native handles.
    ep = EmulatorEpisode(9, 3000)
    ep._native._configure_training(ep._world, 500)
    ep._schedule = [(1, "zombie", 2, 123)]
    ep.step({"plant": "peashooter", "row": 2, "col": 2}, ticks=3000)
    assert ep.truth()["killed"] == 1  # native projectiles actually kill the zombie
    naked = EmulatorEpisode(9, 10000)
    naked.step(None, ticks=10000)
    assert naked.done and naked.truth()["outcome"] == "lose"


def test_spawn_schedule_independent_of_player_actions():
    a, b = EmulatorEpisode(2, 8000), EmulatorEpisode(2, 8000)
    a.step({"plant": "sunflower", "row": 0, "col": 0}, ticks=1800)
    b.step(None, ticks=1800)
    assert a._schedule == b._schedule
    assert a.truth()["zombies"] == b.truth()["zombies"]


def test_eaten_counter_from_native_hp():
    ep = EmulatorEpisode(9, 5000)
    ep._schedule = [(1, "zombie", 2, 123)]
    ep.step({"plant": "sunflower", "row": 2, "col": 8}, ticks=2500)
    assert ep.truth()["plants_eaten"] == 1


def test_reject_bad_actions_and_terminate():
    ep = EmulatorEpisode(0, 2)
    ep.step({"plant": "sunflower", "row": -1, "col": 0}, ticks=1)
    ep.step({"plant": "sunflower", "row": True, "col": 0}, ticks=1)
    assert ep.truth()["invalid_actions"] == 2
    assert ep.done and ep.truth()["outcome"] == "timeout"
    before = ep.truth()
    ep.step(None)
    assert ep.truth() == before
    with pytest.raises(ValueError):
        EmulatorEpisode(-1)


@pytest.mark.parametrize("name", PLANTS)
def test_six_cards_use_native_cost(name):
    ep = EmulatorEpisode(1, 1000)
    ep._native._configure_training(ep._world, 1000)
    expected = next(c["cost"] for c in ep.truth()["cards"] if c["plant"] == name)
    ep.step({"plant": name, "row": 2, "col": 2}, ticks=1)
    assert ep.truth()["plants_placed"] == 1
    assert ep.truth()["sun"] == 1000-expected
