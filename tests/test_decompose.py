"""The one model call per branch, and how little it is trusted."""

from dataclasses import dataclass

from agent.nodes.decompose_node import (
    Decomposition, ProposedSubTask, _settle, make_decomposer)
from pocketchange import funnel
from pocketchange.policy import RUPEE


@dataclass
class FakeChain:
    """Stands in for StructuredChain. Returns scripted answers, counts calls."""

    answers: list
    calls: int = 0

    def invoke(self, variables):
        self.calls += 1
        answer = self.answers[min(self.calls - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def node(budget=100_000 * RUPEE, depth=0):
    return funnel.TaskNode("root", None, depth, "equip the office", budget, token=None)


def test_over_allocation_is_rescaled_not_refused():
    """An arithmetic slip about a division we can do ourselves."""
    out = _settle([ProposedSubTask(description="a", budget_rupees=900),
                   ProposedSubTask(description="b", budget_rupees=900)], 1_000 * RUPEE)
    assert [s.budget_paise for s in out] == [500 * RUPEE, 500 * RUPEE]


def test_under_allocation_is_left_alone():
    """Spending less than allowed is a decision, not an error."""
    out = _settle([ProposedSubTask(description="a", budget_rupees=100)], 1_000 * RUPEE)
    assert out[0].budget_paise == 100 * RUPEE


def test_a_named_supplier_is_required_for_specific_sourcing():
    """A missing name is a missing instruction, not a narrower one.

    It falls back to None - inherit the parent's - rather than to catalogue.
    Substituting a concrete sourcing for an absent one is how the person's
    choice got dropped one layer below the root.
    """
    out = _settle([ProposedSubTask(description="a", budget_rupees=10,
                                   sourcing="specific")], 1_000 * RUPEE)
    assert out[0].sourcing is None and out[0].supplier is None


def test_sourcing_left_unset_stays_unset_so_the_parent_decides():
    """The bug this pins: a schema defaulting to "catalogue" made every child
    declare a sourcing, and an explicit value beats inheritance - so a person's
    "find the best price" reached the root and nothing below it."""
    out = _settle([ProposedSubTask(description="a", budget_rupees=10)],
                  1_000 * RUPEE)
    assert out[0].sourcing is None


def test_a_named_supplier_survives():
    out = _settle([ProposedSubTask(description="a", budget_rupees=10,
                                   sourcing="specific", supplier="meridian-systems")],
                  1_000 * RUPEE)
    assert out[0].sourcing == "specific" and out[0].supplier == "meridian-systems"


def test_zero_budget_sub_tasks_are_dropped():
    out = _settle([ProposedSubTask(description="a", budget_rupees=0),
                   ProposedSubTask(description="b", budget_rupees=10)], 1_000 * RUPEE)
    assert [s.description for s in out] == ["b"]


def test_an_atomic_task_is_a_real_answer_not_a_retry():
    """Where there is genuinely nothing to divide, one answer settles it.

    A node with room to split is a different case and is asked twice - see
    test_one_empty_answer_is_not_enough_to_call_a_big_task_atomic."""
    chain = FakeChain([Decomposition(subtasks=[], reasoning="single purchase")])
    small = node(budget=6_000 * RUPEE)          # under twice the Rs 5,000 floor
    assert make_decomposer(chain=chain)(small) == []
    assert chain.calls == 1


def test_a_flaky_model_is_retried_rather_than_read_as_atomic():
    """Structured output on this family has returned nonsense before, and an
    empty decomposition read as 'already atomic' would silently truncate a tree."""
    good = Decomposition(
        subtasks=[ProposedSubTask(description="engineering", budget_rupees=500)],
        reasoning="by department")
    chain = FakeChain([RuntimeError("schema mismatch"), RuntimeError("again"), good])
    out = make_decomposer(chain=chain)(node())
    assert chain.calls == 3
    assert [s.description for s in out] == ["engineering"]


def test_a_model_that_never_answers_raises_rather_than_returning_nothing():
    import pytest

    chain = FakeChain([RuntimeError("down")])
    with pytest.raises(RuntimeError):
        make_decomposer(chain=chain)(node())
    assert chain.calls == 3


def test_the_funnel_runs_on_a_scripted_chain_end_to_end():
    """The funnel takes a callable and never learns what produced the answer."""
    from datetime import datetime, timedelta, timezone
    from pocketchange import events, identity, token as tokens

    answer = Decomposition(
        subtasks=[ProposedSubTask(description=f"part {i}", budget_rupees=2_000)
                  for i in range(3)],
        reasoning="three ways")
    chain = FakeChain([answer])
    root = tokens.mint(identity.ephemeral(), budget_paise=10_000 * RUPEE, max_depth=8,
                       expires=datetime.now(timezone.utc) + timedelta(hours=1))
    f = funnel.Funnel(root, description="equip the office",
                      budget_paise=10_000 * RUPEE, bus=events.EventBus(history=1000),
                      bounds=funnel.Bounds(decompose_floor_paise=5_000 * RUPEE))
    result = f.run(make_decomposer(chain=chain), lambda n: "paid")

    assert result.nodes == 4
    assert result.complete
    assert chain.calls == 1        # one call for the one branch node


def test_one_empty_answer_is_not_enough_to_call_a_big_task_atomic():
    """A single flaky sample used to collapse the tree: the node then pays its
    whole budget in one transaction. Seen live on a Rs 6,00,000 task that the
    same prompt divided into four streams on the next call."""
    good = Decomposition(
        subtasks=[ProposedSubTask(description=f"stream {i}", budget_rupees=250)
                  for i in range(4)],
        reasoning="by category")
    chain = FakeChain([Decomposition(subtasks=[], reasoning="atomic"), good])

    out = make_decomposer(bounds=funnel.Bounds(decompose_floor_paise=100 * RUPEE),
                          chain=chain)(node(budget=1_000 * RUPEE))
    assert chain.calls == 2
    assert len(out) == 4


def test_a_task_near_the_floor_is_taken_at_its_word():
    """Below twice the floor there is nothing worth splitting, so one answer
    is enough and we do not burn a second model call arguing."""
    chain = FakeChain([Decomposition(subtasks=[], reasoning="atomic")])
    out = make_decomposer(bounds=funnel.Bounds(decompose_floor_paise=600 * RUPEE),
                          chain=chain)(node(budget=1_000 * RUPEE))
    assert chain.calls == 1
    assert out == []


def test_a_persistently_empty_answer_is_finally_accepted():
    chain = FakeChain([Decomposition(subtasks=[], reasoning="atomic")])
    out = make_decomposer(bounds=funnel.Bounds(decompose_floor_paise=100 * RUPEE),
                          chain=chain)(node(budget=1_000 * RUPEE))
    assert chain.calls == 3
    assert out == []
