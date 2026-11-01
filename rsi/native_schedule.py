"""Game-clock scheduling; no operation is created or replayed here."""
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rules.hard_rules import PLANT_COSTS

Event = Literal['new_threat', 'threat_advance', 'plant_lost', 'resource_ready']


class DecisionSchedule(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    seconds: float = Field(default=2, ge=.5, le=10)
    planner_seconds: float = Field(default=20, ge=4, le=60)
    event_min_seconds: float = Field(default=.5, ge=.25, le=5)
    events: list[Event] = Field(default_factory=lambda: ['new_threat', 'threat_advance', 'plant_lost', 'resource_ready'], max_length=4)
    selection: Literal['flat', 'hierarchical'] = 'flat'


def _facts(public):
    scene = public['scene']
    threats = Counter((z['row'], z['type']) for z in scene.get('zombies', []))
    nearest = {}
    for z in scene.get('zombies', []):
        nearest[z['row']] = min(nearest.get(z['row'], 10), z['col'])
    plants = Counter((p['row'], p['col'], plant) for p in public['plants'] for plant in p['plants'])
    sun = scene.get('sun')
    usable = {c['slot'] for c in scene.get('cards', []) if c.get('ready') and sun is not None
              and sun >= (c['cost'] if c.get('cost') is not None else PLANT_COSTS.get(c['plant'], float('inf')))}
    return threats, nearest, plants, usable


class DecisionClock:
    def __init__(self, schedule=None):
        self.schedule = DecisionSchedule.model_validate(schedule or {})
        self.last_tick = None
        self.previous = None

    def due(self, tick, public=None):
        if self.last_tick is None:
            return ['initial']
        elapsed = (tick-self.last_tick)/100
        if elapsed < 0:
            raise ValueError('game clock moved backwards')
        reasons = ['periodic'] if elapsed >= self.schedule.seconds else []
        if public is None or self.previous is None or elapsed < self.schedule.event_min_seconds:
            return reasons
        threats, nearest, plants, usable = _facts(public)
        old_threats, old_nearest, old_plants, old_usable = self.previous
        changes = {
            'new_threat': bool(threats-old_threats),
            'threat_advance': any(row in old_nearest and col < old_nearest[row] for row, col in nearest.items()),
            'plant_lost': bool(old_plants-plants),
            'resource_ready': bool(usable-old_usable),
        }
        return reasons + [name for name in dict.fromkeys(self.schedule.events) if changes[name]]

    def committed(self, tick, public=None, schedule=None):
        self.last_tick = tick
        self.previous = _facts(public) if public is not None else None
        if schedule is not None:
            self.schedule = DecisionSchedule.model_validate(schedule)


def timing_summary(samples):
    result = {}
    for name, values in samples.items():
        ordered = sorted(values)
        if ordered:
            result[name] = dict(count=len(ordered), total_s=sum(ordered),
                p50_s=ordered[(len(ordered)-1)//2], p95_s=ordered[min(len(ordered)-1, int(len(ordered)*.95))])
    return result
