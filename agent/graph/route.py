"""Routing: what a refusal means, decided in code rather than by a prompt.

RabbitHole's route_after_hitl reads state and returns one of three destinations.
The equivalent question here is what to do about a refusal, and the answer is not
uniform - which was the point of the critique that a loop treating every failure
identically is repeating, not routing.

Keeping this out of the prompt has two benefits: it is testable without a model,
and a scope refusal can never be retried because a model felt optimistic.
"""

from __future__ import annotations

from .state import ErrandAction, RefusalState

# A refusal naming any of these is about money. Money frees up only by spending
# less, so a smaller order is a real strategy.
BUDGET_MARKERS = ("budget", "insufficient", "exhausted")

# A refusal naming any of these is about authority. No order is small enough to
# fix a capability the token never carried, so retrying is pure waste.
SCOPE_MARKERS = ("scope", "not permitted", "payout", "forged", "denied by policy")

# The payment is suspended pending a human decision, not refused.
PENDING_MARKERS = ("escalated", "pending", "approval")


def classify(refusal: RefusalState | None) -> str:
    """One of: none, budget, scope, pending, unknown."""
    if not refusal:
        return "none"
    text = str(refusal.get("reason", "")).lower()
    if any(m in text for m in PENDING_MARKERS):
        return "pending"
    if any(m in text for m in SCOPE_MARKERS):
        return "scope"
    if any(m in text for m in BUDGET_MARKERS):
        return "budget"
    return "unknown"


def cheapest_line_paise() -> int:
    """The least anything in the catalogue costs.

    Used as the floor below which re-planning is pointless. Derived rather than
    hardcoded: a fixed figure went stale the moment the catalogue changed from
    groceries to procurement, and a loop that spends three rounds proving it
    cannot afford anything is worse than one that stops.
    """
    from merchant.catalog import CATALOG

    return min(p.price_paise for p in CATALOG)


def route_after_refusal(
    refusal: RefusalState | None,
    *,
    remaining_paise: int | None,
    iteration: int,
    max_rounds: int,
    cheapest_useful_paise: int | None = None,
) -> ErrandAction:
    """Decide what happens after a purchase attempt."""
    if cheapest_useful_paise is None:
        cheapest_useful_paise = cheapest_line_paise()
    kind = classify(refusal)

    if kind == "none":
        return "done"
    if kind == "pending":
        return "waiting"
    if kind == "scope":
        # Unrecoverable by construction: attenuation only narrows, so authority
        # the chain never granted cannot appear later.
        return "give_up"
    if iteration >= max_rounds:
        return "give_up"
    if remaining_paise is not None and remaining_paise < cheapest_useful_paise:
        return "give_up"
    return "continue"
