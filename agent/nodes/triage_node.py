"""Triage: decide how much scrutiny a requisition earns, before spending any.

Two halves, deliberately unequal.

The **rules** decide the floor from observable facts — the total, whether every
supplier is on the approved register, whether they are established. That runs
with no model, cannot be talked out of a threshold, and is asserted in tests.

The **model** may then raise the tier if it sees something the thresholds cannot:
a category that does not fit the department, an order sized to sit just under a
limit, a run of near-identical requisitions. It cannot lower one. That asymmetry
is the point — lowering is the direction an attacker wants, and also the
direction a helpful model naturally drifts.

Running this first is what makes the rest affordable: a 1,450 rupee order gets
one shopper and no external calls, so the model quota goes to the requisitions
that warrant it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from agent.tiers import Tier, TierPolicy, policy_for, settle_tier, tier_from_facts
from agent.models import StructuredChain
from agent.prompts import TRIAGE_PROMPT
from merchant import sellers

COMPANY = "Meridian Industries"


class TriageOpinion(BaseModel):
    tier: Literal["routine", "standard", "elevated", "critical"] = Field(
        description="The assigned tier, or a higher one. Never lower."
    )
    reasoning: str = Field(
        description="One short sentence. If raising the tier, name what worried you."
    )
    concerns: list[str] = Field(
        default_factory=list,
        description="Specific things noticed, empty if nothing stood out.",
    )


triage_chain = StructuredChain(system=TRIAGE_PROMPT, schema=TriageOpinion)


@dataclass(frozen=True)
class Triage:
    """The decision, and enough of the reasoning to audit it."""

    tier: Tier
    floor: Tier
    policy: TierPolicy
    reasoning: str
    concerns: tuple[str, ...]
    raised_by_model: bool

    def as_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "tier_by_policy": self.floor.value,
            "raised_by_model": self.raised_by_model,
            "shoppers": self.policy.shoppers,
            "sanctions": self.policy.sanctions,
            "benchmark": self.policy.benchmark,
            "human_approval": self.policy.human_approval,
            "reasoning": self.reasoning,
            "concerns": list(self.concerns),
        }


def supplier_standing(supplier_ids: tuple[str, ...]) -> tuple[bool, bool]:
    """(every supplier is on the register, every one is established)."""
    if not supplier_ids:
        return True, True
    known = all(sellers.known(s) for s in supplier_ids)
    established = known and all(sellers.get(s).is_established for s in supplier_ids)
    return known, established


def triage(
    *,
    department: str,
    total_paise: int,
    lines: list[dict],
    supplier_ids: tuple[str, ...],
    ask_model: bool = True,
) -> Triage:
    """Rules first, then the model, and only ever upward."""
    known, established = supplier_standing(supplier_ids)
    floor = tier_from_facts(
        amount_paise=total_paise,
        all_suppliers_known=known,
        all_suppliers_established=established,
    )

    # The model is skipped entirely at routine. Asking a model whether a 1,450
    # rupee box of paper is suspicious costs more than the paper.
    if not ask_model or floor is Tier.ROUTINE:
        return Triage(
            tier=floor, floor=floor, policy=policy_for(floor),
            reasoning="set by policy from amount and supplier standing",
            concerns=(), raised_by_model=False,
        )

    try:
        opinion = triage_chain.invoke({
            "company": COMPANY,
            "department": department,
            "total_paise": total_paise,
            "lines": lines,
            "suppliers": list(supplier_ids),
            "assigned_tier": floor.value,
        })
        tier = settle_tier(floor, opinion.tier)
        reasoning, concerns = opinion.reasoning, tuple(opinion.concerns)
    except Exception as exc:  # noqa: BLE001
        # An unreachable model must not lower scrutiny. Keeping the floor is the
        # safe direction, and the reason is recorded rather than swallowed.
        tier, reasoning, concerns = floor, f"triage model unavailable: {type(exc).__name__}", ()

    return Triage(
        tier=tier, floor=floor, policy=policy_for(tier),
        reasoning=reasoning, concerns=concerns,
        raised_by_model=tier is not floor,
    )


def triage_node(state: dict) -> dict:
    """Graph node. Runs once; a cycle must not re-triage."""
    if state.get("triage"):
        return {}
    lines = state.get("cart_lines") or []
    suppliers = tuple(dict.fromkeys(line["seller_id"] for line in lines if "seller_id" in line))
    total = sum(line.get("line_total_paise", 0) for line in lines)
    decision = triage(
        department=state.get("department", "unassigned"),
        total_paise=total, lines=lines, supplier_ids=suppliers,
    )
    return {"triage": decision.as_dict(), "tier": decision.tier.value}
