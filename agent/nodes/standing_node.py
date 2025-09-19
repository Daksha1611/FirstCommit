"""The standing branch: an instruction that keeps running.

The third intake branch, and the one that needs memory. An errand is named, a
target is planned, a standing order *recurs* - so it only means anything if the
system remembers what it bought last time and how much of the period budget is
left.

Split of work, and it is the same one the planner uses:

  the model   once, at setup: turn a sentence into reorder points
  the code    every tick: compare stock to those points and do arithmetic

Re-interpreting the sentence on every tick would be wasteful and, worse, a way
for behaviour to drift for months without anyone changing anything. A policy
written down once can be read by a person; a policy re-derived each week cannot.

A tick is deliberately not a scheduler. Something outside has to call it - cron,
a Cloud Scheduler job, a human. This module decides *what* to buy when asked,
not *when* to ask.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from pydantic import BaseModel, Field

from agent.models import StructuredChain
from agent.utils.catalogue import catalogue_prompt_block
from agent.prompts import STANDING_PROMPT
from merchant import catalog, stock
from agent.nodes.triage_node import triage
from pocketchange.memory import ReorderRule, StandingOrder, new_order

COMPANY = "Meridian Industries"

# --- bounds on what a standing policy may say ------------------------------
#
# These are enforced in code, after the model answers and before anything is
# persisted. Not because the model is untrustworthy in general, but because of
# what persistence does to a mistake here.
#
# An injection against an errand buys one wrong thing once. An injection that
# lands while a policy is being written sets reorder points that persist for
# months, and every tick afterwards executes them as pure arithmetic - no model
# involved, so nothing ever re-reads the poisoned text and nothing gets a second
# chance to notice. One attack, recurring payoff.
#
# So the model proposes levels and code decides whether they are sane, exactly
# as the planner proposes lines and fit_to_budget decides what survives.

# A single top-up of more than this is a stockpile, not replenishment.
MAX_TOPUP = 24

# A single line's full top-up may not exceed one period's budget. Deliberately
# loose: per-tick affordability is already enforced at tick time against
# remaining_paise, and duplicating it here rejected legitimate policies - a
# toner line at 32,400p against a 50,000p month is aggressive but not absurd.
#
# These bounds exist to catch the absurd (400 reams, a recurring server order,
# a target that loops forever), not to re-do arithmetic that happens later.
MAX_LINE_SHARE = 1.0

# What a standing instruction may cover at all. Consumables run out; servers and
# furniture do not, and a recurring order for them would be alarming however
# reasonable its numbers looked.
STANDING_CATEGORIES = frozenset({"consumables", "network"})


@dataclass(frozen=True)
class Rejection:
    sku: str
    reason: str

    def as_dict(self) -> dict:
        return {"sku": self.sku, "reason": self.reason}


def bound_rules(
    lines: list["PolicyLine"], budget_paise: int
) -> tuple[tuple[ReorderRule, ...], tuple[Rejection, ...]]:
    """Keep only rules that are sane to run unattended for months.

    Rejections carry reasons rather than being silently dropped - a policy that
    quietly covers less than the person asked for is its own kind of wrong.
    """
    kept: list[ReorderRule] = []
    rejected: list[Rejection] = []
    line_ceiling = int(budget_paise * MAX_LINE_SHARE)

    for line in lines:
        if line.sku not in catalog.BY_SKU:
            rejected.append(Rejection(line.sku, "not in the catalogue"))
            continue

        product = catalog.get(line.sku)
        if product.category not in STANDING_CATEGORIES:
            rejected.append(Rejection(
                line.sku,
                f"{product.category} is not something a standing order may cover",
            ))
            continue

        if line.target_level <= line.reorder_point:
            rejected.append(Rejection(
                line.sku,
                f"target {line.target_level} is not above reorder point "
                f"{line.reorder_point}, so it would reorder forever",
            ))
            continue

        topup = line.target_level - line.reorder_point
        if topup > MAX_TOPUP:
            rejected.append(Rejection(
                line.sku, f"tops up by {topup} at once, limit is {MAX_TOPUP}"
            ))
            continue

        worst_case = product.price_paise * line.target_level
        if worst_case > line_ceiling:
            rejected.append(Rejection(
                line.sku,
                f"a full top-up would cost {worst_case}p, more than the whole "
                f"{budget_paise}p period budget",
            ))
            continue

        kept.append(ReorderRule(sku=line.sku, reorder_point=line.reorder_point,
                                target_level=line.target_level))

    return tuple(kept), tuple(rejected)


class PolicyLine(BaseModel):
    sku: str = Field(description="A sku from the catalogue. Never invented.")
    reorder_point: int = Field(ge=0, description="Top up at or below this level.")
    target_level: int = Field(ge=1, description="Top up to this level.")
    rationale: str = Field(description="One clause on why these numbers.")


class StandingPolicy(BaseModel):
    lines: list[PolicyLine] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    reasoning: str = ""


standing_chain = StructuredChain(system=STANDING_PROMPT, schema=StandingPolicy)


@dataclass(frozen=True)
class TickResult:
    """What one check of a standing order concluded."""

    order_id: str
    due: tuple[dict, ...]
    total_paise: int
    remaining_paise: int
    affordable: bool
    reason: str

    @property
    def acts(self) -> bool:
        return bool(self.due) and self.affordable

    def to_cart(self) -> dict[str, int]:
        return {line["sku"]: line["quantity"] for line in self.due}

    def as_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "due": list(self.due),
            "total_paise": self.total_paise,
            "remaining_paise": self.remaining_paise,
            "affordable": self.affordable,
            "acts": self.acts,
            "reason": self.reason,
        }


def establish(
    *, instruction: str, department: str, period: str, budget_paise: int
) -> StandingOrder:
    """Read the instruction once and write down the policy it implies."""
    policy = standing_chain.invoke({
        "company": COMPANY,
        "instruction": instruction,
        "department": department,
        "period": period,
        "budget_paise": budget_paise,
        "catalogue": catalogue_prompt_block(),
    })
    rules, rejected = bound_rules(policy.lines, budget_paise)
    if not rules:
        detail = "; ".join(f"{r.sku}: {r.reason}" for r in rejected) or "none proposed"
        raise ValueError(f"no usable reorder rules — {detail}")

    order = new_order(
        instruction=instruction, department=department, period=period,
        period_budget_paise=budget_paise, rules=rules,
    )
    if rejected:
        # Recorded on the order, so a person approving it can see what the
        # policy does NOT cover as well as what it does.
        order = replace(order, notes=tuple(
            f"rejected {r.sku}: {r.reason}" for r in rejected
        ))
    return order


def tick(order: StandingOrder, levels: tuple[stock.StockLevel, ...] | None = None) -> TickResult:
    """Decide what to buy right now. Pure arithmetic, no model.

    Buys nothing when nothing is below its reorder point, which is the normal
    outcome and the one an unattended agent must get right. A standing order
    that finds something to buy every time it runs is not a policy, it is a
    leak.
    """
    if not order.approved:
        return TickResult(order.id, (), 0, order.remaining_paise, False,
                          "this policy has not been approved by anyone yet")

    on_hand = {level.sku: level for level in (levels or stock.levels())}
    due: list[dict] = []
    total = 0

    for rule in order.rules:
        level = on_hand.get(rule.sku)
        if level is None or level.on_hand > rule.reorder_point:
            continue
        quantity = rule.target_level - level.on_hand
        if quantity <= 0:
            continue
        cost = catalog.get(rule.sku).price_paise * quantity
        due.append({"sku": rule.sku, "quantity": quantity, "cost_paise": cost,
                    "on_hand": level.on_hand, "reorder_point": rule.reorder_point})
        total += cost

    if not due:
        return TickResult(order.id, (), 0, order.remaining_paise, True,
                          "nothing is below its reorder point")

    if total > order.remaining_paise:
        # Partial spending is not obviously right and is not the agent's call:
        # a person set a period budget, and quietly buying some of the list
        # would be a decision nobody authorised.
        return TickResult(
            order.id, tuple(due), total, order.remaining_paise, False,
            f"needs {total}p, only {order.remaining_paise}p left this {order.period}",
        )

    # Even an approved policy is triaged on what it is about to buy. A tick that
    # has grown large - prices moved, several lines came due together - should
    # meet the same threshold a person would.
    decision = triage(
        department=order.department, total_paise=total,
        lines=[{"sku": line["sku"], "quantity": line["quantity"]} for line in due],
        supplier_ids=(), ask_model=False,
    )
    if decision.policy.human_approval:
        return TickResult(
            order.id, tuple(due), total, order.remaining_paise, False,
            f"{decision.tier.value} tier: a tick this size needs a person",
        )

    return TickResult(order.id, tuple(due), total, order.remaining_paise, True,
                      f"{len(due)} line(s) below reorder point")
