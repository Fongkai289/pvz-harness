"""Evaluated System0 programs, used identically by pvz_rl and the live model pipeline.

Code derives observations/context and episode memory, and may define Jev's decision menu,
carry out the chosen decision and run reflexes. Its outputs only become operations through
server/action_gate.py and the unchanged hard rules; Jev alone chooses among menu decisions.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import queue
import threading

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rsi.code_runtime import CandidateProgram, _check_json, validate_source
from rsi.code_bundle import compile_bundle, unpack_source

# Starting player: replan checks against the plan's replan_when, a named decision menu mirroring
# the rule tactics, default pickups.
SEED = '''def perceive(image):
    return image


def decide(observation, memory):
    reasons = seed_replan(observation, memory)
    planner_context = {}
    if len(reasons) > 0:
        planner_context["replan_reasons"] = reasons
    rows = seed_rows(observation)
    if len(rows) > 0:
        planner_context["row_estimates"] = rows
        planner_context["row_estimates_note"] = ("rough, from full-health static numbers and unblocked walking; "
                                                 "seconds; holds = the row's shooters kill everything seen there "
                                                 "before it reaches the front plant (or the house)")
    return {"planner_context": planner_context, "jev_context": {}, "request_replan": len(reasons) > 0}


def seed_single_use(plant):
    return plant in ["cherry bomb", "jalapeno", "squash", "potato mine", "doom-shroom", "ice-shroom",
                     "tangle kelp", "blover", "coffee bean"]

def seed_lawn(observation):
    lawn = {}
    for cell in observation.get("plants") or []:
        lawn[str(cell.get("row")) + ":" + str(cell.get("col"))] = cell.get("plants") or []
    return lawn

def seed_zombies(observation):
    found = []
    for zombie in (observation.get("scene") or {}).get("zombies") or []:
        if zombie.get("row") is not None:
            found.append([zombie["row"], zombie.get("type") or "unknown"])
    return found

def seed_replan_state(plan, status, zombies, lawn):
    when = plan.get("replan_when") or {}
    rows = list(when.get("covered_rows") or []) + list(plan.get("priority_rows") or [])
    for item in plan.get("threat_assessment") or []:
        rows.append(item.get("row"))
    types = list(when.get("covered_types") or [])
    for zombie in zombies:
        # What was already on the lawn when the plan arrived is what the plan was made for.
        rows.append(zombie[0])
        types.append(zombie[1])
    keys = []
    for item in when.get("key_plants") or []:
        keys.append([str(item["row"]) + ":" + str(item["col"]), item["plant"]])
    if len(keys) == 0:
        for key in sorted(lawn):
            for plant in lawn[key]:
                if seed_kind(plant) in ["attack", "wall"]:
                    keys.append([key, plant])
    return {"plan": status.get("id"), "rows": rows, "types": types, "keys": keys, "held": [],
            "targets": [], "last": [], "done": False, "goal_open": False}

def seed_replan(observation, memory):
    """Reasons the current plan stopped fitting; each must hold on two decisions in a row."""
    plan = observation.get("guidance")
    status = observation.get("plan_status")
    if plan is None or status is None:
        return []
    when = plan.get("replan_when") or {}
    zombies = seed_zombies(observation)
    lawn = seed_lawn(observation)
    state = memory.get("replan")
    if state is None or state.get("plan") != status.get("id"):
        state = seed_replan_state(plan, status, zombies, lawn)
        memory["replan"] = state
    if state["done"]:
        return []
    reasons = []
    for zombie in zombies:
        if zombie[0] not in state["rows"]:
            reasons.append("new threat: zombie in uncovered row " + str(zombie[0]))
        elif zombie[1] not in state["types"] and zombie[1] != "unknown":
            reasons.append("new threat: unexpected " + zombie[1] + " in row " + str(zombie[0]))
    for item in state["keys"]:
        name = item[0] + " " + item[1]
        if item[1] in (lawn.get(item[0]) or []):
            if name not in state["held"]:
                state["held"].append(name)
        elif name in state["held"] and not seed_single_use(item[1]):
            reasons.append("defense lost: " + name)
    goals = when.get("goal_plants") or []
    reached = len(goals) > 0
    for goal in goals:
        count = 0
        for key in lawn:
            count += lawn[key].count(goal["plant"])
        if count < goal["at_least"]:
            reached = False
    # A goal already met when the plan arrived is a description, not a target: only one that was
    # still open at some point under this plan can be reached.
    if len(goals) > 0 and not reached:
        state["goal_open"] = True
    if reached and state.get("goal_open"):
        reasons.append("goal reached: planned plant counts are on the lawn")
    clear = when.get("goal_clear_rows") or []
    busy = False
    for zombie in zombies:
        if zombie[0] in clear:
            busy = True
            if zombie[0] not in state["targets"]:
                state["targets"].append(zombie[0])
    if len(state["targets"]) > 0 and not busy:
        reasons.append("target gone: rows " + str(clear) + " are clear")
    limit = when.get("max_failed_steps") or 3
    if (status.get("failed_steps") or 0) >= limit:
        reasons.append("plan unworkable: " + str(limit) + "+ decisions in a row could not be carried out")
    confirmed = []
    for reason in reasons:
        if reason in state["last"] and reason not in confirmed:
            confirmed.append(reason)
    state["last"] = reasons[:12]
    if len(confirmed) > 0:
        state["done"] = True
    return confirmed[:6]


def seed_kind(plant):
    kinds = {
        "economy": ["sunflower", "sun-shroom"],
        "attack": ["peashooter", "snow pea", "repeater", "threepeater", "split pea", "cabbage-pult",
                   "kernel-pult", "melon-pult", "starfruit", "cactus", "fume-shroom", "puff-shroom",
                   "scaredy-shroom", "sea-shroom"],
        "wall": ["wall-nut", "tall-nut", "pumpkin"],
        "emergency": ["cherry bomb", "jalapeno", "squash", "potato mine", "chomper", "doom-shroom", "tangle kelp"],
    }
    for name in ["economy", "attack", "wall", "emergency"]:
        if plant in kinds[name]:
            return name
    return ""

def seed_nearest(observation):
    scene = observation.get("scene") or {}
    nearest = {}
    for key in scene.get("nearest_col") or {}:
        nearest[str(key)] = scene["nearest_col"][key]
    for zombie in scene.get("zombies") or []:
        row = zombie.get("row")
        col = zombie.get("col")
        if row is None or col is None:
            continue
        key = str(row)
        if key not in nearest or col < nearest[key]:
            nearest[key] = col
    return nearest

def seed_groups(observation):
    cards = (observation.get("scene") or {}).get("cards") or []
    groups = {}
    for item in observation.get("legal") or []:
        slot = item[0]
        if slot < 0 or slot >= len(cards):
            continue
        plant = cards[slot].get("plant")
        kind = seed_kind(plant)
        if kind == "":
            continue
        key = kind + ":" + str(item[1])
        group = groups.setdefault(key, {"kind": kind, "slot": slot, "row": item[1], "plant": plant, "cols": []})
        if group["slot"] == slot:
            group["cols"].append(item[2])
    return groups

def seed_ref(observation, group, name):
    return ((observation.get("reference") or {}).get(group) or {}).get(name) or {}

def seed_rows(observation):
    """Rough per-row times from static full-health numbers, so models compare instead of computing.

    front_s: until the first zombie reaches the rightmost plant in its way; house_s: unblocked walk
    home; kill_s: the row's rated shooters against every zombie seen there; holds: kill_s <= arrival.
    """
    lawn = {}
    for cell in observation.get("plants") or []:
        lawn.setdefault(str(cell.get("row")), []).append([cell.get("col"), cell.get("plants") or []])
    facts = {}
    for zombie in (observation.get("scene") or {}).get("zombies") or []:
        row = zombie.get("row")
        if row is None:
            continue
        name = zombie.get("type") or "unknown"
        ref = seed_ref(observation, "zombies", name)
        fact = facts.setdefault(str(row), {"row": row, "zombies": 0, "first": None, "col": None, "hp": 0,
                                           "front_s": None, "house_s": None, "dps": 0, "unrated": 0,
                                           "kill_s": None, "holds": None})
        fact["zombies"] += 1
        if fact["hp"] is not None:
            fact["hp"] = None if ref.get("hp") is None else fact["hp"] + ref["hp"]
        col = zombie.get("col")
        speed = ref.get("s_per_tile")
        if col is None or speed is None:
            continue
        house = round((col + 1) * speed)
        if fact["house_s"] is None or house < fact["house_s"]:
            fact["house_s"] = house
            fact["first"] = name
            fact["col"] = col
        front = None
        for item in lawn.get(str(row)) or []:
            if item[0] is not None and item[0] <= col and (front is None or item[0] > front):
                front = item[0]
        if front is not None and (fact["front_s"] is None or round((col - front) * speed) < fact["front_s"]):
            fact["front_s"] = round((col - front) * speed)
    for key in facts:
        fact = facts[key]
        for item in lawn.get(key) or []:
            if fact["col"] is not None and item[0] is not None and item[0] > fact["col"]:
                continue
            for plant in item[1]:
                dps = seed_ref(observation, "plants", plant).get("dps")
                if dps is not None:
                    fact["dps"] += dps
                elif seed_kind(plant) == "attack":
                    fact["unrated"] += 1
        if fact["hp"] is not None and fact["dps"] > 0 and fact["unrated"] == 0:
            fact["kill_s"] = round(fact["hp"] / fact["dps"])
            arrival = fact["front_s"] if fact["front_s"] is not None else fact["house_s"]
            if arrival is not None:
                fact["holds"] = fact["kill_s"] <= arrival
        fact["dps"] = round(fact["dps"], 1)
    return [facts[key] for key in sorted(facts)]

def seed_note(fact, added):
    """Precomputed comparisons for one menu line; added is the damage per second the option adds."""
    if fact is None:
        return "no zombie seen"
    text = str(fact["zombies"]) + (" zombie" if fact["zombies"] == 1 else " zombies")
    arrival = fact["front_s"] if fact["front_s"] is not None else fact["house_s"]
    if fact["first"] is not None:
        text += "; " + fact["first"] + " col " + str(fact["col"])
    if fact["front_s"] is not None:
        text += " reaches front plant ~" + str(fact["front_s"]) + "s"
    elif fact["house_s"] is not None:
        text += " reaches house ~" + str(fact["house_s"]) + "s"
    if fact["kill_s"] is not None:
        text += "; row kills all ~" + str(fact["kill_s"]) + "s"
        if fact["holds"] is False:
            text += ", too slow"
    elif fact["hp"] is not None and fact["dps"] == 0 and fact["unrated"] == 0:
        text += "; no shooter"
    if added and fact["hp"] is not None and fact["unrated"] == 0:
        kill = round(fact["hp"] / (fact["dps"] + added))
        text += ", with it ~" + str(kill) + "s"
        if arrival is not None and kill > arrival:
            text += ", too slow"
    return text

def seed_sun_after(observation, group):
    sun = observation.get("sun_available")
    if sun is None:
        sun = (observation.get("scene") or {}).get("sun")
    cards = (observation.get("scene") or {}).get("cards") or []
    cost = cards[group["slot"]].get("cost")
    if cost is None:
        cost = seed_ref(observation, "plants", group["plant"]).get("cost")
    if sun is None or cost is None:
        return ""
    return "; sun after " + str(sun - cost)

def options(observation, memory):
    groups = seed_groups(observation)
    nearest = seed_nearest(observation)
    facts = {}
    for fact in seed_rows(observation):
        facts[str(fact["row"])] = fact
    rows = (observation.get("scene") or {}).get("rows") or 5
    menu = []
    economy = [key for key in sorted(groups) if groups[key]["kind"] == "economy"]
    if len(economy) > 0:
        group = groups[economy[0]]
        ref = seed_ref(observation, "plants", group["plant"])
        income = ""
        if ref.get("sun_per_cycle") is not None and ref.get("cycle_s") is not None:
            income = " (+" + str(ref["sun_per_cycle"]) + " sun every ~" + str(round(ref["cycle_s"])) + "s"
            income += seed_sun_after(observation, group) + ")"
        menu.append({"id": "economy", "text": ("Economy: plant a " + group["plant"] + " for sun income" + income)[:160],
                     "tags": ["economy"], "rows": [], "params": {"kind": "economy"}})
    for row in range(rows):
        zombie = nearest.get(str(row))
        fact = facts.get(str(row))
        group = groups.get("emergency:" + str(row))
        if group is not None and zombie is not None and zombie <= 6:
            menu.append({"id": "emergency_row_" + str(row), "tags": ["defense"], "rows": [row],
                         "text": ("Row " + str(row) + " emergency: use " + group["plant"] + " (" + seed_note(fact, 0)
                                  + seed_sun_after(observation, group) + ")")[:160],
                         "params": {"kind": "emergency", "row": row}})
        group = groups.get("attack:" + str(row))
        if group is not None:
            added = seed_ref(observation, "plants", group["plant"]).get("dps") or 0
            menu.append({"id": "defend_row_" + str(row), "tags": ["reinforce"] if zombie is None else ["defense"],
                         "rows": [row], "text": ("Row " + str(row) + ": add a " + group["plant"] + " ("
                                                 + seed_note(fact, added) + seed_sun_after(observation, group) + ")")[:160],
                         "params": {"kind": "attack", "row": row}})
        group = groups.get("wall:" + str(row))
        if group is not None and zombie is not None:
            hold = ""
            wall_hp = seed_ref(observation, "plants", group["plant"]).get("hp")
            chew = seed_ref(observation, "zombies", (fact or {}).get("first")).get("chew_dps")
            if wall_hp is not None and chew:
                hold = "; holds one ~" + str(round(wall_hp / chew)) + "s"
            menu.append({"id": "wall_row_" + str(row), "tags": ["defense"], "rows": [row],
                         "text": ("Row " + str(row) + ": " + group["plant"] + " in front of the zombie ("
                                  + seed_note(fact, 0) + hold + seed_sun_after(observation, group) + ")")[:160],
                         "params": {"kind": "wall", "row": row}})
    return menu

def seed_closest(cols, target):
    best = None
    for col in cols:
        if best is None or abs(col - target) < abs(best - target) or (abs(col - target) == abs(best - target) and col < best):
            best = col
    return best

def act(observation, memory, decision):
    return seed_place(observation, decision)

def seed_place(observation, decision):
    params = decision.get("params") or {}
    kind = params.get("kind")
    groups = seed_groups(observation)
    nearest = seed_nearest(observation)
    if kind == "economy":
        counts = {}
        for cell in observation.get("plants") or []:
            for plant in cell.get("plants") or []:
                if seed_kind(plant) == "economy":
                    counts[str(cell.get("row"))] = counts.get(str(cell.get("row")), 0) + 1
        best = None
        for key in sorted(groups):
            group = groups[key]
            if group["kind"] != "economy":
                continue
            score = [counts.get(str(group["row"]), 0), min(group["cols"]), group["row"]]
            if best is None or score < best[0]:
                best = [score, group]
        if best is None:
            return []
        group = best[1]
        return [{"type": "plant", "slot": group["slot"], "row": group["row"], "col": min(group["cols"])}]
    row = params.get("row")
    group = groups.get(str(kind) + ":" + str(row))
    if group is None:
        return []
    zombie = nearest.get(str(row))
    if kind == "attack":
        col = min(group["cols"])
    elif kind == "wall":
        col = seed_closest(group["cols"], 6 if zombie is None else max(zombie - 1, 0))
    elif kind == "emergency":
        target = 6 if zombie is None else zombie
        if group["plant"] == "potato mine":
            target = max(target - 2, 0)
        col = seed_closest(group["cols"], target)
    else:
        return []
    return [{"type": "plant", "slot": group["slot"], "row": row, "col": col}]


def fallback(observation, memory):
    """The plan no longer fits and a new one is coming: shore up the row hit soonest, else keep the sun."""
    groups = seed_groups(observation)
    nearest = seed_nearest(observation)
    worst = None
    for fact in seed_rows(observation):
        if fact["holds"] is True or nearest.get(str(fact["row"])) is None:
            continue
        arrival = fact["front_s"] if fact["front_s"] is not None else fact["house_s"]
        key = [999 if arrival is None else arrival, fact["row"]]
        if worst is None or key < worst[0]:
            worst = [key, fact["row"]]
    if worst is None:
        return []
    row = worst[1]
    kinds = ["attack", "wall"]
    if nearest[str(row)] <= 6 and worst[0][0] <= 10:
        kinds = ["emergency", "attack", "wall"]
    for kind in kinds:
        if groups.get(kind + ":" + str(row)) is not None:
            return seed_place(observation, {"params": {"kind": kind, "row": row}})
    return []


def reflex(observation, memory):
    actions = []
    for index in range(min(2, len(observation.get("suns") or []))):
        actions.append({"type": "collect_sun", "sun": index})
    return actions
'''


CONTRACT = '''Native System0 contract, pvz_rl and live screenshot client:
perceive(image) receives a JSON object, NOT an Emulator RGB object:
{scene: current observed sun/cards/zombies/rows and shovel (true when the shovel is in its box),
 plants: list of {row,col,plants},
 reference: static per-species numbers for the cards, planted plants and seen zombie types,
 {plants: {name: {cost, hp, dps, sun_per_cycle, cycle_s}}, zombies: {type: {hp, chew_dps,
 s_per_tile (fastest listed phase)}}}; full-health public values, null when unverified, never
 live HP or timers,
 guidance: previous/current Planner message or null, committed_spend: integer,
 tick: game/observation ticks (100 per second), pixels: null or 16x12 RGB averages,
 sun_available: observed sun minus commands issued since that observation, or null,
 legal: list of [slot,row,col] placements that pass the hard rules now, or null,
 suns: list of {x,y,area} pickup targets the host has confirmed on two frames (reflex only),
 plan_status: null or {id: changes with every new plan, age_s, failed_steps: consecutive Jev
 decisions under this plan that could not be carried out, fallback_reasons: the reasons decide()
 gave when it requested a replan for this plan, or []} (decide and fallback only)}.
Pixels[y][x] is [r,g,b]; no pixels are available in the state condition or in reflex calls.
Never assume that observations are complete. Live images and Qwen observations may have
different ages. perceive returns derived JSON observations; every other entry receives them.
memory is a mutable JSON dictionary private to one episode, shared by all entries.

decide(observation,memory) must return exactly {planner_context: JSON dict,
jev_context: JSON dict, request_replan: bool}. Planner receives planner_context; Jev
receives jev_context. These are model inputs, not commands.
request_replan is the only way to call the Planner before its routine refresh: check the plan's
replan_when (covered rows/types, key plants, goal, max_failed_steps) and put the reasons in
planner_context.replan_reasons. The host enforces fixed limits the code cannot change: at least
8 s between Planner calls, routine refreshes at least 25 s apart, plans valid at least 40 s, and
playbook.timing.danger_replan=false
ignores the request. Sun or card changes alone are not a reason; Jev acts on them.

Optional entries give the player code operation authority through a fixed action gate:
- options(observation,memory) returns Jev's decision menu, at most 24 items
  {id, text, tags, rows, params}: id is a lowercase identifier (not "wait"), text is the
  1-160 character description Jev and Planner read, tags is a subset of
  [defense, economy, reinforce, save_sun], rows lists the rows involved, params is private
  JSON passed back to act. The host always adds "wait". Only Jev chooses from the menu.
- act(observation,memory,decision) receives the chosen item {id,text,tags,rows,params}
  and returns at most one {type:"plant", slot, row, col} and at most one
  {type:"shovel", row, col}. A shovel digs up the plant in that cell (the game refunds no
  sun) and runs first, so [shovel, plant] in the same cell replaces a plant. Digging is
  possible only while observation.scene.shovel is true. An empty list means the decision
  could not be carried out now; the host waits and records decision_unexecutable.
- reflex(observation,memory) runs every 0.25 s on the latest frame and returns at most
  two {type:"collect_sun", sun:index into observation.suns} and, only when
  playbook.reflex.allow_planting is true, at most one plant (rate-limited by
  playbook.reflex.min_plant_interval_s). An error or timeout falls back to the host's
  default pickup of the first two confirmed suns.
- fallback(observation,memory) runs instead of Jev from the decide() that requests a replan
  until the next plan arrives, only when playbook.fallback.allow_planting is true (and, live,
  PVZ_SYSTEM0_ACTIONS is on). It returns at most one plant and one shovel (as act), or [] to keep the sun, rate-limited
  by playbook.fallback.min_plant_interval_s. An error or timeout hands that step back to Jev,
  told that the plan no longer fits.
Define options and act together, or neither. Without them Jev keeps the built-in
placement choice; without reflex the host collects suns itself. playbook.decision.jev_mode
can force the built-in choice for comparison.
Every plant passes the unchanged hard rules (bounds, slot, free cell, card ready, slot not
used since the last look, affordability) and, in research, the native action mask, at the
moment it is executed; every shovel needs the shovel in its box and a plant in the cell (not a
grave buster). Rejected actions are not retried with another cell. Actions cannot
name screen coordinates, pause, score, reset, seeds, validation or promotion.
No files/network, imports/attributes, hidden state or judge data. No host helpers are available.
You may create, split, merge, rename and delete modules under player/; original filenames
and module count are not fixed. Use lowercase identifier directory/file names and .py.
Use edit_modules writes/deletes for atomic refactors. Modules are compiled in one restricted
namespace without imports. Define each entry function exactly once in any module, and use
only pure JSON arithmetic, loops and safe data methods (no lambda, f-strings or while). Max
combined source 24000 characters, memory 64 KiB, result 16 KiB, one call has a fixed
time/memory cap; reflex calls must answer within 150 ms once the worker is warm.
State development and confirmation precede an independent pixels audit. Frozen-clock
tests are not real-time inference benchmarks. Keep code fast for the live client.
Research environments collect suns automatically, so sun pickup choices are only exercised
in live games and cannot earn a promotion by themselves.
For native research only, jev_context may include runtime_schedule:
{seconds: 0.5..10, planner_seconds: 4..60, event_min_seconds: 0.25..5,
 events: subset of [new_threat,threat_advance,plant_lost,resource_ready],
selection: flat or hierarchical}. Omitted fields use defaults (2s/20s/.5s/all/flat).
Explicit playbook.timing.planner_seconds takes precedence over planner_seconds.
This tunes future model calls in GAME time, never creates an action. A periodic
heartbeat is mandatory; all edits are tested with unchanged quality/promotion gates.
The live screenshot client treats this as context only until separately validated.
'''


class NativeCodeStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "native_player_code"
        self.root.mkdir(parents=True, exist_ok=True)

    def add(self, source):
        source = compile_bundle(unpack_source(source, ()), ())
        version = hashlib.sha256(source.encode()).hexdigest()
        path = self.root / (version + ".py")
        if not path.exists():
            try:
                with path.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(source)
            except FileExistsError:
                pass
        if self.source(version) != source:
            raise ValueError("immutable code collision")
        return version

    def source(self, version=None):
        if version is None:
            return compile_bundle(unpack_source(SEED, ()), ())
        if not re.fullmatch(r"[0-9a-f]{64}", version):
            raise ValueError("invalid native code version")
        source = (self.root / (version + ".py")).read_text(encoding="utf-8")
        if hashlib.sha256(source.encode()).hexdigest() != version:
            raise ValueError("immutable native code changed")
        validate_source(source)
        return source


def defined_entries(source):
    """Which optional entries (options/act/reflex) a validated source defines."""
    import ast
    from rsi.code_runtime import OPTIONAL_ENTRIES
    names = {node.name for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    return frozenset(name for name in OPTIONAL_ENTRIES if name in names)


def player_input(scene, plants, guidance, spent, tick, jpeg=None, *, legal=None, suns=None, sun_available=None,
                 plan_status=None):
    pixels = None
    if jpeg:
        import cv2
        import numpy as np
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape[:2] != (600, 800):
            raise ValueError("invalid player image")
        pixels = cv2.cvtColor(cv2.resize(frame, (16,12), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB).tolist()
    from server.mechanics import player_reference
    # JSON roundtrip removes int dictionary keys/tuples; no judge or native mask.
    return json.loads(json.dumps(dict(scene=scene.model_dump(), plants=plants, reference=player_reference(scene, plants),
        guidance=guidance.model_dump() if guidance else None, committed_spend=spent, tick=tick, pixels=pixels,
        sun_available=sun_available, legal=None if legal is None else [list(p) for p in legal],
        suns=[dict(x=s.x, y=s.y, area=s.area) for s in suns or ()], plan_status=plan_status)))


def validate_entry_result(entry, result):
    if entry == "decide":
        return validate_result(result)
    if type(result) is not list:
        raise ValueError(f"System0 {entry} must return a list")
    _check_json(result, f"System0 {entry} result", 16384)
    return result


def run_entry(program, entry, observation, memory, decision=None):
    perceived = program.perceive(observation)
    if entry == "decide":
        result = program.decide(perceived, memory)
    elif entry == "act":
        result = program.call("act", perceived, memory, decision)
    elif entry in {"options", "reflex", "fallback"}:
        result = program.call(entry, perceived, memory)
    else:
        raise ValueError("unknown System0 entry")
    return validate_entry_result(entry, result)


def validate_result(result):
    if type(result) is not dict or set(result) != {"planner_context","jev_context","request_replan"}:
        raise ValueError("System0 may return contexts/replan only; no operation authority")
    if type(result["planner_context"]) is not dict or type(result["jev_context"]) is not dict or type(result["request_replan"]) is not bool:
        raise ValueError("invalid System0 result types")
    _check_json(result, "System0 result", 16384)
    return result


def _windows_job(process):
    """Assign before sending source; kill-on-close and process memory bound on Windows."""
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [("process_time",ctypes.c_int64),("job_time",ctypes.c_int64),("flags",wintypes.DWORD),
                    ("min_ws",ctypes.c_size_t),("max_ws",ctypes.c_size_t),("processes",wintypes.DWORD),
                    ("affinity",ctypes.c_size_t),("priority",wintypes.DWORD),("scheduling",wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name,ctypes.c_uint64) for name in ("read","write","other","read_bytes","write_bytes","other_bytes")]
    class Limits(ctypes.Structure):
        _fields_ = [("basic",Basic),("io",IO),("process_memory",ctypes.c_size_t),
                    ("job_memory",ctypes.c_size_t),("peak_process",ctypes.c_size_t),("peak_job",ctypes.c_size_t)]
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p,wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateJobObjectW(None,None)
    if not handle: raise ctypes.WinError(ctypes.get_last_error())
    limits = Limits(); limits.basic.flags = 0x2000 | 0x100
    limits.process_memory = 256 * 1024**2
    if not api.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not api.AssignProcessToJobObject(handle,int(process._handle)):
        error = ctypes.get_last_error(); api.CloseHandle(handle); raise ctypes.WinError(error)
    return lambda: api.CloseHandle(handle)


def execute(source, observation, memory, timeout=3):
    validate_source(source)
    _check_json(observation,"player input")
    _check_json(memory,"player memory")
    request = json.dumps(dict(source=source,observation=observation,memory=memory),ensure_ascii=False).encode()
    if len(request)>262144: raise ValueError("System0 request too large")
    # No credentials or model URLs in worker environment, arguments or working directory.
    env = {key:os.environ[key] for key in ("SYSTEMROOT","WINDIR") if key in os.environ}
    with tempfile.TemporaryDirectory(prefix="pvz-player-") as cwd:
        process = subprocess.Popen([sys.executable,"-I",str(Path(__file__).resolve()),"--worker"],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=cwd,env=env,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        close_job = None
        try:
            if sys.platform == "win32": close_job = _windows_job(process)
            output, error = process.communicate(request, timeout=timeout)
            if process.returncode or len(output)>150000:
                raise RuntimeError("isolated System0 worker failed")
            response = json.loads(output)
            if "error" in response: raise ValueError(response["error"])
            validate_result(response["result"])
            _check_json(response["memory"],"player memory")
            return response
        finally:
            if process.poll() is None: process.kill()
            process.wait()
            if close_job: close_job()


class PersistentWorker:
    """One bounded, credential-free subprocess; compile once, JSON per request."""
    def __init__(self, source):
        validate_source(source)
        self.source = source
        self.cwd = tempfile.TemporaryDirectory(prefix='pvz-player-')
        self.close_job = None
        self.process = None
        self.closed = False
        self.warm = False  # the first answer includes process start-up and compilation
        env = {key:os.environ[key] for key in ('SYSTEMROOT','WINDIR') if key in os.environ}
        try:
            self.process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), '--persistent'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=self.cwd.name, env=env, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if sys.platform == 'win32':
                self.close_job = _windows_job(self.process)
            self.responses = queue.Queue()
            stream, responses = self.process.stdout, self.responses
            def read():
                try:
                    while line := stream.readline(150001):
                        responses.put(line)
                        if len(line)>150000 or not line.endswith(b'\n'):
                            break
                finally:
                    responses.put(None)
            self.reader = threading.Thread(target=read, daemon=True)
            self.reader.start()
        except BaseException:
            self.close()
            raise

    def call(self, observation, memory, timeout=3, entry='decide', decision=None):
        _check_json(observation, 'player input')
        _check_json(memory, 'player memory')
        request = json.dumps(dict(source=self.source, observation=observation, memory=memory,
                                  entry=entry, decision=decision), ensure_ascii=False).encode()+b'\n'
        if len(request)>262144:
            raise ValueError('System0 request too large')
        try:
            if self.closed:
                raise RuntimeError('System0 worker closed')
            self.process.stdin.write(request)
            self.process.stdin.flush()
            output = self.responses.get(timeout=timeout)
            if output is None or len(output)>150000:
                raise RuntimeError('isolated System0 worker failed')
            response = json.loads(output)
            if 'error' in response:
                raise ValueError(response['error'])
            validate_entry_result(entry, response['result'])
            _check_json(response['memory'], 'player memory')
            self.warm = True
            return response
        except queue.Empty as exc:
            self.close()
            raise TimeoutError('System0 call exceeded wall-time limit') from exc
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
        if self.close_job:
            self.close_job()
        self.cwd.cleanup()


class PlayerRuntime:
    def __init__(self, source):
        self.source, self.memory = source, {}
        self.entries = defined_entries(source)
        self.lock = asyncio.Lock()
        self.generation = 0
        self.worker = None
        self.closed = False

    def reset(self):
        self.generation += 1
        self.memory = {}
        if self.worker:
            self.worker.close()
            self.worker = None

    def close(self):
        self.closed = True
        self.reset()

    def __del__(self):
        if getattr(self, 'worker', None):
            self.worker.close()

    @property
    def has_menu(self):
        return {"options", "act"} <= self.entries

    async def step(self, observation, entry="decide", decision=None, warm_timeout=None):
        """One entry call; warm_timeout applies once the worker has answered before."""
        async with self.lock:
            generation = self.generation
            if self.closed:
                raise RuntimeError('player runtime closed')
            if self.worker is None or self.worker.closed:
                # A failed or timed-out worker is replaced; episode memory survives.
                self.worker = PersistentWorker(self.source)
            timeout = warm_timeout if warm_timeout is not None and self.worker.warm else 3
            response = await asyncio.to_thread(self.worker.call, observation, self.memory, timeout, entry, decision)
            if generation != self.generation:
                raise RuntimeError("episode changed during System0 call")
            self.memory = response["memory"]
            return response["result"]


def worker():
    if sys.platform == "linux":
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2))
        resource.setrlimit(resource.RLIMIT_CPU,(2,2))
    try:
        request = json.loads(sys.stdin.buffer.read(262145))
        program = CandidateProgram(request["source"])
        observation = program.perceive(request["observation"])
        result = validate_result(program.decide(observation,request["memory"]))
        response = dict(result=result,memory=request["memory"])
    except Exception as exc:
        response = {"error":f"{type(exc).__name__}: {exc}"[:500]}
    sys.stdout.buffer.write(json.dumps(response,ensure_ascii=False,allow_nan=False).encode("utf-8"))


def persistent_worker():
    if sys.platform == 'linux':
        import math
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (256*1024**2,256*1024**2))
    program, source = None, None
    while line := sys.stdin.buffer.readline(262145):
        try:
            if len(line)>262144 or not line.endswith(b'\n'):
                raise ValueError('oversized worker input')
            if sys.platform == 'linux':
                used = resource.getrusage(resource.RUSAGE_SELF)
                resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(used.ru_utime+used.ru_stime)+2, resource.RLIM_INFINITY))
            request = json.loads(line)
            if program is None:
                source = request['source']
                program = CandidateProgram(source)
            if request['source'] != source:
                raise ValueError('code changed within worker')
            _check_json(request['observation'], 'player input')
            _check_json(request['memory'], 'player memory')
            result = run_entry(program, request.get('entry', 'decide'), request['observation'], request['memory'],
                               request.get('decision'))
            _check_json(request['memory'], 'player memory')
            response = dict(result=result, memory=request['memory'])
        except Exception as exc:
            response = {'error':f'{type(exc).__name__}: {exc}'[:500]}
        sys.stdout.buffer.write(json.dumps(response,ensure_ascii=False,allow_nan=False).encode()+b'\n')
        sys.stdout.buffer.flush()
        if 'error' in response:
            return


if __name__ == "__main__":
    persistent_worker() if '--persistent' in sys.argv else worker()
