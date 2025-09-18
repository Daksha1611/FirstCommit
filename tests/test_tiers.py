"""Scrutiny tiers: rules set the floor, the model may only raise it.

The asymmetry is the whole discipline. Lowering scrutiny is the direction an
attacker wants and the direction a helpful model drifts, so the code must make
it impossible rather than discourage it in a prompt.
"""

import pytest

from agent.nodes import triage
from agent.nodes.triage_node import TriageOpinion, triage_chain
from agent.tiers import (
    ELEVATED_CEILING,
    ROUTINE_CEILING,
    STANDARD_CEILING,
    Tier,
    policy_for,
    settle_tier,
    tier_from_facts,
)

RUPEE = 100


def facts(amount, known=True, established=True):
    return tier_from_facts(
        amount_paise=amount, all_suppliers_known=known, all_suppliers_established=established
    )


# --- thresholds ------------------------------------------------------------


@pytest.mark.parametrize("amount,expected", [
    (ROUTINE_CEILING - 1, Tier.ROUTINE),
    (ROUTINE_CEILING, Tier.STANDARD),
    (STANDARD_CEILING - 1, Tier.STANDARD),
    (STANDARD_CEILING, Tier.ELEVATED),
    (ELEVATED_CEILING - 1, Tier.ELEVATED),
    (ELEVATED_CEILING, Tier.CRITICAL),
])
def test_amount_thresholds(amount, expected):
    assert facts(amount) == expected


def test_an_unknown_supplier_is_critical_at_any_amount():
    """The risk is the counterparty, not the sum."""
    assert facts(1, known=False) == Tier.CRITICAL


def test_an_unestablished_supplier_lifts_the_floor_to_elevated():
    assert facts(100, established=False) == Tier.ELEVATED


def test_an_unestablished_supplier_never_lowers_a_tier():
    assert facts(ELEVATED_CEILING + 1, established=False) == Tier.CRITICAL


# --- the one-directional rule ----------------------------------------------


def test_the_model_cannot_lower_a_tier():
    assert settle_tier(Tier.CRITICAL, "routine") is Tier.CRITICAL
    assert settle_tier(Tier.ELEVATED, "standard") is Tier.ELEVATED


def test_the_model_can_raise_a_tier():
    assert settle_tier(Tier.ROUTINE, "critical") is Tier.CRITICAL


def test_an_unparseable_opinion_keeps_the_floor():
    assert settle_tier(Tier.STANDARD, "whatever") is Tier.STANDARD
    assert settle_tier(Tier.STANDARD, None) is Tier.STANDARD


# --- what a tier costs -----------------------------------------------------


def test_effort_rises_with_tier():
    shoppers = [policy_for(t).shoppers for t in
                (Tier.ROUTINE, Tier.STANDARD, Tier.ELEVATED, Tier.CRITICAL)]
    assert shoppers == sorted(shoppers)


def test_only_critical_requires_a_human():
    assert policy_for(Tier.CRITICAL).human_approval
    assert not any(policy_for(t).human_approval
                   for t in (Tier.ROUTINE, Tier.STANDARD, Tier.ELEVATED))


def test_sanctions_fail_closed_only_at_critical():
    """A watchlist check that silently passes when the service is down is worse
    than none — but only where the stakes justify blocking the purchase."""
    assert policy_for(Tier.CRITICAL).sanctions_fails_closed
    assert not policy_for(Tier.ELEVATED).sanctions_fails_closed


def test_routine_costs_nothing_external():
    p = policy_for(Tier.ROUTINE)
    assert p.shoppers == 1 and not p.sanctions and not p.benchmark


# --- the node --------------------------------------------------------------


def test_routine_never_calls_the_model(monkeypatch):
    """Asking a model whether a 1,450 rupee box of paper is suspicious costs
    more than the paper."""
    def explode(_variables):
        raise AssertionError("the model must not be called at routine tier")

    monkeypatch.setattr(triage_chain, "invoke", explode)
    decision = triage(department="operations", total_paise=1_450 * RUPEE,
                      lines=[], supplier_ids=("northgate-supply",))
    assert decision.tier is Tier.ROUTINE
    assert not decision.raised_by_model


def test_the_model_may_raise_through_the_node(monkeypatch):
    monkeypatch.setattr(triage_chain, "invoke", lambda _v: TriageOpinion(
        tier="critical", reasoning="quantity makes no sense for this department",
        concerns=["40 laptops for a team of 4"],
    ))
    decision = triage(department="marketing", total_paise=60_000 * RUPEE,
                      lines=[], supplier_ids=("meridian-systems",))
    assert decision.tier is Tier.CRITICAL
    assert decision.raised_by_model
    assert decision.policy.human_approval


def test_the_model_cannot_lower_through_the_node(monkeypatch):
    monkeypatch.setattr(triage_chain, "invoke", lambda _v: TriageOpinion(
        tier="routine", reasoning="looks fine to me", concerns=[],
    ))
    decision = triage(department="engineering", total_paise=310_000 * RUPEE,
                      lines=[], supplier_ids=("meridian-systems",))
    assert decision.tier is Tier.CRITICAL


def test_an_unreachable_model_keeps_the_floor(monkeypatch):
    """Unavailability must not reduce scrutiny, and the reason is recorded."""
    def boom(_variables):
        raise TimeoutError("no route to host")

    monkeypatch.setattr(triage_chain, "invoke", boom)
    decision = triage(department="engineering", total_paise=60_000 * RUPEE,
                      lines=[], supplier_ids=("meridian-systems",))
    assert decision.tier is Tier.ELEVATED
    assert "unavailable" in decision.reasoning


def test_an_unknown_supplier_reaches_critical_through_the_node(monkeypatch):
    monkeypatch.setattr(triage_chain, "invoke", lambda _v: TriageOpinion(
        tier="critical", reasoning="supplier is not on the register", concerns=[],
    ))
    decision = triage(department="engineering", total_paise=2_000 * RUPEE,
                      lines=[], supplier_ids=("ghost-mart",))
    assert decision.tier is Tier.CRITICAL


def test_the_decision_is_auditable():
    decision = triage(department="operations", total_paise=1_000 * RUPEE,
                      lines=[], supplier_ids=("northgate-supply",), ask_model=False)
    record = decision.as_dict()
    for key in ("tier", "tier_by_policy", "raised_by_model", "shoppers",
                "sanctions", "human_approval", "reasoning"):
        assert key in record
