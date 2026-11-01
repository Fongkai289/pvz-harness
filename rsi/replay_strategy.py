"""Bounded real-model strategy experiment on recorded player observations.

No mouse executor, simulated progress, CURRENT writes or automatic promotion.
python -m rsi.replay_strategy --session NAME --decision-id 19 --coach
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json

from rsi.coach import propose, diff
from rsi.decision_evidence import decision_snapshots
from rsi.review_live import read_events, select_session, build_evidence
from rsi.code_store import write_json
from server.action_selector import DecisionContext, OneJevSelector, candidates
from server.config import load_settings, effective_settings
from server.planner import Plan, QwenPlanner
from server.playbook import PlaybookStore
from server.scene import Scene
from server.threats import observed_threats


async def experiment(settings, session, decision_id, use_coach, repeats=2):
    path = select_session(settings.runs_dir, session)
    events = read_events(path)
    snapshot = next(d for d in decision_snapshots(events) if d["decision_id"] == decision_id)
    store = PlaybookStore(settings.data_dir)
    current_before = store.current()
    book = store.load(events[0]["playbook_loaded"])
    output = settings.data_dir / "strategy_replays" / (session + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True)
    record = {"session": session, "decision_id": decision_id, "snapshot": snapshot,
              "scope": "fixed recorded observations; text-only planner; no game execution or outcome evaluation",
              "promoted": False, "runs": []}
    write_json(output / "snapshot.json", snapshot)
    original = Plan.model_validate(snapshot["guidance"])
    context = snapshot["decision_context"]
    scene = Scene.model_validate(context["scene"])
    planted = {(p["row"],p["col"]):p["plants"] for p in context["plants"]}

    async def run(label, active_book, refresh):
        active_settings = effective_settings(settings, active_book)
        plan = original
        planning = None
        if refresh:
            planner = QwenPlanner(active_settings.qwen_url, active_settings.qwen_model, False, True, active_settings.planner_timeout)
            try:
                result = await planner.decide(scene, active_book.planner.knowledge, context={
                    "plants": context["plants"], "previous_guidance_for_reference_only": original.model_dump(),
                    "committed_spend_under_previous_guidance": snapshot["committed_spend"],
                    "observed_threats": observed_threats(scene, planted, context["layout_fresh"]),
                    "replan_reasons": ["Recorded resource and card observations are available for reassessment"]})
            finally:
                await planner.client.aclose()
            planning = {"latency_s": result.latency, "error": result.error,
                        "plan": result.plan.model_dump() if result.plan else None}
            if result.plan is None:
                record["runs"].append({"label":label,"planning":planning,"decisions":[]})
                return
            plan = result.plan
        ctx = DecisionContext(scene, planted, set(context["used_slots"]), plan,
                              context["sun_spent_since_scene"], 0 if refresh else snapshot["committed_spend"],
                              layout_fresh=context["layout_fresh"], strategy_knowledge=active_book.planner.knowledge)
        offered = candidates(ctx)
        item = {"label":label,"planning":planning,"budget_advice":ctx.budget_message(),
                "scope": "Fixed-state replay exercises message changes only; timing changes cannot be evaluated here.",
                "offered_plant_rows": sorted({(scene.cards[p.slot].plant,p.row) for p in offered}),"decisions":[]}
        selector = OneJevSelector(active_settings.onejev_url, active_settings.onejev_model, active_settings.decider_timeout)
        try:
            for _ in range(repeats):
                choice = await selector.select(ctx, active_book.jev)
                item["decisions"].append({"placement": choice.placement.model_dump() if choice.placement else None,
                    "plant": scene.cards[choice.placement.slot].plant if choice.placement else None,
                    "error":choice.error,"latency_s":choice.latency,"scores":choice.scores})
        finally:
            await selector.client.aclose()
        record["runs"].append(item)
        write_json(output / "result.json", record)
        print(json.dumps({"label":label,"planning":planning,"decisions":[{k:v for k,v in d.items() if k!='scores'} for d in item['decisions']]},ensure_ascii=False),flush=True)

    await run("same_historical_guidance_new_contract", book, False)
    await run("fresh_planner_new_contract", book, True)
    if use_coach:
        print("Requesting Coach proposal from complete segment evidence",flush=True)
        proposal, error = await propose(book, {"game":"real_windows_replay", "evidence":build_evidence(events)}, [], settings)
        record["coach_error"] = error
        if proposal is not None:
            write_json(output / "proposal.json", proposal.model_dump())
            record["coach_proposal"] = proposal.model_dump()
            record["coach_changes"] = diff(book, proposal)
            # Memory-only candidate; not a new stored/live playbook version.
            candidate = proposal.apply(book, book.version)
            await run("coach_candidate_fresh_planner", candidate, True)
    record["current_before"] = current_before
    record["current_after"] = store.current()
    assert record["current_after"] == current_before, "Replay must not promote a version"
    write_json(output / "result.json", record)
    print("RESULT_DIR="+str(output),flush=True)
    return record, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--decision-id", type=int, required=True)
    parser.add_argument("--coach", action="store_true")
    parser.add_argument("--repeats", type=int, choices=range(1,4), default=2)
    args = parser.parse_args()
    asyncio.run(experiment(load_settings(), args.session, args.decision_id, args.coach, args.repeats))


if __name__ == "__main__":
    main()
