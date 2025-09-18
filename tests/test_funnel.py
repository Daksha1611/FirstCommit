"""The recursive funnel: it grows, and more importantly it stops.

Everything here runs offline with a scripted decomposer. No model, no quota, no
gateway - which is the point of putting the bounds in code rather than in a
prompt.
"""

import pytest
from datetime import datetime, timedelta, timezone

from pocketchange import events, funnel, identity, token as tokens
from pocketchange.policy import RUPEE, Operation


def root_token(budget_paise, *, max_depth=8):
    return tokens.mint(
        identity.ephemeral(),
        budget_paise=budget_paise,
        max_depth=max_depth,
        expires=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def make(budget=600_000 * RUPEE, *, bounds=None, max_depth=8, **kw):
    bus = events.EventBus(history=10_000)
    f = funnel.Funnel(
        root_token(budget, max_depth=max_depth),
        description="equip the new Bengaluru office",
        budget_paise=budget,
        bounds=bounds or funnel.Bounds(),
        bus=bus,
        **kw,
    )
    return f, bus


def even_split(n):
    """A decomposer that splits every node n ways, evenly. Deterministic."""
    def decompose(node):
        share = node.budget_paise // n
        return [
            funnel.SubTask(f"{node.description} / part {i}", share)
            for i in range(1, n + 1)
        ]
    return decompose


def noop_execute(node):
    return {"paid": node.budget_paise}


# --- it grows ---------------------------------------------------------------


def test_a_hundred_node_tree_with_no_model_calls():
    """Depth 4, fan-out 3. The shape the whole design is for."""
    # ₹6,00,000 split three ways per layer: 2,00,000 / 66,666 / 22,222 / 7,407.
    # A ₹10,000 floor stops the fourth layer, giving 1+3+9+27+81 nodes.
    f, _ = make(600_000 * RUPEE, bounds=funnel.Bounds(
        max_depth=8, max_fanout=6, max_nodes=200,
        decompose_floor_paise=10_000 * RUPEE))
    result = f.run(even_split(3), noop_execute)

    assert result.nodes == 121, result.summary()
    assert result.summary()["max_depth_reached"] == 4
    assert len(result.leaves) == 81
    assert result.complete


def test_every_child_token_is_one_block_deeper_than_its_parent():
    f, _ = make(bounds=funnel.Bounds(max_nodes=200))
    result = f.run(even_split(2), noop_execute)
    by_id = {n.id: n for n in result.root.walk()}
    for node in result.root.walk():
        assert tokens.depth_of(node.token) == node.depth
        if node.parent_id:
            assert tokens.depth_of(node.token) == tokens.depth_of(by_id[node.parent_id].token) + 1


def test_the_whole_tree_shares_one_mandate_id():
    """One budget pool covers 121 nodes exactly as it covers two."""
    f, _ = make(bounds=funnel.Bounds(max_nodes=200))
    result = f.run(even_split(3), noop_execute)
    ids = {tokens.mandate_id(n.token) for n in result.root.walk()}
    assert len(ids) == 1


def test_a_leaf_cannot_delegate():
    """A compromised leaf can misspend its own budget and cannot recruit help."""
    f, _ = make(bounds=funnel.Bounds(max_nodes=200))
    result = f.run(even_split(2), noop_execute)
    leaf = result.leaves[0]
    with pytest.raises(tokens.Denied):
        tokens.verify(leaf.token, Operation("delegate", 0, depth=leaf.depth))


# --- it stops ---------------------------------------------------------------


def test_the_budget_floor_is_what_actually_terminates_it():
    """Not the depth cap. Budget strictly decreases, so the floor is reached first."""
    f, _ = make(600_000 * RUPEE, bounds=funnel.Bounds(
        max_depth=8, max_nodes=500, decompose_floor_paise=10_000 * RUPEE))
    result = f.run(even_split(3), noop_execute)
    assert result.summary()["max_depth_reached"] == 4 < 8
    for leaf in result.leaves:
        assert leaf.budget_paise < 10_000 * RUPEE


def test_no_node_exceeds_the_depth_cap():
    f, _ = make(600_000 * RUPEE, max_depth=3, bounds=funnel.Bounds(
        max_depth=3, max_nodes=500, decompose_floor_paise=1))
    result = f.run(even_split(2), noop_execute)
    assert max(n.depth for n in result.root.walk()) == 3


def test_fan_out_beyond_the_limit_refuses_rather_than_trimming():
    f, _ = make(bounds=funnel.Bounds(max_fanout=3))
    result = f.run(even_split(4), noop_execute)
    assert not result.complete
    assert result.refusals[0][1] == "fan-out"
    assert result.root.state == funnel.REFUSED
    # Nothing was silently kept.
    assert result.root.children == []


def test_the_node_budget_refuses_loudly():
    """The 129th node does not quietly vanish."""
    f, _ = make(600_000 * RUPEE, bounds=funnel.Bounds(
        max_nodes=20, decompose_floor_paise=1 * RUPEE))
    result = f.run(even_split(3), noop_execute)
    assert result.nodes <= 20
    assert not result.complete
    assert any(r == "node budget" for _, r in result.refusals)


def test_a_repeated_sub_task_is_caught_as_a_cycle():
    def loop(node):
        return [funnel.SubTask(node.description, node.budget_paise // 2)]
    f, _ = make()
    result = f.run(loop, noop_execute)
    assert [r for _, r in result.refusals] == ["cycle"]


def test_over_allocation_is_refused_even_though_the_chain_would_catch_it():
    def greedy(node):
        return [funnel.SubTask(f"{node.description} / greedy", node.budget_paise * 2)]
    f, _ = make()
    result = f.run(greedy, noop_execute)
    assert [r for _, r in result.refusals] == ["conservation"]


def test_a_decomposer_that_raises_refuses_that_node_only():
    calls = {"n": 0}

    def flaky(node):
        calls["n"] += 1
        if node.depth == 1 and node.id.endswith(".1"):
            raise RuntimeError("model returned nonsense")
        share = node.budget_paise // 2
        return [funnel.SubTask(f"{node.description} / {i}", share) for i in (1, 2)]

    f, _ = make(bounds=funnel.Bounds(max_nodes=200))
    result = f.run(flaky, noop_execute)
    assert any("model returned nonsense" in r for _, r in result.refusals)
    # Its sibling still ran.
    assert len(result.executed) > 0


def test_spend_never_exceeds_the_root_budget():
    f, _ = make(600_000 * RUPEE, bounds=funnel.Bounds(max_nodes=500))
    result = f.run(even_split(3), noop_execute)
    assert result.committed_paise() <= 600_000 * RUPEE


# --- it narrates ------------------------------------------------------------


def test_every_node_emits_spawned_and_something_terminal():
    f, bus = make(bounds=funnel.Bounds(max_nodes=200))
    result = f.run(even_split(3), noop_execute)

    spawned, terminal = set(), set()
    for e in bus.history():
        if e.kind == events.SPAWNED:
            spawned.add(e.node_id)
        if e.kind in events.TERMINAL:
            terminal.add(e.node_id)

    every = {n.id for n in result.root.walk()}
    assert spawned == every
    assert terminal == every


def test_a_grant_event_records_what_was_actually_handed_over():
    f, bus = make(bounds=funnel.Bounds(max_nodes=200))
    f.run(even_split(2), noop_execute)
    grants = [e for e in bus.history() if e.kind == events.GRANTED]
    assert grants
    for g in grants:
        assert g.detail["token_depth"] == g.depth
        assert "pay" in g.detail["tools"]


# --- sourcing inherits ------------------------------------------------------


def test_sourcing_inherits_unless_a_sub_task_narrows_it():
    """A sub-task of "buy from the best supplier" is sourced that way too."""
    f, _ = make(bounds=funnel.Bounds(max_nodes=200), sourcing=funnel.BEST)
    result = f.run(even_split(2), noop_execute)
    assert all(n.sourcing == funnel.BEST for n in result.root.walk())


def test_a_sub_task_may_narrow_the_sourcing():
    def to_specific(node):
        if node.depth > 0:
            return []
        return [funnel.SubTask("from meridian", node.budget_paise // 2,
                               sourcing=funnel.SPECIFIC, supplier="meridian-systems")]
    f, _ = make(sourcing=funnel.BEST)
    result = f.run(to_specific, noop_execute)
    child = result.root.children[0]
    assert child.sourcing == funnel.SPECIFIC
    assert child.supplier == "meridian-systems"


def test_a_named_supplier_does_not_leak_onto_a_narrowed_sibling():
    """Inheriting a supplier is right; inheriting it onto a sub-task that chose
    a different sourcing is not."""
    def mixed(node):
        if node.depth > 0:
            return []
        half = node.budget_paise // 2
        return [funnel.SubTask("same source", half),
                funnel.SubTask("from stock", half, sourcing=funnel.CATALOGUE)]
    f, _ = make(sourcing=funnel.SPECIFIC, supplier="meridian-systems")
    result = f.run(mixed, noop_execute)
    inherited, narrower = result.root.children
    assert (inherited.sourcing, inherited.supplier) == (funnel.SPECIFIC, "meridian-systems")
    assert (narrower.sourcing, narrower.supplier) == (funnel.CATALOGUE, None)


def test_a_sub_task_may_narrow_its_sourcing_but_never_widen_it():
    """The same rule the tokens follow, on the other axis of authority.

    A token cannot grant more than it holds. A sub-task cannot reach further for
    its goods than the task it came from - otherwise the untrusted decomposer
    can put the tree in front of open-web text nobody authorised.
    """
    def widen(node):
        if node.depth > 0:
            return []
        half = node.budget_paise // 2
        return [funnel.SubTask("shop the web", half, sourcing=funnel.BEST),
                funnel.SubTask("from stock", half, sourcing=funnel.CATALOGUE)]
    f, _ = make(sourcing=funnel.CATALOGUE)
    result = f.run(widen, noop_execute)
    wider, narrower = result.root.children
    assert wider.sourcing == funnel.CATALOGUE      # clamped, not obeyed
    assert narrower.sourcing == funnel.CATALOGUE


def test_a_sub_task_that_says_nothing_inherits_the_sourcing_a_person_chose():
    """The bug that kept the zero-budget looker from ever running.

    "Find the best source" was recorded on the root and every child came back
    declaring catalogue, so no leaf ever searched and the whole looker path -
    the separation this project is built on - was dead under the real
    decomposer.
    """
    def quiet(node):
        if node.depth > 0:
            return []
        half = node.budget_paise // 2
        return [funnel.SubTask("desks", half), funnel.SubTask("chairs", half)]
    f, _ = make(sourcing=funnel.BEST)
    result = f.run(quiet, noop_execute)
    assert [c.sourcing for c in result.root.children] == [funnel.BEST, funnel.BEST]


# --- held is not refused ----------------------------------------------------


def test_an_escalation_is_held_not_refused():
    """The gateway answers 202 and keeps the reservation. Reporting that as a
    refusal would tell the operator the budget is back when it is not."""
    def escalate(node):
        raise funnel.Escalated("monitor escalated: unfamiliar supplier")

    f, bus = make(10_000 * RUPEE,
                  bounds=funnel.Bounds(decompose_floor_paise=50_000 * RUPEE))
    result = f.run(lambda n: [], escalate)

    # The root narrows once before anything pays, so the held payment is its
    # sole payer rather than the mandate itself.
    payer = result.root.children[0]
    assert payer.state == funnel.ESCALATED
    assert result.refusals == []
    assert result.escalations == [("root.1", "monitor escalated: unfamiliar supplier")]
    assert not result.complete          # held is not done
    assert result.summary()["escalated"] == 1
    assert events.ESCALATED in {e.kind for e in bus.history()}


def test_a_refusal_is_still_a_refusal():
    def refuse(node):
        raise RuntimeError("insufficient budget")

    f, _ = make(10_000 * RUPEE,
                bounds=funnel.Bounds(decompose_floor_paise=50_000 * RUPEE))
    result = f.run(lambda n: [], refuse)
    assert result.root.children[0].state == funnel.REFUSED
    assert len(result.refusals) == 1 and result.escalations == []


def test_a_grant_carries_the_datalog_but_never_the_token():
    """The Datalog says what a holder may do and is safe to show. The signed
    base64 is a bearer credential and must never leave the process."""
    f, bus = make(bounds=funnel.Bounds(max_nodes=200))
    f.run(even_split(2), noop_execute)

    grants = [e for e in bus.history() if e.kind == events.GRANTED]
    assert grants
    for g in grants:
        assert g.detail["blocks"], "no Datalog on the grant"
        assert g.detail["token_bytes"] > 0
        joined = " ".join(g.detail["blocks"])
        assert "check if" in joined
        # Nothing anywhere in the event may be a serialised token.
        for value in g.detail.values():
            assert not (isinstance(value, str) and len(value) > 400 and " " not in value)

    deepest = max(grants, key=lambda e: e.depth)
    assert len(deepest.detail["blocks"]) == deepest.depth + 1


def test_the_mandate_itself_never_pays():
    """The root is the human's own authority: unattenuated, every capability,
    capped only by the ceiling. A live model answering "this task is atomic" once
    made the root settle the entire budget in one transaction."""
    paid = []
    f, bus = make(10_000 * RUPEE,
                  bounds=funnel.Bounds(decompose_floor_paise=50_000 * RUPEE))
    result = f.run(lambda n: [], lambda n: paid.append(n) or "order_1")

    assert [n.id for n in paid] == ["root.1"], "the root paid directly"
    payer = paid[0]
    assert payer.depth == 1
    assert payer.budget_paise == 10_000 * RUPEE

    tokens.verify(payer.token, Operation("pay", 1, depth=payer.depth))
    for forbidden in ("delegate", "search"):
        with pytest.raises(tokens.Denied):
            tokens.verify(payer.token, Operation(forbidden, 0, depth=payer.depth))

    assert result.root.state == funnel.DECOMPOSED
    assert result.committed_paise() == 10_000 * RUPEE


# --- the rail's ceiling -----------------------------------------------------


def test_a_leaf_the_rail_would_refuse_is_refused_first():
    """Razorpay rejected a Rs 6,00,000 leaf live, after earlier leaves in the
    same tree had already paid. The floor sits below this cap, so an oversized
    node keeps splitting on its own - the only way to reach a leaf that is too
    big is to run out of depth, and then there is nothing left to try."""
    paid = []
    f, bus = make(600_000 * RUPEE, max_depth=2, bounds=funnel.Bounds(
        max_depth=2, max_nodes=500,
        decompose_floor_paise=1 * RUPEE, max_leaf_paise=50_000 * RUPEE))
    result = f.run(even_split(3), lambda n: paid.append(n) or "order")

    # Depth 2 leaves are Rs 66,666 - over the cap, and nothing was attempted.
    assert paid == []
    assert not result.complete
    assert all(r == "above the rail's per-order limit" for _, r in result.refusals)

    hit = [e for e in bus.history() if e.kind == events.BOUND_HIT and e.detail.get("fault")]
    assert hit and hit[0].detail["limit_paise"] == 50_000 * RUPEE


def test_enough_depth_brings_every_leaf_under_the_cap():
    paid = []
    f, _ = make(600_000 * RUPEE, bounds=funnel.Bounds(
        max_depth=8, max_nodes=500,
        # Rs 6,00,000 thirds down to Rs 7,407 at depth 4: 121 nodes, every leaf
        # comfortably under the cap.
        decompose_floor_paise=10_000 * RUPEE, max_leaf_paise=50_000 * RUPEE))
    result = f.run(even_split(3), lambda n: paid.append(n) or "order")

    assert paid
    assert result.complete
    for node in paid:
        assert node.budget_paise <= 50_000 * RUPEE


def test_an_unpayable_configuration_is_refused_outright():
    """A floor at or above the cap leaves no payable amount at all."""
    with pytest.raises(ValueError, match="rail cap"):
        funnel.Bounds(decompose_floor_paise=50_000 * RUPEE, max_leaf_paise=50_000 * RUPEE)


# --- judging the plan, not just the payment ---------------------------------
#
# Every bound in this file is arithmetic. None of them can see whether the
# sub-tasks have anything to do with what the human authorised.


def approve_all(node, subtasks):
    return funnel.CriticVerdict(approve=True, reason="fits the mandate", confidence=0.9)


def test_an_approving_critic_changes_nothing():
    f, _ = make(bounds=funnel.Bounds(max_nodes=200,
                                     decompose_floor_paise=10_000 * RUPEE),
                critic=approve_all)
    result = f.run(even_split(3), noop_execute)
    assert result.complete
    assert result.critiques == []
    assert len(result.leaves) == 81


def test_a_refusing_critic_stops_that_branch_before_authority_exists():
    """The point of the seam: refused at the plan, so no token is ever minted."""
    def picky(node, subtasks):
        if node.id == "root.2":
            return funnel.CriticVerdict(False, "nothing here matches the mandate", 0.9)
        return approve_all(node, subtasks)

    f, bus = make(bounds=funnel.Bounds(max_nodes=200,
                                       decompose_floor_paise=10_000 * RUPEE),
                  critic=picky)
    result = f.run(even_split(3), noop_execute)

    assert not result.complete
    assert [r for _, r in result.refusals] == ["critic"]
    assert result.critiques == [("root.2", "nothing here matches the mandate")]

    # root.2 has no children at all - nothing was attenuated from it.
    refused = next(n for n in result.root.walk() if n.id == "root.2")
    assert refused.children == []
    # Its siblings finished normally.
    assert any(n.id.startswith("root.1.") for n in result.root.walk())

    hit = [e for e in bus.history()
           if e.kind == events.BOUND_HIT and e.detail.get("reason") == "critic"]
    assert not hit or hit[0].detail.get("fault")


def test_a_broken_critic_leaves_the_run_exactly_as_it_would_have_been():
    """The trust boundary. Judgement is a second layer; if it were a
    prerequisite, one model outage would stop all spending."""
    def broken(node, subtasks):
        raise RuntimeError("provider unreachable")

    shape = funnel.Bounds(max_nodes=200, decompose_floor_paise=10_000 * RUPEE)
    without, _ = make(bounds=shape)
    baseline = without.run(even_split(3), noop_execute)

    withc, bus = make(bounds=shape, critic=broken)
    result = withc.run(even_split(3), noop_execute)

    assert result.summary() == baseline.summary()
    assert result.complete
    # Recorded as ordinary progress, not a fault: nothing went wrong with the run.
    notes = [e for e in bus.history()
             if e.kind == events.BOUND_HIT and "critic unavailable" in str(e.detail.get("reason"))]
    assert notes and notes[0].detail["fault"] is False


def test_no_critic_is_the_default_and_costs_nothing():
    f, _ = make(bounds=funnel.Bounds(max_nodes=200,
                                     decompose_floor_paise=10_000 * RUPEE))
    assert f.critic is None
    assert f.run(even_split(3), noop_execute).complete


def test_the_bounds_run_first_so_a_broken_plan_costs_no_model_call():
    """Arithmetic is cheap and certain. Only a structurally sound plan is worth
    asking an expensive question about."""
    asked = []

    def counting(node, subtasks):
        asked.append(node.id)
        return approve_all(node, subtasks)

    # Allocates twice the parent's budget: conservation refuses it.
    f, _ = make(critic=counting)
    result = f.run(
        lambda n: [funnel.SubTask(f"{n.description} greedy", n.budget_paise * 2)],
        noop_execute)

    assert [r for _, r in result.refusals] == ["conservation"]
    assert asked == [], "the critic was consulted on a plan the bounds had already refused"
