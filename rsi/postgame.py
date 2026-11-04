"""Post-game tables for the coach: fixed code, no model calls, never a player input.

Research games: the Windows executor records engine state (ground truth the player never
sees) once per game second plus every decision, and ships the tables with the episode report.
Live games: the same tables are approximated from the session log, in the player's view.

Tables: costly moments (mower triggers, plants lost), missed chances (a threatened row with
no attacker while a ready card was affordable, for at least 3 s, and nothing planted there),
decision statistics, Planner calls (count and trigger) and Planner->Jev alignment.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter

from rules.hard_rules import PLANT_COSTS

ECONOMY = frozenset({"sunflower", "sun-shroom"})
ATTACK = frozenset({"peashooter", "snow pea", "repeater", "threepeater", "split pea", "cabbage-pult",
                    "kernel-pult", "melon-pult", "starfruit", "cactus", "fume-shroom", "puff-shroom",
                    "scaredy-shroom", "sea-shroom", "gatling pea", "winter melon"})
PLANTS = sorted(PLANT_COSTS)
CODES = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
CODE = {plant: CODES[i] for i, plant in enumerate(PLANTS)}
NAME = {code: plant for plant, code in CODE.items()}
THREAT_COL = 6          # a zombie at or left of this column threatens its row
MISSED_MIN_TICKS = 300  # 3 game seconds
TIMELINE_EVERY = 100    # one sample per game second
TABLE_LIMIT = 20
ALIGNMENT_DEFINITION = (
    "A non-wait decision agrees with a plan when its id is in plan.preferred_decisions, or its first "
    "tag equals plan.priorities[0] and (it names no rows or shares a row with plan.priority_rows). "
    "Built-in placements count as economy for sunflower/sun-shroom, otherwise defense, in the planted row. "
    "after = first non-wait decision under a new plan; before = the last non-wait decision before it, "
    "judged against the same new plan.")


def legend():
    return {"plants": {code: plant for code, plant in NAME.items()}, "empty": ".", "unknown": "?",
            "rows": "one string per lawn row, column 0 (house side) first; top plant of a cell",
            "z": "per row [zombie count, nearest column or null]"}


def plant_tags(plant: str | None) -> list[str]:
    return ["economy"] if plant in ECONOMY else ["defense"]


def signature(decision: dict | None, placement: dict | None, plant: str | None) -> dict:
    """What alignment compares: a named decision, or the built-in placement's plant and row."""
    if decision:
        return {"id": decision.get("id"), "tags": list(decision.get("tags", [])), "rows": list(decision.get("rows", [])),
                "wait": False}
    if placement:
        return {"id": None, "tags": plant_tags(plant), "rows": [placement["row"]], "wait": False}
    return {"id": None, "tags": [], "rows": [], "wait": True}


def agrees(sig: dict, plan: dict | None) -> bool:
    if not plan or sig["wait"]:
        return False
    if sig["id"] and sig["id"] in (plan.get("preferred_decisions") or []):
        return True
    priorities = plan.get("priorities") or []
    if not sig["tags"] or not priorities or sig["tags"][0] != priorities[0]:
        return False
    return not sig["rows"] or bool(set(sig["rows"]) & set(plan.get("priority_rows") or []))


def alignment(decisions: list[dict]) -> dict:
    """decisions: ordered {plan_key, plan, sig}; see ALIGNMENT_DEFINITION."""
    segments: list[list[dict]] = []
    for d in decisions:
        if d.get("plan_key") is None:
            continue
        if not segments or segments[-1][0]["plan_key"] != d["plan_key"]:
            segments.append([])
        segments[-1].append(d)
    before = after = before_total = after_total = 0
    last = None
    for index, segment in enumerate(segments):
        plan = segment[0]["plan"]
        first = next((d for d in segment if not d["sig"]["wait"]), None)
        if index and first is not None:
            after_total += 1
            after += agrees(first["sig"], plan)
            if last is not None:
                before_total += 1
                before += agrees(last["sig"], plan)
        last = next((d for d in reversed(segment) if not d["sig"]["wait"]), last)
    rate = lambda a, n: None if n == 0 else round(a / n, 3)
    return {"plan_changes": max(len(segments) - 1, 0),
            "after": {"agreed": after, "total": after_total, "rate": rate(after, after_total)},
            "before": {"agreed": before, "total": before_total, "rate": rate(before, before_total)},
            "definition": ALIGNMENT_DEFINITION}


def plan_key(plan: dict | None) -> str | None:
    if not plan:
        return None
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:12]


def plan_brief(plan: dict | None) -> dict | None:
    if not plan:
        return None
    return {k: plan.get(k) for k in ("objective", "priorities", "priority_rows", "preferred_decisions")}


def encode_state(state: dict, tick: int) -> dict:
    """Compact engine-state sample: sun, ready slots, plant rows, zombies per row."""
    scene = state["scene"]
    rows = scene.get("rows") or 5
    grid = [["."] * 9 for _ in range(rows)]
    for cell in state.get("plants", []):
        if 0 <= cell["row"] < rows and 0 <= cell["col"] < 9 and cell["plants"]:
            grid[cell["row"]][cell["col"]] = CODE.get(cell["plants"][-1], "?")
    zombies = [[0, None] for _ in range(rows)]
    for row, col in (scene.get("nearest_col") or {}).items():
        if 0 <= int(row) < rows:
            zombies[int(row)][1] = col
    for zombie in scene.get("zombies", []):
        row, col = zombie.get("row"), zombie.get("col")
        if row is None or not 0 <= row < rows:
            continue
        zombies[row][0] += 1
        if col is not None and (zombies[row][1] is None or col < zombies[row][1]):
            zombies[row][1] = col
    return {"t": tick, "sun": scene.get("sun"), "ready": [c["slot"] for c in scene.get("cards", []) if c.get("ready")],
            "rows": ["".join(r) for r in grid], "z": zombies}


def _affordable(entry: dict, costs: dict[int, int]) -> bool:
    sun = entry.get("sun")
    return sun is not None and any(costs.get(slot, 10**9) <= sun for slot in entry["ready"])


def _attackers(row: str) -> int:
    return sum(NAME.get(ch) in ATTACK for ch in row)


def _decisions_between(decisions, start, end, row=None):
    picked = [d for d in decisions if start <= d["t"] <= end]
    return {"total": len(picked), "waits": sum(d["sig"]["wait"] for d in picked),
            "planted_in_row": None if row is None else sum(row in d["sig"]["rows"] and not d["sig"]["wait"] for d in picked),
            "chosen": dict(Counter(d["sig"]["id"] or ("wait" if d["sig"]["wait"] else "placement") for d in picked))}


def analyze(timeline: list[dict], decisions: list[dict], mowers: list[dict], costs: dict[int, int]) -> dict:
    costly = []
    for event in mowers:
        at = max((e for e in timeline if e["t"] <= event["t"]), key=lambda e: e["t"], default=None)
        row = event["row"]
        costly.append({"kind": "mower", "t": event["t"], "row": row,
                       "lawn_row": at["rows"][row] if at and row < len(at["rows"]) else None,
                       "zombies": at["z"][row] if at and row < len(at["z"]) else None,
                       "sun": at and at["sun"], "ready": at and at["ready"],
                       "decisions_last_10s": _decisions_between(decisions, event["t"] - 1000, event["t"], row),
                       "plan": next((d["plan_brief"] for d in reversed(decisions) if d["t"] <= event["t"]), None)})
    for prev, cur in zip(timeline, timeline[1:]):
        for row, (a, b) in enumerate(zip(prev["rows"], cur["rows"])):
            for col, (x, y) in enumerate(zip(a, b)):
                if x not in ".?" and y == "." and cur["z"][row][0] and (cur["z"][row][1] is None or cur["z"][row][1] <= col + 1):
                    costly.append({"kind": "plant_lost", "t": cur["t"], "row": row, "col": col, "plant": NAME.get(x),
                                   "lawn_row": b, "zombies": cur["z"][row], "sun": cur["sun"]})
    costly.sort(key=lambda c: (c["kind"] != "mower", c["t"]))

    missed, open_rows = [], {}
    def close(row, start, end):
        if end - start["t"] >= MISSED_MIN_TICKS:
            missed.append({"row": row, "t_start": start["t"], "t_end": end, "seconds": (end - start["t"]) / 100,
                           "sun_at_start": start["sun"], "ready_at_start": start["ready"],
                           "lawn_row": start["rows"][row], "zombies": start["z"][row],
                           "decisions": _decisions_between(decisions, start["t"], end, row)})
    for entry in timeline:
        for row, lawn in enumerate(entry["rows"]):
            count, nearest = entry["z"][row]
            threatened = count and nearest is not None and nearest <= THREAT_COL and _attackers(lawn) == 0
            start = open_rows.get(row)
            planted = start is not None and _decisions_between(decisions, start["t"], entry["t"], row)["planted_in_row"]
            if threatened and _affordable(entry, costs) and not planted:
                open_rows.setdefault(row, entry)
            elif start is not None:
                close(row, start, entry["t"])
                del open_rows[row]
    for row, start in open_rows.items():  # still open when the record ends
        close(row, start, timeline[-1]["t"])
    missed.sort(key=lambda m: -m["seconds"])

    return {"costly_moments": costly[:TABLE_LIMIT], "missed_chances": missed[:TABLE_LIMIT],
            "decision_stats": decision_stats(decisions), "alignment": alignment(decisions)}


def decision_stats(decisions: list[dict]) -> dict:
    jev = [d for d in decisions if d.get("source", "jev") == "jev"]
    offered = Counter(i for d in jev for i in d.get("offered", []))
    chosen = Counter(d["sig"]["id"] or ("wait" if d["sig"]["wait"] else "placement") for d in jev)
    return {"jev_decisions": len(jev), "waits": chosen.get("wait", 0),
            "wait_share": None if not jev else round(chosen.get("wait", 0) / len(jev), 3),
            "chosen": dict(chosen.most_common()), "offered": dict(offered.most_common()),
            "decision_unexecutable": sum(bool(d.get("unexecutable")) for d in jev),
            "rejected": sum(bool(d.get("rejected")) for d in jev),
            "reflex_plants": sum(d.get("source") == "reflex" for d in decisions),
            "fallback_plants": sum(d.get("source") == "fallback" and d.get("placement") is not None
                                   and not d.get("rejected") for d in decisions)}


def planner_stats(plans: list[dict], duration_ticks: int) -> dict:
    """plans: ordered {t, trigger, reasons}, one per Planner call that produced a plan."""
    minutes = duration_ticks / 6000
    return {"calls": len(plans), "by_trigger": dict(Counter(p.get("trigger") or "unknown" for p in plans)),
            "per_minute": None if minutes <= 0 else round(len(plans) / minutes, 2),
            "reasons": dict(Counter(r.split(":")[0] for p in plans for r in p.get("reasons") or []).most_common(8))}


def fallback_stats(windows: list[dict], mowers: list[dict]) -> dict:
    """windows: ordered {t_start, t_end, reasons, plants}; player code planted in Jev's place inside each."""
    listed = [{**w, "seconds": (w["t_end"] - w["t_start"]) / 100,
               "mowers": [m["row"] for m in mowers if w["t_start"] <= m["t"] <= w["t_end"]]} for w in windows]
    return {"windows": len(windows), "plants": sum(len(w["plants"]) for w in windows),
            "seconds": round(sum(w["seconds"] for w in listed), 1),
            "mowers_during": sum(len(w["mowers"]) for w in listed),
            "reasons": dict(Counter(r.split(":")[0] for w in windows for r in w["reasons"]).most_common(8)),
            "list": listed[:TABLE_LIMIT]}


def lesson_stats(plans: list[dict], decisions: list[dict], mowers: list[dict]) -> dict:
    """Per lesson: how often it was shown to the Planner and to Jev, and mowers within 10 s after a showing."""
    stats = {}
    for kind, rows in (("planner", plans), ("jev", decisions)):
        for entry in rows:
            for lesson in entry.get("lessons") or []:
                item = stats.setdefault(lesson, {"planner": 0, "jev": 0, "first_t": entry["t"], "times": set()})
                item[kind] += 1
                item["times"].add(entry["t"])
    for item in stats.values():
        item["mowers_within_10s"] = sum(any(0 <= m["t"] - t <= 1000 for t in item["times"]) for m in mowers)
        del item["times"]
    return dict(sorted(stats.items()))


class EpisodeRecorder:
    """Windows-side recorder for one research episode; output goes to the coach only."""

    def __init__(self):
        self.timeline: list[dict] = []
        self.decisions: list[dict] = []
        self.mowers: list[dict] = []
        self.plans: list[dict] = []
        self.fallbacks: list[dict] = []
        self.costs: dict[int, int] = {}
        self.complete = True

    def observe(self, tick: int, state: dict | None, force=False):
        if state is None:
            self.complete = False
            return
        if not self.costs:
            for card in state["scene"].get("cards", []):
                cost = card.get("cost") if card.get("cost") is not None else PLANT_COSTS.get(card["plant"])
                if cost is not None:
                    self.costs[card["slot"]] = cost
        if force or not self.timeline or tick - self.timeline[-1]["t"] >= TIMELINE_EVERY:
            entry = encode_state(state, tick)
            if self.timeline and self.timeline[-1]["t"] == tick:
                self.timeline[-1] = entry
            else:
                self.timeline.append(entry)

    def decision(self, tick: int, choice: dict, placement: dict | None, source="jev"):
        menu = choice.get("decision") or {}
        cards = (choice.get("scene") or {}).get("cards") or []
        plant = cards[placement["slot"]]["plant"] if placement and placement["slot"] < len(cards) else None
        self.decisions.append({
            "t": tick, "source": source, "plan_key": choice.get("plan_tick"), "plan": choice.get("plan"),
            "plan_brief": plan_brief(choice.get("plan")),
            "sig": signature(menu.get("decision"), placement, plant), "placement": placement,
            "offered": [m["id"] for m in menu.get("menu", [])],
            "unexecutable": bool(menu.get("decision_unexecutable")), "rejected": menu.get("rejected"),
            "lessons": ((choice.get("lessons") or {}).get("jev") or []) if source != "reflex" else []})

    def mower(self, tick: int, row: int):
        self.mowers.append({"t": tick, "row": row})

    def plan(self, tick: int, replan: dict | None, lessons=None):
        if self.fallbacks and self.fallbacks[-1]["open"]:
            self.fallbacks[-1].update(t_end=tick, open=False)
        self.plans.append({"t": tick, **(replan or {"trigger": None, "reasons": []}), "lessons": lessons or []})

    def fallback(self, tick: int, step: dict):
        """One step in which player code acted in Jev's place; steps under one plan form a window."""
        if not self.fallbacks or not self.fallbacks[-1]["open"] or self.fallbacks[-1]["window"] != step["window"]:
            self.fallbacks.append({"window": step["window"], "t_start": tick, "t_end": tick, "open": True,
                                   "reasons": step["reasons"], "plants": []})
        current = self.fallbacks[-1]
        current["t_end"] = tick
        if step["action"] == "plant":
            current["plants"].append(step["placement"])

    def report(self) -> dict:
        slim = [{k: v for k, v in d.items() if k != "plan"} for d in self.decisions]
        tables = analyze(self.timeline, self.decisions, self.mowers, self.costs)
        end = self.timeline[-1]["t"] if self.timeline else 0
        tables["planner_calls"] = planner_stats(self.plans, end)
        windows = [{k: v for k, v in w.items() if k not in ("open", "window")} for w in self.fallbacks]
        tables["fallback"] = fallback_stats(windows, self.mowers)
        tables["lessons"] = lesson_stats(self.plans, self.decisions, self.mowers)
        return {"legend": legend(), "timeline_complete": self.complete, "timeline": self.timeline,
                "decisions": slim, "mowers": self.mowers, "plans": self.plans, "fallbacks": windows, "tables": tables}


def episode_tables(report: dict) -> list[dict]:
    """Per-episode tables from a stored development report, for the coach's evidence."""
    return [{"episode": i, "level": ep.get("level"), "outcome": ep.get("outcome"), "score": ep.get("score"),
             **(ep.get("postgame") or {}).get("tables", {"missing": "no post-game record"})}
            for i, ep in enumerate(report.get("episodes", []))]


def summarize(reports: dict[str, dict]) -> dict:
    """Aggregate alignment and decision statistics across named development reports."""
    out = {}
    for name, report in reports.items():
        tables = [t for t in episode_tables(report) if "alignment" in t]
        after = sum(t["alignment"]["after"]["agreed"] for t in tables)
        after_n = sum(t["alignment"]["after"]["total"] for t in tables)
        before = sum(t["alignment"]["before"]["agreed"] for t in tables)
        before_n = sum(t["alignment"]["before"]["total"] for t in tables)
        out[name] = {"episodes": len(tables),
                     "alignment_after": None if not after_n else round(after / after_n, 3),
                     "alignment_before": None if not before_n else round(before / before_n, 3),
                     "plan_changes": sum(t["alignment"]["plan_changes"] for t in tables),
                     "planner_calls": sum((t.get("planner_calls") or {}).get("calls", 0) for t in tables),
                     "planner_calls_per_minute": [(t.get("planner_calls") or {}).get("per_minute") for t in tables],
                     "mower_moments": sum(c["kind"] == "mower" for t in tables for c in t["costly_moments"]),
                     "plants_lost": sum(c["kind"] == "plant_lost" for t in tables for c in t["costly_moments"]),
                     "missed_chances": sum(len(t["missed_chances"]) for t in tables),
                     "wait_share": [t["decision_stats"]["wait_share"] for t in tables],
                     "decision_unexecutable": sum(t["decision_stats"]["decision_unexecutable"] for t in tables)}
    return out


# ---- coach query tools (development reports only) ----

MOMENT_KINDS = ("mower", "plant_lost", "missed_chance", "decision_unexecutable", "rejected", "long_wait", "replan",
                "fallback")


def query_episode(report: dict, episode: int, t_from: int, t_to: int) -> dict:
    episodes = report.get("episodes", [])
    if not 0 <= episode < len(episodes):
        raise ValueError(f"episode must be 0..{len(episodes) - 1}")
    if t_to < t_from or t_to - t_from > 6000:
        raise ValueError("query at most 60 game seconds (6000 ticks) at a time")
    record = episodes[episode].get("postgame") or {}
    return {"episode": episode, "level": episodes[episode].get("level"), "legend": record.get("legend"),
            "timeline": [e for e in record.get("timeline", []) if t_from <= e["t"] <= t_to],
            "decisions": [d for d in record.get("decisions", []) if t_from <= d["t"] <= t_to],
            "mowers": [m for m in record.get("mowers", []) if t_from <= m["t"] <= t_to],
            "plans": [p for p in record.get("plans", []) if t_from <= p["t"] <= t_to],
            "fallbacks": [w for w in record.get("fallbacks", []) if w["t_start"] <= t_to and w["t_end"] >= t_from]}


def find_moments(report: dict, kind: str) -> list[dict]:
    if kind not in MOMENT_KINDS:
        raise ValueError(f"kind must be one of {MOMENT_KINDS}")
    found = []
    for i, ep in enumerate(report.get("episodes", [])):
        record = ep.get("postgame") or {}
        tables = record.get("tables", {})
        if kind in {"mower", "plant_lost"}:
            found += [{"episode": i, "t": c["t"], "row": c["row"]} for c in tables.get("costly_moments", []) if c["kind"] == kind]
        elif kind == "missed_chance":
            found += [{"episode": i, "t": m["t_start"], "row": m["row"], "seconds": m["seconds"]}
                      for m in tables.get("missed_chances", [])]
        elif kind == "replan":
            found += [{"episode": i, **p} for p in record.get("plans", [])]
        elif kind == "fallback":
            found += [{"episode": i, "t": w["t_start"], **w} for w in record.get("fallbacks", [])]
        elif kind in {"decision_unexecutable", "rejected"}:
            key = "unexecutable" if kind == "decision_unexecutable" else "rejected"
            found += [{"episode": i, "t": d["t"], "decision": d["sig"]["id"], "detail": d.get(key)}
                      for d in record.get("decisions", []) if d.get(key)]
        else:  # long_wait: 5+ consecutive Jev waits
            run = []
            for d in record.get("decisions", []) + [{"t": None, "sig": {"wait": False}, "source": "jev"}]:
                if d.get("source", "jev") != "jev":
                    continue
                if d["sig"]["wait"]:
                    run.append(d["t"])
                    continue
                if len(run) >= 5:
                    found.append({"episode": i, "t": run[0], "until": run[-1], "waits": len(run)})
                run = []
    return found[:50]


# ---- live games (player's view, from events.jsonl) ----

def analyze_live(events: list[dict]) -> dict:
    """Same tables from a live session log. Lawn facts are model observations, not truth."""
    if not events:
        return {}
    start = events[0]["t_server"]
    decisions, mowers, plans, fallbacks, seen_rows = [], [], [], [], set()
    contexts = []
    for event in events:
        t = int(round((event["t_server"] - start) * 100))
        if event["kind"] == "jev":
            guidance = event.get("guidance")
            ctx = event.get("decision_context") or {}
            scene = ctx.get("scene") or {}
            cards = scene.get("cards") or []
            placement = event.get("placement") if not event.get("rejected") else None
            plant = cards[placement["slot"]]["plant"] if placement and placement["slot"] < len(cards) else None
            decisions.append({"t": t, "source": "jev", "plan_key": plan_key(guidance), "plan": guidance,
                              "plan_brief": plan_brief(guidance),
                              "sig": signature(event.get("decision"), placement, plant), "placement": placement,
                              "offered": [m["id"] for m in event.get("menu") or []],
                              "unexecutable": bool(event.get("decision_unexecutable")), "rejected": event.get("rejected"),
                              "lessons": event.get("lessons") or []})
            if scene:
                contexts.append(encode_state({"scene": {**scene, "sun": None if scene.get("sun") is None else
                                                        scene["sun"] - ctx.get("sun_spent_since_scene", 0)},
                                              "plants": ctx.get("plants", [])}, t))
        elif event["kind"] == "plan" and event.get("plan"):
            context = (event.get("observed_context") or {}).get("context") or {}
            plans.append({"t": t, "trigger": event.get("trigger"), "reasons": event.get("trigger_reasons") or [],
                          "lessons": [l["id"] for l in context.get("lessons_now") or []]})
        elif event["kind"] == "fallback_start":
            fallbacks.append({"t_start": t, "t_end": t, "reasons": event.get("reasons") or [], "plants": []})
        elif event["kind"] == "fallback" and fallbacks:
            fallbacks[-1]["t_end"] = t
            if event.get("action") == "plant" and not event.get("rejected"):
                fallbacks[-1]["plants"].append(event.get("placement"))
                decisions.append({"t": t, "source": "fallback", "plan_key": None, "plan": None, "plan_brief": None,
                                  "sig": signature(None, event.get("placement"), None), "placement": event.get("placement")})
        elif event["kind"] == "fallback_end" and fallbacks:
            fallbacks[-1]["t_end"] = t
        elif event["kind"] == "reflex_plant" and not event.get("rejected"):
            decisions.append({"t": t, "source": "reflex", "plan_key": None, "plan": None, "plan_brief": None,
                              "sig": signature(None, event.get("placement"), None), "placement": event.get("placement")})
        elif event["kind"] == "state":
            for mower in (event.get("data") or {}).get("mower_events", []):
                if mower.get("row") not in seen_rows:
                    seen_rows.add(mower.get("row"))
                    mowers.append({"t": t, "row": mower["row"]})
    costs = {}
    for event in events:
        if event["kind"] == "jev" and (event.get("decision_context") or {}).get("scene"):
            for card in event["decision_context"]["scene"].get("cards", []):
                cost = card.get("cost") if card.get("cost") is not None else PLANT_COSTS.get(card["plant"])
                if cost is not None:
                    costs[card["slot"]] = cost
            break
    tables = analyze(contexts, decisions, mowers, costs)
    tables["planner_calls"] = planner_stats(plans, int(round((events[-1]["t_server"] - start) * 100)))
    tables["fallback"] = fallback_stats(fallbacks, mowers)
    tables["lessons"] = lesson_stats(plans, decisions, mowers)
    tables["view"] = "player observations at Jev decisions (model-read, may be wrong); times in session ticks"
    return tables
