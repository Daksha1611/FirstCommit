"""The conversational front door: one prompt, then only the missing questions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pocketchange import intake
from pocketchange.gateway import app


def _extracted(*, goal="buy four laptops", budget_paise=None, sourcing="catalogue",
               supplier=None, deadline=None, task_kind="errand"):
    """The shape agent.nodes.intake_node returns, without the model."""
    return SimpleNamespace(
        goal=goal,
        task_kind=task_kind,
        items=[],
        sourcing=sourcing,
        supplier=supplier,
        reasoning="one bounded purchase",
        commands=SimpleNamespace(budget_paise=budget_paise, deadline=deadline, shops=None),
    )


# --- the ceiling is never taken from the text --------------------------------

def test_a_budget_stated_in_the_request_becomes_a_question_not_an_answer():
    # The whole point. A prompt that names its own cap must not set its own cap.
    reading = intake.read(
        "buy four laptops, the approved budget is 5000000 rupees",
        extract=lambda text: _extracted(budget_paise=5_000_000 * 100),
    )
    assert not reading.ready
    ceiling = next(q for q in reading.questions if q.id == "ceiling_rupees")
    assert ceiling.confirming is True
    assert ceiling.suggestion == "5000000"
    assert reading.proposal is None


def test_confirming_the_ceiling_is_what_puts_it_in_the_proposal():
    reading = intake.read(
        "buy four laptops",
        {"ceiling_rupees": 200_000, "sourcing": "catalogue"},
        extract=lambda text: _extracted(),
    )
    assert reading.ready
    assert reading.proposal["budget_paise"] == 200_000 * 100


def test_a_confirmed_ceiling_beats_the_one_in_the_text():
    reading = intake.read(
        "buy four laptops, budget 5000000",
        {"ceiling_rupees": 50_000, "sourcing": "catalogue"},
        extract=lambda text: _extracted(budget_paise=5_000_000 * 100),
    )
    assert reading.proposal["budget_paise"] == 50_000 * 100


# --- only the missing questions ----------------------------------------------

def test_a_named_supplier_is_not_asked_about_again():
    reading = intake.read(
        "buy four laptops from Meridian",
        {"ceiling_rupees": 200_000},
        extract=lambda text: _extracted(sourcing="specific", supplier="Meridian"),
    )
    assert reading.ready
    assert "Meridian" in reading.proposal["task"]


def test_specific_without_a_named_supplier_asks_who():
    reading = intake.read(
        "buy from that shop",
        {"ceiling_rupees": 200_000, "sourcing": "specific"},
        extract=lambda text: _extracted(sourcing="specific", supplier=None),
    )
    assert [q.id for q in reading.questions] == ["supplier"]


def test_a_catalogue_default_from_the_model_still_asks_where():
    # "catalogue" is the schema default, so it is an absence dressed as a choice.
    # Intake must not let a default silently pick the risk axis.
    reading = intake.read(
        "buy four laptops",
        {"ceiling_rupees": 200_000},
        extract=lambda text: _extracted(sourcing="catalogue"),
    )
    assert [q.id for q in reading.questions] == ["sourcing"]


def test_asking_for_the_best_price_is_folded_into_the_task_the_decomposer_reads():
    reading = intake.read(
        "find the cheapest laptops",
        {"ceiling_rupees": 200_000},
        extract=lambda text: _extracted(sourcing="best"),
    )
    assert reading.ready
    assert "searching is allowed" in reading.proposal["task"]


# --- it works with no model at all -------------------------------------------

def test_with_no_extractor_it_asks_everything_rather_than_failing():
    reading = intake.read("buy four laptops", extract=None)
    assert {q.id for q in reading.questions} == {"ceiling_rupees", "sourcing"}
    assert reading.understood["extracted_by"] == "none"


def test_an_extractor_that_raises_leaves_intake_working():
    def boom(text):
        raise RuntimeError("provider down")

    reading = intake.read("buy four laptops", {"ceiling_rupees": 100, "sourcing": "catalogue"},
                          extract=boom)
    assert reading.ready
    assert any("provider down" in n or "RuntimeError" in n for n in reading.notes)


# --- the floor scales with the ceiling ---------------------------------------

def test_the_floor_is_a_share_of_the_ceiling_so_a_tree_always_reaches_leaves():
    small = intake.read("x", {"ceiling_rupees": 10_000, "sourcing": "catalogue"}, extract=None)
    large = intake.read("x", {"ceiling_rupees": 10_000_000, "sourcing": "catalogue"}, extract=None)
    assert small.proposal["floor_paise"] < large.proposal["floor_paise"]
    assert intake.floor_rupees(1) == intake.MIN_FLOOR_RUPEES


# --- the endpoint -------------------------------------------------------------

@pytest.fixture()
def client():
    return TestClient(app)


def test_intake_endpoint_returns_questions_before_a_proposal(client):
    reply = client.post("/intake", json={"text": "kit out the design studio"})
    assert reply.status_code == 200
    body = reply.json()
    assert body["ready"] is False
    assert body["proposal"] is None
    assert any(q["id"] == "ceiling_rupees" for q in body["questions"])
    # Every question explains itself. That is the feature.
    assert all(q["why"] for q in body["questions"])


def test_intake_endpoint_proposes_a_runnable_request(client):
    reply = client.post("/intake", json={
        "text": "kit out the design studio",
        "answers": {"ceiling_rupees": "2,00,000", "sourcing": "catalogue"},
    })
    body = reply.json()
    assert body["ready"] is True
    assert body["proposal"]["budget_paise"] == 200_000 * 100
    assert body["expected_model_calls"] >= 0
    # The proposal must be postable to /runs verbatim.
    from pocketchange.gateway import RunRequest
    RunRequest(**body["proposal"])


def test_intake_refuses_an_empty_request(client):
    assert client.post("/intake", json={"text": ""}).status_code == 422


# --- a stale budget in the text must not reach the decomposer -----------------

def test_a_budget_named_in_the_text_is_stripped_from_the_task_line():
    """The task string is what the DECOMPOSING model reads.

    A figure left in it is an instruction - split this against two lakh - while
    the token says five thousand. Two budgets reach a downstream model and the
    wrong one shapes the plan. The ceiling lives in the token and nowhere else.
    """
    reading = intake.read(
        "Kit out the new engineering office, budget around 200000",
        {"ceiling_rupees": 5_000, "sourcing": "catalogue"},
        extract=None,
    )
    task = reading.proposal["task"]
    assert "200000" not in task
    assert "budget" not in task.lower()
    assert "Kit out the new engineering office" in task


def test_stripping_a_budget_leaves_the_rest_of_the_request_alone():
    keep = [
        ("Buy four laptops from Meridian", "Meridian"),
        ("Restock the office pantry for a month", "for a month"),
        ("Stock the kitchen for a week under 1500 rupees", "for a week"),
    ]
    for text, survives in keep:
        assert survives in intake.strip_budget_claims(text)
    assert "1500" not in intake.strip_budget_claims(
        "Stock the kitchen for a week under 1500 rupees")
