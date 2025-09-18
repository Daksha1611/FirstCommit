"""The target branch: a goal becomes a requisition that fits.

The division of labour is what these assert. The model reads the goal and ranks
each line; the code decides what survives the budget. A model asked to stay
under a number will simply say it did.
"""

import pytest

from agent.graph import NotSupported, interpret
from agent.nodes import Plan, PlannedLine, fit_to_budget, plan_requisition
from agent.nodes.intake_node import IntakeCommands, IntakeOutput, intake_chain
from agent.nodes.planner_node import planner_chain
from merchant import catalog

RUPEE = 100


def plan(*lines, reasoning="two engineers need kit", assumptions=("assumed two people",)):
    return Plan(
        reasoning=reasoning,
        assumptions=list(assumptions),
        lines=[PlannedLine(sku=s, quantity=q, priority=p, rationale="because")
               for s, q, p in lines],
    )


FULL = plan(
    ("LAP-PRO-1", 2, "essential"),
    ("MON-27Q-1", 2, "important"),
    ("CHR-ERG-1", 2, "important"),
    ("HDS-ANC-1", 2, "optional"),
)


def cost(sku, qty):
    return catalog.get(sku).price_paise * qty


# --- fitting a plan to a budget --------------------------------------------


def test_a_generous_budget_keeps_everything():
    got = fit_to_budget(FULL, 500_000 * RUPEE)
    assert got.affordable
    assert got.dropped == ()


def test_optional_goes_before_important():
    """Ranking is the planner's judgement; acting on it is the code's job."""
    budget = cost("LAP-PRO-1", 2) + cost("MON-27Q-1", 2) + cost("CHR-ERG-1", 2)
    got = fit_to_budget(FULL, budget)
    kept = {line.sku for line in got.lines}
    assert "HDS-ANC-1" not in kept
    assert "CHR-ERG-1" in kept


def test_essentials_survive_a_tight_budget():
    got = fit_to_budget(FULL, cost("LAP-PRO-1", 2))
    assert [line.sku for line in got.lines] == ["LAP-PRO-1"]


def test_an_unaffordable_goal_says_so_rather_than_trimming_essentials():
    """Silently dropping something the planner called essential would answer a
    different question than the one that was asked."""
    got = fit_to_budget(FULL, 1_000 * RUPEE)
    assert not got.affordable
    assert [line.sku for line in got.lines] == ["LAP-PRO-1"]
    assert got.total_paise > got.budget_paise


def test_the_most_expensive_line_in_a_band_is_dropped_first():
    """One large optional goes before several small ones."""
    p = plan(
        ("STA-A4-5", 1, "essential"),
        ("SRV-RCK-1", 1, "optional"),
        ("TNR-LSR-1", 1, "optional"),
    )
    got = fit_to_budget(p, cost("STA-A4-5", 1) + cost("TNR-LSR-1", 1))
    kept = {line.sku for line in got.lines}
    assert "TNR-LSR-1" in kept and "SRV-RCK-1" not in kept


def test_an_invented_sku_is_dropped_not_priced():
    p = plan(("LAP-PRO-1", 1, "essential"), ("MADE-UP-9", 1, "essential"))
    got = fit_to_budget(p, 500_000 * RUPEE)
    assert [line.sku for line in got.lines] == ["LAP-PRO-1"]
    assert any(line.sku == "MADE-UP-9" for line in got.dropped)


def test_what_was_dropped_is_recorded():
    got = fit_to_budget(FULL, cost("LAP-PRO-1", 2))
    assert len(got.dropped) == 3
    assert got.as_dict()["assumptions"] == ["assumed two people"]


def test_quantities_roll_up_into_a_cart():
    p = plan(("LAP-PRO-1", 2, "essential"), ("LAP-PRO-1", 1, "important"))
    assert fit_to_budget(p, 900_000 * RUPEE).to_cart() == {"LAP-PRO-1": 3}


# --- the branch -------------------------------------------------------------


def classified(kind, goal="", items=(), budget=None):
    return IntakeOutput(
        task_kind=kind, goal=goal,
        items=[{"name": n, "quantity": q} for n, q in items],
        commands=IntakeCommands(budget_paise=budget), reasoning="because",
    )


def test_a_target_is_planned(monkeypatch):
    monkeypatch.setattr(intake_chain, "invoke", lambda _v: classified(
        "target", goal="kit out two engineers", budget=500_000 * RUPEE))
    monkeypatch.setattr(planner_chain, "invoke", lambda _v: FULL)

    intent = interpret("kit out two new engineers under 5 lakh", department="engineering")
    assert intent.kind == "target"
    assert intent.planned
    assert intent.cart["LAP-PRO-1"] == 2
    assert intent.summary()["assumptions"]


def test_an_errand_is_not_planned(monkeypatch):
    """The lines are already named; planning them would be inventing work."""
    def explode(_variables):
        raise AssertionError("an errand must not reach the planner")

    monkeypatch.setattr(intake_chain, "invoke", lambda _v: classified(
        "errand", goal="buy 4 laptops", items=[("LAP-STD-1", "4")]))
    monkeypatch.setattr(planner_chain, "invoke", explode)

    intent = interpret("buy 4 standard laptops")
    assert intent.kind == "errand"
    assert not intent.planned
    assert intent.requested_items == ("4 LAP-STD-1",)
    assert intent.cart == {}


def test_a_target_without_a_budget_is_refused(monkeypatch):
    """Planning against an unknown ceiling is inventing permission to spend."""
    monkeypatch.setattr(intake_chain, "invoke", lambda _v: classified(
        "target", goal="kit out the new team"))
    with pytest.raises(NotSupported, match="needs a budget"):
        interpret("kit out the new team")


def test_a_standing_request_without_a_budget_is_refused(monkeypatch):
    """Without a period cap it is an open-ended claim on the company's money."""
    monkeypatch.setattr(intake_chain, "invoke", lambda _v: classified(
        "standing", goal="keep stock topped up"))
    with pytest.raises(NotSupported, match="needs a budget per period"):
        interpret("keep the stationery cupboard stocked")


def test_errand_items_stay_as_words(monkeypatch):
    """Intake records phrases; resolving them to SKUs is the shoppers' job."""
    monkeypatch.setattr(intake_chain, "invoke", lambda _v: classified(
        "errand", items=[("standard laptops", "a pack"), ("toner", "2")]))
    intent = interpret("some laptops and two toners")
    assert intent.requested_items == ("a pack standard laptops", "2 toner")


def test_an_empty_plan_is_a_failure_not_a_free_lunch():
    """Seen live: the model returned reasoning and assumptions but no lines,
    and the result read as an affordable zero-rupee requisition."""
    empty = Plan(reasoning="thought about it", assumptions=["two people"], lines=[])
    got = fit_to_budget(empty, 500_000 * RUPEE)
    assert not got.affordable
    assert "no usable lines" in got.problem


# --- flaky structured output -----------------------------------------------


def test_an_empty_plan_is_retried(monkeypatch):
    """The model intermittently returns reasoning with no lines, on inputs it
    handles fine moments later. Notice and ask again."""
    calls = []
    empty = Plan(reasoning="thought about it", assumptions=[], lines=[])

    def flaky(_variables):
        calls.append(1)
        return empty if len(calls) < 3 else FULL

    monkeypatch.setattr(planner_chain, "invoke", flaky)
    got = plan_requisition(request="kit out two engineers", budget_paise=500_000 * RUPEE)
    assert len(calls) == 3
    assert got.lines and got.affordable


def test_giving_up_reports_failure_rather_than_inventing_a_plan(monkeypatch):
    empty = Plan(reasoning="nothing came to mind", assumptions=[], lines=[])
    monkeypatch.setattr(planner_chain, "invoke", lambda _v: empty)

    got = plan_requisition(request="kit out two engineers", budget_paise=500_000 * RUPEE)
    assert not got.affordable
    assert "no usable lines" in got.problem
    assert got.lines == ()


def test_a_plan_of_only_invented_skus_counts_as_empty(monkeypatch):
    """Lines that price against nothing are not lines."""
    bogus = Plan(reasoning="", assumptions=[],
                 lines=[PlannedLine(sku="NOPE-1", quantity=1, priority="essential",
                                    rationale="invented")])
    calls = []

    def flaky(_variables):
        calls.append(1)
        return bogus if len(calls) < 2 else FULL

    monkeypatch.setattr(planner_chain, "invoke", flaky)
    got = plan_requisition(request="x", budget_paise=500_000 * RUPEE)
    assert len(calls) == 2
    assert got.affordable
