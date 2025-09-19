"""The model's opinion on a plan, and the line it must never cross.

Every bound in the funnel is arithmetic. A decomposition that divides its budget
perfectly and buys entirely the wrong things satisfies all of them. This is the
layer that can say so - and it is advisory, so a run with a broken critic must
be indistinguishable from a run with none.

No model is called here.
"""

from dataclasses import dataclass

import pytest

from agent.nodes.critic_node import Critique, _render, make_critic
from pocketchange import funnel
from pocketchange.policy import RUPEE


@dataclass
class FakeChain:
    answers: list
    calls: int = 0
    seen: list = None

    def __post_init__(self):
        self.seen = []

    def invoke(self, variables):
        self.calls += 1
        self.seen.append(variables)
        answer = self.answers[min(self.calls - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def node(depth=0, budget=100_000 * RUPEE, description="equip the office"):
    return funnel.TaskNode("root", None, depth, description, budget, token=None)


def subtasks(*pairs):
    return [funnel.SubTask(d, b * RUPEE) for d, b in pairs]


# --- the verdict ------------------------------------------------------------


def test_an_approval_passes_through():
    chain = FakeChain([Critique(approve=True, reason="matches the mandate", confidence=0.9)])
    v = make_critic("kit out the office", chain)(node(), subtasks(("desks", 500)))
    assert v.approve and v.confidence == 0.9


def test_a_refusal_carries_its_reason():
    chain = FakeChain([Critique(approve=False, reason="chairs under a laptop mandate", confidence=0.8)])
    v = make_critic("buy laptops", chain)(node(), subtasks(("chairs", 500)))
    assert not v.approve
    assert "chairs under a laptop mandate" in v.reason


def test_a_verdict_without_words_still_says_something():
    """A blank reason on a refusal would leave a run refused for no stated
    cause, which is worse than no critic at all."""
    chain = FakeChain([Critique(approve=False, reason="", confidence=0.5)])
    assert make_critic("x", chain)(node(), subtasks(("y", 1))).reason == "refused"


# --- what it is shown -------------------------------------------------------


def test_it_sees_what_the_human_authorised_not_just_the_parent():
    """Judging a split only against its parent misses the drift that matters: a
    tree that divides beautifully and walks away from what was asked for."""
    chain = FakeChain([Critique(approve=True)])
    make_critic("restock the stationery cupboard", chain)(
        node(description="office supplies"), subtasks(("paper", 500)))
    seen = chain.seen[0]
    assert seen["intent"] == "restock the stationery cupboard"
    assert seen["description"] == "office supplies"


def test_the_sub_tasks_are_rendered_with_their_budgets():
    rendered = _render(subtasks(("twelve desks", 2_000), ("twelve chairs", 1_500)))
    assert "twelve desks - 2,000 rupees" in rendered
    assert "twelve chairs - 1,500 rupees" in rendered


def test_a_named_supplier_and_sourcing_reach_the_critic():
    rendered = _render([
        funnel.SubTask("toner", 500 * RUPEE, sourcing="specific", supplier="northgate"),
        funnel.SubTask("laptops", 500 * RUPEE, sourcing="best"),
    ])
    assert "via northgate" in rendered and "[specific]" in rendered
    assert "[best]" in rendered


# --- the trust boundary -----------------------------------------------------


def test_a_broken_critic_does_not_stop_the_funnel():
    """The whole line. If judgement became a prerequisite, one outage would stop
    all spending - the dependency this architecture exists to avoid."""
    from datetime import datetime, timedelta, timezone

    from pocketchange import events, identity, token as tokens

    chain = FakeChain([RuntimeError("every provider is down")])
    root = tokens.mint(identity.ephemeral(), budget_paise=100_000 * RUPEE, max_depth=8,
                       expires=datetime.now(timezone.utc) + timedelta(hours=1))
    f = funnel.Funnel(root, description="equip the office",
                      budget_paise=100_000 * RUPEE, bus=events.EventBus(history=1000),
                      bounds=funnel.Bounds(decompose_floor_paise=20_000 * RUPEE),
                      critic=make_critic("equip the office", chain))

    result = f.run(
        lambda n: ([] if n.depth else
                   [funnel.SubTask(f"part {i}", n.budget_paise // 3) for i in range(3)]),
        lambda n: "paid")

    assert result.complete
    assert result.critiques == []
    assert len(result.leaves) == 3
    assert chain.calls == 1        # it was asked, and its failure was absorbed


def test_a_critic_is_asked_once_per_branch_not_once_per_node():
    """Branch points are where authority is created. Judging leaves too would
    multiply cost for the layer that holds least."""
    from datetime import datetime, timedelta, timezone

    from pocketchange import events, identity, token as tokens

    chain = FakeChain([Critique(approve=True)])
    root = tokens.mint(identity.ephemeral(), budget_paise=90_000 * RUPEE, max_depth=8,
                       expires=datetime.now(timezone.utc) + timedelta(hours=1))
    f = funnel.Funnel(root, description="equip the office",
                      budget_paise=90_000 * RUPEE, bus=events.EventBus(history=1000),
                      bounds=funnel.Bounds(decompose_floor_paise=20_000 * RUPEE),
                      critic=make_critic("equip the office", chain))
    result = f.run(
        lambda n: ([] if n.depth else
                   [funnel.SubTask(f"part {i}", n.budget_paise // 3) for i in range(3)]),
        lambda n: "paid")

    assert len(result.leaves) == 3
    assert chain.calls == 1, "one plan was proposed, so one question should be asked"
