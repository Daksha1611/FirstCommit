"""Scrutiny tiers: how much effort a requisition earns.

Lives outside agent/graph/ on purpose. Importing it from a node would otherwise
pull in agent.graph.__init__, which pulls the builder, which pulls agent.nodes -
a cycle. These are policy rules rather than graph wiring, so this is also where
they belong conceptually.

Real procurement runs delegation-of-authority thresholds: a box of paper and a
rack server do not collect the same signatures. Two reasons ours does the same -
scrutiny should follow risk, and effort spent on a 1,450 rupee order is effort
not spent on a 310,000 rupee one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

RUPEE = 100  # paise


class Tier(str, Enum):
    """How much scrutiny a requisition earns.

    Real procurement runs delegation-of-authority thresholds: a box of paper and
    a rack server do not get the same signatures. Ours does the same thing, and
    for the same two reasons - scrutiny should follow risk, and effort spent on a
    1,450 rupee order is effort not spent on a 310,000 rupee one.
    """

    ROUTINE = "routine"
    STANDARD = "standard"
    ELEVATED = "elevated"
    CRITICAL = "critical"


ORDER = (Tier.ROUTINE, Tier.STANDARD, Tier.ELEVATED, Tier.CRITICAL)


@dataclass(frozen=True)
class TierPolicy:
    """What a tier actually costs to process."""

    shoppers: int
    sanctions: bool
    benchmark: bool
    human_approval: bool
    sanctions_fails_closed: bool


POLICY: dict[Tier, TierPolicy] = {
    # A box of paper from a supplier we have used forty times does not need
    # three opinions and two API calls.
    Tier.ROUTINE: TierPolicy(1, sanctions=False, benchmark=False,
                             human_approval=False, sanctions_fails_closed=False),
    Tier.STANDARD: TierPolicy(1, sanctions=True, benchmark=False,
                              human_approval=False, sanctions_fails_closed=False),
    Tier.ELEVATED: TierPolicy(3, sanctions=True, benchmark=True,
                              human_approval=False, sanctions_fails_closed=False),
    # At this size a watchlist check that silently passes because the service is
    # down is worse than no check at all.
    Tier.CRITICAL: TierPolicy(3, sanctions=True, benchmark=True,
                              human_approval=True, sanctions_fails_closed=True),
}

# Thresholds on the total, in paise.
ROUTINE_CEILING = 5_000 * RUPEE
STANDARD_CEILING = 50_000 * RUPEE
ELEVATED_CEILING = 200_000 * RUPEE


def tier_from_facts(
    *,
    amount_paise: int,
    all_suppliers_known: bool = True,
    all_suppliers_established: bool = True,
) -> Tier:
    """The tier the observable facts demand. No model involved.

    A supplier nobody has heard of is critical whatever the amount - the risk is
    the counterparty, not the sum. A known but new supplier lifts the floor to
    elevated for the same reason.
    """
    if not all_suppliers_known:
        return Tier.CRITICAL
    if amount_paise >= ELEVATED_CEILING:
        return Tier.CRITICAL
    if not all_suppliers_established:
        return max(Tier.ELEVATED, _tier_by_amount(amount_paise), key=ORDER.index)
    return _tier_by_amount(amount_paise)


def _tier_by_amount(amount_paise: int) -> Tier:
    if amount_paise < ROUTINE_CEILING:
        return Tier.ROUTINE
    if amount_paise < STANDARD_CEILING:
        return Tier.STANDARD
    if amount_paise < ELEVATED_CEILING:
        return Tier.ELEVATED
    return Tier.CRITICAL


def settle_tier(floor: Tier, model_opinion: Tier | str | None) -> Tier:
    """Combine the rules with the model's reading. The model can only RAISE.

    This is the whole discipline. A model that sees something the thresholds
    cannot - a supplier substitution, an unusual category, a requisition that
    smells wrong - should be able to demand more scrutiny. It must never be able
    to argue for less, because that is the direction an attacker wants and the
    direction a helpful model drifts.
    """
    if model_opinion is None:
        return floor
    try:
        proposed = Tier(model_opinion)
    except ValueError:
        return floor
    return max(floor, proposed, key=ORDER.index)


def policy_for(tier: Tier) -> TierPolicy:
    return POLICY[tier]

# A refusal naming any of these is about money. Money frees up only by spending
# less, so a smaller cart is a real strategy.
BUDGET_MARKERS = ("budget", "insufficient", "exhausted")

# A refusal naming any of these is about authority. No cart is small enough to
# fix a capability the token never carried, so retrying is pure waste.
SCOPE_MARKERS = ("scope", "not permitted", "payout", "forged", "denied by policy")

# The payment is suspended pending a human decision, not refused.
PENDING_MARKERS = ("escalated", "pending", "approval")
