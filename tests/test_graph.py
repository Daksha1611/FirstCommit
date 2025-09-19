"""The agent graph: routing, reducers, and assembly.

None of these need a model. Routing was moved out of the reviewer's prompt and
into graph/route.py precisely so it could be tested this way - a rule that only
exists inside a prompt cannot be asserted on.
"""

import pytest

from agent.graph import (
    MAX_ROUNDS,
    build_buyer,
    classify,
    merge_by_key,
    route_after_refusal,
)
from agent.tools import ToolSurface
from merchant import offers
from merchant.catalog import RUPEE


def line(sku: str, quantity: int) -> dict:
    """A cart line naming a seller that genuinely carries the product.

    Prices come from the real offer, so a test cannot accidentally assert
    against a price that does not exist in the market.
    """
    available = offers.available_offers(sku, quantity)
    assert available, f"no offer can fulfil {sku} x{quantity}"
    return {"sku": sku, "quantity": quantity, "seller_id": available[0].seller_id}


@pytest.fixture
def tools():
    return ToolSurface(client=None, shopper_token="s", payer_token="p")


# --- routing ---------------------------------------------------------------


def route(refusal, available=None, iteration=1):
    return route_after_refusal(
        refusal, remaining_paise=available, iteration=iteration, max_rounds=MAX_ROUNDS
    )


def test_success_finishes():
    assert route(None) == "done"


def test_budget_refusal_with_room_continues():
    assert route({"reason": "cumulative budget exhausted"}, available=240_400 * RUPEE) == "continue"


def test_scope_refusal_is_never_retried():
    """Attenuation only narrows, so authority absent from the chain cannot appear."""
    assert route({"reason": "scope never granted: no block permits payout"}) == "give_up"


def test_escalation_waits_rather_than_failing():
    assert route({"reason": "monitor escalated: needs a human"}) == "waiting"


def test_budget_below_useful_floor_gives_up():
    """A budget below the cheapest catalogue line is not a smaller order, it is none."""
    from agent.graph.route import cheapest_line_paise

    floor = cheapest_line_paise()
    assert route({"reason": "cumulative budget exhausted"}, available=floor - 1) == "give_up"
    assert route({"reason": "cumulative budget exhausted"}, available=floor * 50) == "continue"


def test_last_round_gives_up():
    assert route({"reason": "budget exhausted"}, available=240_000 * RUPEE,
                 iteration=MAX_ROUNDS) == "give_up"


@pytest.mark.parametrize("reason,kind", [
    ("cumulative budget exhausted", "budget"),
    ("scope never granted", "scope"),
    ("monitor escalated", "pending"),
    ("something we have never seen", "unknown"),
])
def test_classify(reason, kind):
    assert classify({"reason": reason}) == kind


# --- reducers --------------------------------------------------------------


def test_merge_keeps_both_writers():
    """The property that makes the fan-out real rather than decorative."""
    merged = merge_by_key({"a": 1}, {"b": 2})
    assert merged == {"a": 1, "b": 2}


def test_concurrent_proposals_do_not_clobber(tools):
    tools.propose_cart("thrifty", [line("LAP-STD-1", 2)], "cheapest")
    tools.propose_cart("complete", [line("LAP-STD-1", 4), line("MON-27Q-1", 2)], "everything")
    tools.propose_cart("balanced", [line("LAP-STD-1", 3), line("MON-27Q-1", 1)], "sensible")
    assert sorted(tools.proposals) == ["balanced", "complete", "thrifty"]


def test_round_opens_once_for_concurrent_shoppers(tools):
    """Three shoppers call current_constraint(); the round must open once."""
    seen = [tools.current_constraint()["iteration"] for _ in range(3)]
    assert seen == [1, 1, 1]


def test_a_sibling_call_does_not_erase_a_proposal(tools):
    tools.current_constraint()
    tools.propose_cart("thrifty", [line("LAP-STD-1", 1)], "r")
    tools.current_constraint()
    assert "thrifty" in tools.proposals


def test_choosing_adopts_that_cart_and_closes_the_round(tools):
    tools.current_constraint()
    tools.propose_cart("thrifty", [line("LAP-STD-1", 2)], "cheapest")
    tools.propose_cart("complete", [line("LAP-STD-1", 4)], "more")
    tools.choose_cart("complete", "covers the list")
    assert tools.cart == {"LAP-STD-1": 4}
    assert [l.sku for l in tools.cart_lines] == ["LAP-STD-1"]
    assert tools.round_open is False


def test_choosing_none_leaves_an_empty_cart(tools):
    tools.current_constraint()
    tools.propose_cart("thrifty", [line("LAP-STD-1", 2)], "cheapest")
    tools.choose_cart("none", "nothing affordable")
    assert tools.cart == {}
    assert tools.cart_lines == []


def test_next_round_clears_stale_proposals(tools):
    tools.current_constraint()
    tools.propose_cart("thrifty", [line("LAP-STD-1", 2)], "cheapest")
    tools.choose_cart("thrifty", "only option")
    tools.current_constraint()
    assert tools.proposals == {}


# --- assembly --------------------------------------------------------------


def test_graph_shape(tools):
    buyer = build_buyer(tools, model="anthropic.claude-haiku-4-5")
    assert [s.name for s in buyer.stages] == ["market", "chooser", "payer", "reviewer"]
    assert len(buyer.stages[0].agents) == 3
    assert buyer.max_iterations == MAX_ROUNDS


def test_only_the_market_stage_runs_concurrently(tools):
    """The fan-out is the one place order does not matter.

    Everything after it is a decision that depends on the step before: the
    chooser needs the proposals, the payer needs the adopted cart, the reviewer
    needs the outcome. Running any of those in parallel would be a race, not a
    speed-up.
    """
    buyer = build_buyer(tools, model="anthropic.claude-haiku-4-5")
    assert [s.name for s in buyer.stages if s.parallel] == ["market"]


def test_every_agent_draws_on_the_same_model(tools):
    """The inverse of what this test used to assert, and deliberately so.

    On the previous provider quota was metered per model *name*, so three
    shoppers sharing one name queued behind one bucket and the fix was to hand
    each a different name. Bedrock meters per account and region, so spreading
    across models of unequal capability would buy nothing and cost consistency.
    See PORTING.md D3.
    """
    buyer = build_buyer(tools)
    ids = [a.model.config["model_id"] for a in buyer.stages[0].agents]
    assert len(set(ids)) == 1


def test_shoppers_cannot_pay_and_payer_cannot_browse(tools):
    """The trust boundary, asserted rather than assumed."""
    shopper = {f.__name__ for f in tools.shopper_functions()}
    payer = {f.__name__ for f in tools.payer_functions()}
    assert "checkout" not in shopper
    assert "search_products" not in payer
    assert "payout" in shopper  # present so the injection can genuinely be tried


def test_snapshot_matches_state_shape(tools):
    snap = tools.snapshot()
    for key in ("iteration", "proposals", "cart", "history", "finished"):
        assert key in snap
