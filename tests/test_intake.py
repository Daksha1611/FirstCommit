"""Intake classification: the branch everything else hangs off.

The chain is stubbed. These assert the node's contract - guards, defaults, the
shape of the slice it returns - not the model's judgement, which is measured
separately by running it against real requests.
"""

import pytest

from agent.nodes import intake_node
from agent.nodes.intake_node import (
    IntakeCommands,
    IntakeOutput,
    RequestedItem,
    build_task,
    intake_chain,
)


def make(kind="errand", budget=None, items=(), goal="buy the things"):
    return IntakeOutput(
        task_kind=kind,
        goal=goal,
        items=[RequestedItem(name=n, quantity=q) for n, q in items],
        commands=IntakeCommands(budget_paise=budget),
        reasoning="because",
    )


def test_build_task_shape():
    slice_ = build_task(make(items=[("rice", "4 kg")]))
    assert slice_["task_kind"] == "errand"
    assert slice_["items"] == [{"name": "rice", "quantity": "4 kg"}]
    assert slice_["commands"]["budget_paise"] is None


def test_intake_runs_once(monkeypatch):
    """A cycle back through the graph must not re-classify."""
    calls = []

    def spy(_variables):
        calls.append(1)
        return make()

    monkeypatch.setattr(intake_chain, "invoke", spy)

    first = intake_node({"request": "buy rice"})
    assert first["task_kind"] == "errand"
    assert len(calls) == 1

    again = intake_node({"request": "buy rice", **first})
    assert again == {}
    assert len(calls) == 1


def test_empty_request_defaults_to_errand_without_calling_the_model(monkeypatch):
    def explode(_variables):
        raise AssertionError("should not call the model on an empty request")

    monkeypatch.setattr(intake_chain, "invoke", explode)
    out = intake_node({"request": "   "})
    assert out["task_kind"] == "errand"
    assert out["items"] == []


@pytest.mark.parametrize("kind", ["errand", "target", "standing"])
def test_every_kind_round_trips(monkeypatch, kind):
    monkeypatch.setattr(intake_chain, "invoke", lambda _v: make(kind=kind))
    assert intake_node({"request": "x"})["task_kind"] == kind


def test_budget_is_carried_in_paise(monkeypatch):
    monkeypatch.setattr(intake_chain, "invoke", lambda _v: make(budget=150_000))
    out = intake_node({"request": "stock the kitchen under 1500 rupees"})
    assert out["commands"]["budget_paise"] == 150_000


def test_unstated_budget_stays_none(monkeypatch):
    """Inventing a cap would be inventing permission to spend."""
    monkeypatch.setattr(intake_chain, "invoke", lambda _v: make())
    out = intake_node({"request": "buy rice"})
    assert out["commands"]["budget_paise"] is None
