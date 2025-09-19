"""The funnel, driven to a hundred and twenty-one nodes, offline.

No model, no credentials, no quota. That is the claim being demonstrated: the
recursion, the attenuation and every bound are arithmetic, so the expensive part
of the system - asking a model what the sub-tasks are - is the only part that
costs anything, and it is one call per branch node rather than one per node.

Then the same machinery is pushed until each of the four fault bounds fires, to
show that it refuses rather than quietly returning a smaller tree.

Run:  python -m eval.funnel
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pocketchange import events, funnel, identity, token as tokens
from pocketchange.policy import RUPEE, Operation

BUDGET = 600_000 * RUPEE          # ₹6,00,000 to equip an office
FLOOR = 10_000 * RUPEE            # below ₹10,000 a task is done being split


def root_token(budget: int, *, max_depth: int = 8):
    return tokens.mint(
        identity.ephemeral(),
        budget_paise=budget,
        max_depth=max_depth,
        expires=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def rupees(paise: int) -> str:
    return f"₹{paise // 100:,}"


# --- a decomposer that costs nothing ---------------------------------------
#
# Stands in for agent/nodes/decompose_node.py, which asks a model the same
# question. Deterministic so the numbers below are reproducible.

DEPARTMENTS = ["engineering", "design", "operations"]
CATEGORIES = ["workstations", "peripherals", "furniture"]
ITEMS = ["primary units", "spares", "accessories"]


def scripted(node) -> list[funnel.SubTask]:
    share = node.budget_paise // 3
    if node.depth == 0:
        return [funnel.SubTask(f"kit out {d}", share) for d in DEPARTMENTS]
    if node.depth == 1:
        return [funnel.SubTask(f"{node.description}: {c}", share) for c in CATEGORIES]
    if node.depth == 2:
        # One branch per layer sources from the open web rather than the
        # catalogue. Those children also hold `search`.
        return [
            funnel.SubTask(f"{node.description} - {i}", share,
                           sourcing=funnel.BEST if i == ITEMS[0] else funnel.CATALOGUE)
            for i in ITEMS
        ]
    return [funnel.SubTask(f"{node.description} lot {i}", share) for i in (1, 2, 3)]


def buy(node):
    return {"order": f"order_{node.id}", "amount_paise": node.budget_paise}


def banner(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m\n" + "─" * 72)


def main() -> int:
    bus = events.EventBus(history=10_000)
    f = funnel.Funnel(
        root_token(BUDGET), description="equip the new Bengaluru office",
        budget_paise=BUDGET, bounds=funnel.Bounds(
            max_depth=8, max_fanout=6, max_nodes=200, decompose_floor_paise=FLOOR),
        bus=bus,
    )

    banner("THE FUNNEL")
    print(f"one task, {rupees(BUDGET)}, decomposed until each piece is under "
          f"{rupees(FLOOR)}\n")

    result = f.run(scripted, buy)
    s = result.summary()

    # The tree, three layers deep, then a count.
    for node in result.root.walk():
        if node.depth > 3:
            continue
        tag = {funnel.BEST: " ⌕ web", funnel.SPECIFIC: " → named"}.get(node.sourcing, "")
        print(f"{'  ' * node.depth}{node.id:<16} {rupees(node.budget_paise):>12}  "
              f"{node.description}{tag}")
    print(f"{'  ' * 4}… {len([n for n in result.root.walk() if n.depth == 4])} "
          f"nodes at depth 4, not printed")

    banner("WHAT IT COST")
    branches = len([n for n in result.root.walk() if n.children])
    print(f"  nodes                 {s['nodes']}")
    print(f"  depth reached         {s['max_depth_reached']} of {result.bounds.max_depth}")
    print(f"  leaves executed       {s['executed']}")
    print(f"  model calls needed    {branches}   (one per branch node, not per node)")
    print(f"  free nodes            {s['nodes'] - branches}")
    print(f"  events emitted        {len(bus.history())}")
    print(f"  committed             {rupees(s['committed_paise'])} of {rupees(BUDGET)}")

    banner("WHAT THE CHAIN GUARANTEES")
    mandates = {tokens.mandate_id(n.token) for n in result.root.walk()}
    print(f"  one mandate id across all {s['nodes']} nodes        "
          f"{'yes' if len(mandates) == 1 else 'NO'}   "
          "→ one budget pool, however wide the tree")

    depths_ok = all(tokens.depth_of(n.token) == n.depth for n in result.root.walk())
    print(f"  token depth derived, never claimed          "
          f"{'yes' if depths_ok else 'NO'}")

    leaf = result.leaves[0]
    try:
        tokens.verify(leaf.token, Operation("delegate", 0, depth=leaf.depth))
        recruits = "NO - a leaf can delegate"
    except tokens.Denied:
        recruits = "yes"
    print(f"  a leaf cannot recruit accomplices           {recruits}")

    over = list(bus.history())[-1]
    biggest = max(len(tokens.serialize(n.token)) for n in result.root.walk())
    print(f"  deepest token size                          {biggest:,} chars "
          "(header limit is ~8,192)")
    assert over is not None

    banner("WHERE IT REFUSES")
    print("  a bound that is hit must be heard. Each of these returns an "
          "incomplete\n  tree AND says so, rather than a smaller tree that "
          "looks finished.\n")

    def probe(label, bounds, decompose):
        g = funnel.Funnel(root_token(BUDGET), description="equip the office",
                          budget_paise=BUDGET, bounds=bounds,
                          bus=events.EventBus(history=10_000))
        r = g.run(decompose, buy)
        reason = r.refusals[0][1] if r.refusals else "—"
        print(f"  {label:<22} nodes {r.nodes:>4}   complete "
              f"{str(r.complete):<5}  refused: {reason}")

    probe("fan-out of 8 (max 6)", funnel.Bounds(decompose_floor_paise=FLOOR),
          lambda n: [funnel.SubTask(f"{n.description} p{i}", n.budget_paise // 8)
                     for i in range(8)])
    probe("node budget of 20", funnel.Bounds(max_nodes=20, decompose_floor_paise=FLOOR),
          scripted)
    probe("a task restating itself", funnel.Bounds(decompose_floor_paise=FLOOR),
          lambda n: [funnel.SubTask(n.description, n.budget_paise // 2)])
    probe("allocating twice its own", funnel.Bounds(decompose_floor_paise=FLOOR),
          lambda n: [funnel.SubTask(f"{n.description} greedy", n.budget_paise * 2)])

    banner("HOW IT ENDS WHEN NOTHING IS WRONG")
    print("  depth cap 8, floor ₹10,000, budget divided by three each layer:\n"
          "  600,000 → 200,000 → 66,666 → 22,222 → 7,407 · under the floor at "
          "depth 4.\n"
          "  The floor stops it, not the cap. Budget strictly decreases, so the\n"
          "  recursion is bounded by log(budget / floor) whatever the cap says.\n")

    ok = (s["nodes"] == 121 and s["max_depth_reached"] == 4
          and result.complete and len(mandates) == 1 and depths_ok)
    print("\033[1m" + ("  PASS" if ok else "  FAIL") + "\033[0m")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
