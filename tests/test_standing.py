"""The standing branch: an instruction that keeps running.

Two properties carry it, and both are about restraint rather than capability:

  a tick buys nothing when nothing is below its reorder point
  the period budget survives a restart, so a fresh mandate does not reset it

A standing order that finds something to buy every time it runs is not a policy,
it is a leak.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agent.graph import NotSupported, interpret, register_standing
from agent.nodes import tick
from agent.nodes.intake_node import IntakeCommands, IntakeOutput, intake_chain
from agent.nodes.standing_node import PolicyLine, StandingPolicy, establish, standing_chain
from merchant import stock
from pocketchange.memory import InMemoryBank, ReorderRule, new_order

RUPEE = 100

RULES = (
    ReorderRule("STA-A4-5", reorder_point=3, target_level=12),
    ReorderRule("TNR-LSR-1", reorder_point=2, target_level=6),
    ReorderRule("CBL-CAT6-1", reorder_point=4, target_level=20),
)


def order(budget=50_000 * RUPEE, period="month", approved=True):
    """An approved policy by default - most tests are about what a running one does.

    Approval itself is exercised separately below.
    """
    made = new_order(
        instruction="keep the stationery cupboard stocked", department="operations",
        period=period, period_budget_paise=budget, rules=RULES,
    )
    return replace(made, approved=approved) if approved else made


def full_cupboard():
    return tuple(
        stock.StockLevel(l.sku, l.target_level, l.reorder_point, l.target_level)
        for l in stock.levels()
    )


# --- restraint --------------------------------------------------------------


def test_a_full_cupboard_buys_nothing():
    """The normal outcome, and the one an unattended agent must get right."""
    got = tick(order(), full_cupboard())
    assert not got.acts
    assert got.due == ()
    assert "nothing is below" in got.reason


def test_only_lines_below_their_reorder_point_are_bought():
    got = tick(order())
    bought = {line["sku"] for line in got.due}
    assert bought == {"STA-A4-5", "TNR-LSR-1"}     # CBL is above its point
    assert got.acts


def test_it_tops_up_to_the_target_not_by_one():
    got = tick(order())
    paper = next(line for line in got.due if line["sku"] == "STA-A4-5")
    assert paper["quantity"] == 12 - 2


def test_an_unaffordable_tick_buys_nothing_rather_than_some():
    """Partial spending is a decision nobody authorised."""
    got = tick(order(budget=1_000 * RUPEE))
    assert not got.affordable
    assert not got.acts
    assert got.due != ()          # it still reports what it wanted
    assert "left this month" in got.reason


def test_a_rule_for_something_not_stocked_is_skipped():
    o = replace(order(), rules=(ReorderRule("SRV-RCK-1", 1, 5),))
    assert tick(o).due == ()


# --- memory across sessions -------------------------------------------------


def test_the_period_budget_survives_between_ticks():
    """Each tick mints a fresh mandate; without memory the cap would reset."""
    bank = InMemoryBank()
    o = bank.remember(order())
    o = bank.record_check(o.id, spent_paise=41_500 * RUPEE, note="topped up")
    assert o.remaining_paise == 8_500 * RUPEE

    with pytest.raises(Exception, match="left this month"):
        bank.record_check(o.id, spent_paise=20_000 * RUPEE, note="again")


def test_the_period_rolls_and_the_budget_resets_once():
    bank = InMemoryBank()
    o = bank.remember(order())
    o = bank.record_check(o.id, spent_paise=40_000 * RUPEE, note="first")
    assert o.remaining_paise == 10_000 * RUPEE

    stale = replace(o, period_started_at=datetime.now(timezone.utc) - timedelta(days=40))
    bank.remember(stale)
    rolled = bank.record_check(o.id, spent_paise=40_000 * RUPEE, note="new period")
    assert rolled.remaining_paise == 10_000 * RUPEE


def test_checks_and_notes_are_remembered():
    bank = InMemoryBank()
    o = bank.remember(order())
    for n in range(3):
        o = bank.record_check(o.id, spent_paise=100 * RUPEE, note=f"tick {n}")
    assert o.checks == 3
    assert o.notes[-1] == "tick 2"


def test_only_approved_orders_are_listed():
    """The list is what may actually run, not what has been drafted."""
    bank = InMemoryBank()
    bank.remember(order(approved=False))
    assert bank.standing_orders("operations") == []
    bank.remember(order())
    assert [o.department for o in bank.standing_orders("operations")] == ["operations"]
    assert bank.standing_orders("engineering") == []


# --- setting one up ---------------------------------------------------------


def policy(*lines):
    return StandingPolicy(
        reasoning="a small office gets through paper and toner",
        assumptions=["assumed roughly one ream a week"],
        lines=[PolicyLine(sku=s, reorder_point=r, target_level=t, rationale="because")
               for s, r, t in lines],
    )


def test_establish_writes_down_a_policy(monkeypatch):
    monkeypatch.setattr(standing_chain, "invoke",
                        lambda _v: policy(("STA-A4-5", 3, 12), ("TNR-LSR-1", 2, 6)))
    got = establish(instruction="keep the cupboard stocked", department="operations",
                    period="month", budget_paise=50_000 * RUPEE)
    assert {rule.sku for rule in got.rules} == {"STA-A4-5", "TNR-LSR-1"}
    assert got.remaining_paise == 50_000 * RUPEE


def test_invented_skus_and_nonsense_levels_are_dropped(monkeypatch):
    """A target at or below the reorder point would reorder forever."""
    monkeypatch.setattr(standing_chain, "invoke", lambda _v: policy(
        ("STA-A4-5", 3, 12), ("MADE-UP-1", 1, 5), ("TNR-LSR-1", 5, 5)))
    got = establish(instruction="x", department="operations", period="month",
                    budget_paise=50_000 * RUPEE)
    assert {rule.sku for rule in got.rules} == {"STA-A4-5"}


def test_a_policy_covering_nothing_is_refused(monkeypatch):
    monkeypatch.setattr(standing_chain, "invoke", lambda _v: policy(("MADE-UP-1", 1, 5)))
    with pytest.raises(ValueError, match="no usable reorder rules"):
        establish(instruction="keep the servers stocked", department="ops",
                  period="month", budget_paise=50_000 * RUPEE)


def test_the_branch_produces_a_policy_not_a_purchase(monkeypatch):
    monkeypatch.setattr(intake_chain, "invoke", lambda _v: IntakeOutput(
        task_kind="standing", goal="keep the cupboard stocked", items=[],
        commands=IntakeCommands(budget_paise=50_000 * RUPEE), reasoning="recurring",
    ))
    monkeypatch.setattr(standing_chain, "invoke",
                        lambda _v: policy(("STA-A4-5", 3, 12), ("TNR-LSR-1", 2, 6)))

    intent = interpret("keep the stationery cupboard stocked", department="operations")
    assert intent.kind == "standing"
    assert intent.cart == {}                  # a policy, not a purchase
    assert intent.standing_order is not None

    bank = InMemoryBank()
    saved, first = register_standing(intent, bank)
    assert bank.recall(saved.id).instruction.startswith("keep the stationery")

    # Inert until a person says so, even though the cupboard really is low.
    assert not saved.approved
    assert not first.acts
    assert "not been approved" in first.reason

    approved = bank.approve(saved.id, by="somay")
    assert tick(approved).acts


# --- approval gate ----------------------------------------------------------


def test_an_unapproved_policy_does_nothing():
    """A standing order creates recurring authority from one sentence, in the
    one mode where nobody is watching. That is where approval belongs."""
    got = tick(order(approved=False))
    assert not got.acts
    assert got.due == ()
    assert "not been approved" in got.reason


def test_approval_makes_it_run():
    bank = InMemoryBank()
    o = bank.remember(order(approved=False))
    assert not tick(bank.recall(o.id)).acts
    assert tick(bank.approve(o.id, by="somay")).acts


def test_approval_is_recorded_on_the_order():
    bank = InMemoryBank()
    o = bank.remember(order(approved=False))
    assert "approved by somay" in bank.approve(o.id, by="somay").notes


# --- a tick that has grown too large ----------------------------------------


def test_a_large_tick_escalates_rather_than_acting(monkeypatch):
    """Prices move, several lines come due at once. A tick that has grown into
    a critical-tier purchase should meet the same threshold a person would."""
    import agent.nodes.standing_node as node
    from agent.tiers import Tier, policy_for

    class Loud:
        tier = Tier.CRITICAL
        policy = policy_for(Tier.CRITICAL)

    monkeypatch.setattr(node, "triage", lambda **kwargs: Loud())
    got = tick(order())
    assert not got.acts
    assert "needs a person" in got.reason
    assert got.due != ()          # it still reports what it wanted to buy


# --- bounds on what a policy may say ----------------------------------------


def test_a_stockpile_target_is_refused():
    from agent.nodes import bound_rules

    kept, rejected = bound_rules(
        [PolicyLine(sku="STA-A4-5", reorder_point=3, target_level=400, rationale="x")],
        50_000 * RUPEE,
    )
    assert kept == ()
    assert "tops up by 397" in rejected[0].reason


def test_a_standing_order_over_infrastructure_is_refused():
    """Consumables run out. Rack servers do not, and a recurring order for one
    would be alarming however reasonable its numbers looked."""
    from agent.nodes import bound_rules

    kept, rejected = bound_rules(
        [PolicyLine(sku="SRV-RCK-1", reorder_point=1, target_level=3, rationale="x")],
        5_000_000 * RUPEE,
    )
    assert kept == ()
    assert "not something a standing order may cover" in rejected[0].reason


def test_rejections_are_recorded_for_the_approver(monkeypatch):
    """Someone approving a policy should see what it does NOT cover."""
    monkeypatch.setattr(standing_chain, "invoke", lambda _v: policy(
        ("STA-A4-5", 3, 12), ("SRV-RCK-1", 1, 3)))
    got = establish(instruction="keep things stocked", department="operations",
                    period="month", budget_paise=50_000 * RUPEE)
    assert any("rejected SRV-RCK-1" in note for note in got.notes)


def test_a_policy_awaiting_approval_is_invisible_by_default_and_findable_on_request():
    """The tick loop must only ever see policies a person authorised, so the
    default stays narrow. But "inert until approved" is the whole security story
    here, and a policy waiting on a human was invisible to every reader - which
    made the most interesting state the one nothing could show."""
    from datetime import datetime, timezone

    from pocketchange import memory

    bank = memory.InMemoryBank()
    order = memory.new_order(
        instruction="keep the cupboard stocked", department="operations",
        period="month", period_budget_paise=600_000,
        rules=(memory.ReorderRule("STA-A4-5", 5, 20),),
    )
    bank.remember(order)

    assert bank.standing_orders() == []                      # nothing may run yet
    pending = bank.standing_orders(include_pending=True)
    assert len(pending) == 1 and not pending[0].approved

    bank.approve(order.id)
    assert len(bank.standing_orders()) == 1                  # now it may run
    assert len(bank.standing_orders(include_pending=True)) == 1
