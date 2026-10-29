"""Native simulation host. Never expose EmulatorEpisode itself to candidate code.

The trusted evaluator calls frame/step/truth. Candidate code receives only the
copied RGB image and its own memory; truth is exclusively for the judge.
"""
from .emulator import EmulatorEpisode

__all__ = ["EmulatorEpisode"]
