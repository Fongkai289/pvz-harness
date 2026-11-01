import asyncio
import json

from server.action_selector import candidates
from server.planner import Plan, Placement
from rsi.decision_evidence import decision_snapshots
from tests.test_action_selector import context, scorer, selector, session
from tests.test_server import settings
from rules.playbook_rules import JevPolicy


def test_bottom_row_regression_125_sun_and_old_50_advice_do_not_hide_shooter():
    ctx = context(spend_limit=50, sun_reserve=150, blocked_rows=[4])
    ctx.scene.sun = 125
    ctx.planted = {(4,0): ["sunflower"], (4,4): ["wall-nut"]}
    offered = candidates(ctx)
    assert Placement(slot=0, row=4, col=1) in offered
    assert not any(p.slot == 5 for p in offered)  # 175 actual cost still forbidden
    assert Placement(slot=0, row=4, col=0) not in offered  # occupied cell still forbidden


def test_budget_200_then_50_spent_is_150_in_next_message_and_not_reset():
    ctx = context(spend_limit=200, budget_policy="Reassess when observed resources change")
    ctx.directive_spent = 50
    ctx.strategy_knowledge = "A coach-editable test strategy."
    seen = []
    asyncio.run(selector(scorer(seen=seen)).select(ctx, JevPolicy()))
    state, _ = json.JSONDecoder().raw_decode(seen[0]["state"])
    assert state["planner_guidance"]["spend_limit"] == 200
    assert state["budget_remaining"] == state["budget_advice"]["remaining_advice"] == 150
    assert state["budget_advice"]["committed_spend"] == 50
    assert state["strategy_knowledge"] == ctx.strategy_knowledge
    ctx.directive_spent = 250
    assert ctx.budget_message()["remaining_advice"] == -50 and candidates(ctx)
    assert Plan(objective="No budget specified").spend_limit is None


def test_threat_judgment_and_tactics_are_message_data():
    plan = Plan(objective="Assess", tactics="Use the reference arming delay and observed movement.",
                threat_assessment=[{"row":4,"urgency":"critical","reason":"No attacker in this lane"}])
    assert plan.threat_assessment[0].row == 4
    ctx = context()
    ctx.directive = plan
    assert any(p.row == 0 for p in candidates(ctx))  # assessment itself does not exclude other rows


def test_review_uses_recorded_context_and_never_joins_judge_truth():
    context_data = {"scene": {"sun":125}, "budget_advice": {"remaining_advice":50}}
    events = [{"kind":"state", "data":{"sun":9999}},
              {"kind":"jev", "decision_id":19, "decision_context":context_data,
               "guidance":{"spend_limit":50}, "budget_spent":0,
               "placement":{"slot":1,"row":4,"col":3},"action":"plant"}]
    record = decision_snapshots(events)[0]
    assert record["provenance"] == "recorded_request_context"
    assert record["decision_context"]["scene"]["sun"] == 125
    assert "9999" not in json.dumps(record)
