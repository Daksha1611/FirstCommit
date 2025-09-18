"""The standing branch: an instruction that keeps running.

A person says something once. The system writes down what it means, then acts on
that policy for months without asking again.

The interesting behaviour is restraint. A standing order that finds something to
buy every time it runs is not a policy, it is a leak - so most ticks must buy
nothing, and this demo shows that as clearly as it shows a tick that acts.

Two model calls: classify the instruction, then turn it into reorder points.
Every tick after that is arithmetic.

Run:  .venv/bin/python scripts/demo_standing.py
"""

from agent.graph import interpret, register_standing
from agent.nodes import tick
from merchant import stock
from pocketchange import config
from pocketchange.memory import InMemoryBank

RUPEE = 100
BAR = "-" * 72

INSTRUCTION = "keep the stationery cupboard stocked, up to 50,000 rupees a month"


def heading(text):
    print(f"\n{text}\n{BAR}")


def show_tick(label, result):
    print(f"\n  {label}")
    print(f"    {result.reason}")
    for line in result.due:
        print(f"      {line['sku']:<12} on hand {line['on_hand']:>2} "
              f"<= reorder {line['reorder_point']:<2}  buy {line['quantity']:>2}  "
              f"Rs {line['cost_paise'] / RUPEE:>8,.0f}")
    print(f"    acts: {result.acts}    "
          f"would spend Rs {result.total_paise / RUPEE:,.0f}    "
          f"remaining Rs {result.remaining_paise / RUPEE:,.0f}")


def main() -> int:
    config.load()
    bank = InMemoryBank()

    heading("THE INSTRUCTION")
    print(f'  "{INSTRUCTION}"')

    intent = interpret(INSTRUCTION, department="operations")
    print(f"\n  classified        {intent.kind}")
    print(f"  produces          a policy, not a purchase  (cart: {intent.cart})")

    order, first = register_standing(intent, bank)

    heading("THE POLICY IT WROTE DOWN")
    print(f"  budget            Rs {order.period_budget_paise / RUPEE:,.0f} per {order.period}")
    for rule in order.rules:
        print(f"    {rule.sku:<12} reorder at {rule.reorder_point:>2}, top up to {rule.target_level:>2}")

    heading("TICKS")
    show_tick("now, with the cupboard low:", first)

    if first.acts:
        bank.record_check(order.id, spent_paise=first.total_paise,
                          note="topped up paper and toner")
        order = bank.recall(order.id)

    topped_up = tuple(
        stock.StockLevel(l.sku, l.target_level, l.reorder_point, l.target_level)
        for l in stock.levels()
    )
    show_tick("next week, after that delivery arrived:", tick(order, topped_up))

    heading("WHAT SURVIVED")
    saved = bank.recall(order.id)
    print(f"  checks run        {saved.checks}")
    print(f"  spent this month  Rs {saved.spent_this_period_paise / RUPEE:,.0f}")
    print(f"  remaining         Rs {saved.remaining_paise / RUPEE:,.0f}")
    print(f"  last note         {saved.notes[-1] if saved.notes else '-'}")

    print(f"\n{BAR}")
    print("  The instruction was read once. Every tick after that is arithmetic")
    print("  against the policy, and most of them buy nothing.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
