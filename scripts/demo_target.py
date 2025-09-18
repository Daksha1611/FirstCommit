"""The target branch: a goal, not a list.

An errand names what to buy. A target names an outcome and something has to work
out what that means - which SKUs, how many, and what to give up when the money
does not stretch.

Two model calls: one to classify the request, one to plan it. The budget is then
enforced in code, because a model asked to stay under a number will say it did.

Run:  .venv/bin/python scripts/demo_target.py
"""

from agent.graph import NotSupported, interpret
from agent.nodes import triage
from merchant import offers
from pocketchange import config

RUPEE = 100
BAR = "-" * 72

REQUESTS = [
    ("kit out two new engineers, budget 5 lakh", "engineering"),
    ("kit out two new engineers, budget 2.5 lakh", "engineering"),
    ("buy 4 standard laptops", "operations"),
]


def heading(text):
    print(f"\n{text}\n{BAR}")


def show(request: str, department: str) -> None:
    heading(f'"{request}"')
    try:
        intent = interpret(request, department=department)
    except NotSupported as exc:
        print(f"  refused: {exc}")
        return

    print(f"  classified        {intent.kind}")
    if intent.goal:
        print(f"  goal              {intent.goal[:64]}")
    if intent.budget_paise:
        print(f"  budget            Rs {intent.budget_paise / RUPEE:,.0f}")

    if not intent.planned:
        print(f"  asked for         {list(intent.requested_items)}")
        print("  (an errand names its own lines; the shoppers resolve the words")
        print("   to catalogue SKUs, so the planner is skipped entirely)")
        return

    req = intent.requisition
    print(f"\n  planned           Rs {req['total_paise'] / RUPEE:,.0f}"
          f"   affordable: {req['affordable']}")
    if req["problem"]:
        print(f"  PROBLEM           {req['problem']}")
    print(f"  reasoning         {req['reasoning'][:62]}")

    if req["assumptions"]:
        print("\n  it assumed:")
        for line in req["assumptions"]:
            print(f"    - {line[:66]}")

    print("\n  requisition:")
    for line in req["lines"]:
        print(f"    {line['priority']:<10} {line['sku']:<12} x{line['quantity']:<3} "
              f"{line['rationale'][:38]}")

    if req["dropped"]:
        print("\n  dropped to fit the budget:")
        for line in req["dropped"]:
            print(f"    {line['priority']:<10} {line['sku']:<12} x{line['quantity']}")

    # What this requisition would cost to process, once it reaches the buyer.
    lines, suppliers = [], []
    for sku, qty in intent.cart.items():
        available = offers.available_offers(sku, qty)
        if available:
            lines.append({"sku": sku, "quantity": qty, "seller_id": available[0].seller_id})
            suppliers.append(available[0].seller_id)
    if lines:
        decision = triage(
            department=department, total_paise=req["total_paise"], lines=lines,
            supplier_ids=tuple(dict.fromkeys(suppliers)), ask_model=False,
        ).as_dict()
        print(f"\n  triage            {decision['tier']}  "
              f"shoppers={decision['shoppers']}  "
              f"sanctions={decision['sanctions']}  "
              f"human={decision['human_approval']}")


def main() -> int:
    config.load()
    for request, department in REQUESTS:
        show(request, department)
    print(f"\n{BAR}")
    print("  The model decides what the goal implies and ranks each line.")
    print("  The code decides what survives the budget.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
