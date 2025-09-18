"""The front door: a request in plain language becomes work.

Until now `build_buyer` took a raw string and `intake_node` sat unused beside it,
classifying correctly and being called by nothing. This is where that stops.

    request
       |
    intake       errand | target | standing
       |
       +-- errand    the lines are named; go straight to the buyer
       +-- target    a goal; plan it into a requisition first
       +-- standing  recurring; not built, and says so rather than
                     guessing at something that spends money

The branch matters more than it looks. An errand and a target ask different
things of the system - one is "buy these", the other is "work out what these
should be" - and answering the second as though it were the first is how an
agent buys the wrong thing confidently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.nodes import establish, intake_node, plan_requisition, tick


class NotSupported(Exception):
    """A branch that exists in the classification and not yet in the code."""


@dataclass(frozen=True)
class Intent:
    """What the request turned out to be, and the work it produced."""

    kind: str
    goal: str
    department: str
    budget_paise: int | None
    cart: dict[str, int]
    # An errand names its lines in human words - "standard laptops", "a pack".
    # Those are not SKUs, and pretending they are would put a phrase where the
    # rest of the system expects a catalogue key. The shoppers resolve them.
    requested_items: tuple[str, ...]
    requisition: dict[str, Any] | None
    intake: dict[str, Any]
    # Only set for a standing request: the persisted policy, not a purchase.
    standing_order: Any = None

    @property
    def planned(self) -> bool:
        return self.requisition is not None

    def summary(self) -> dict[str, Any]:
        out = {
            "kind": self.kind,
            "goal": self.goal,
            "department": self.department,
            "budget_paise": self.budget_paise,
            "cart": self.cart,
            "requested_items": list(self.requested_items),
        }
        if self.requisition:
            out["assumptions"] = self.requisition["assumptions"]
            out["problem"] = self.requisition["problem"]
        if self.standing_order is not None:
            out["standing_order"] = self.standing_order.as_dict()
            out["dropped"] = [
                f"{l['sku']} ({l['priority']})" for l in self.requisition["dropped"]
            ]
            out["affordable"] = self.requisition["affordable"]
        return out


def _requested(items: list[dict]) -> tuple[str, ...]:
    """What an errand asked for, in the words it used.

    Kept as text on purpose. Intake records "standard laptops", not LAP-STD-1 -
    resolving a phrase to a catalogue key is the shoppers' job, and guessing it
    here would put a human phrase where a SKU belongs.
    """
    out = []
    for item in items:
        quantity = str(item.get("quantity") or "").strip()
        out.append(f"{quantity} {item['name']}".strip() if quantity else item["name"])
    return tuple(out)


def interpret(
    request: str,
    *,
    department: str = "unassigned",
    budget_paise: int | None = None,
    period: str = "month",
) -> Intent:
    """Classify a request, then do what that kind of request needs.

      errand    the lines are named; hand them to the buyer as words
      target    a goal; plan it into a requisition that fits the budget
      standing  recurring; write down a reorder policy to run later

    A standing request produces no purchase. It produces a policy, which is the
    distinction that makes it safe to set up and forget.
    """
    classified = intake_node({"request": request})
    kind = classified.get("task_kind", "errand")
    commands = classified.get("commands") or {}
    budget = commands.get("budget_paise") or budget_paise

    if kind == "standing":
        if not budget:
            raise NotSupported(
                "a standing instruction needs a budget per period: without one "
                "it is an open-ended claim on the company's money"
            )
        order = establish(
            instruction=request, department=department,
            period=period, budget_paise=budget,
        )
        return Intent(
            kind=kind, goal=classified.get("goal", ""), department=department,
            budget_paise=budget, cart={}, requested_items=(),
            requisition=None, intake=classified, standing_order=order,
        )

    if kind == "target":
        if not budget:
            # Planning against an unknown ceiling would be inventing permission
            # to spend. An errand can proceed without one because its lines are
            # already named; a target cannot.
            raise NotSupported(
                "a target needs a budget: nobody said what this may cost"
            )
        requisition = plan_requisition(
            request=request,
            goal=classified.get("goal") or request,
            budget_paise=budget,
            department=department,
        )
        return Intent(
            kind=kind, goal=classified.get("goal", ""), department=department,
            budget_paise=budget, cart=requisition.to_cart(),
            requested_items=(), requisition=requisition.as_dict(), intake=classified,
        )

    return Intent(
        kind=kind, goal=classified.get("goal", ""), department=department,
        budget_paise=budget, cart={},
        requested_items=_requested(classified.get("items", [])),
        requisition=None, intake=classified,
    )


def register_standing(
    intent: Intent, bank: Any | None = None
) -> tuple[Any, Any]:
    """Persist a standing policy and check it once. Returns (order, tick result).

    Setting one up and immediately checking it is deliberate: a person who says
    "keep the cupboard stocked" wants to know whether that means buying
    something right now, and finding out a month later is not an answer.
    """
    from pocketchange import memory

    if intent.kind != "standing" or intent.standing_order is None:
        raise NotSupported("this request did not produce a standing policy")

    # `bank or ...` would be wrong: InMemoryBank defines __len__, so an empty
    # one is falsy and the caller's store would be silently discarded for a
    # fresh one. Test the sentinel, not the truthiness.
    store = memory.from_env() if bank is None else bank
    order = store.remember(intent.standing_order)
    return order, tick(order)
