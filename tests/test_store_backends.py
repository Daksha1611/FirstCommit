"""The counterparty book and the standing-order bank, on both backends.

Same argument as tests/test_ledger_backends.py: the gateway holds these through
a Protocol and is not supposed to know which implementation it has, and that is
only true if both are held to the same assertions.

The standing-order bank especially. Its durable implementation did not exist -
`from_env` imported a module nobody had written and the except swallowed the
ImportError, so a configured deployment silently kept recurring authority in
process memory and lost it on the next deploy. Nothing failed, no test noticed,
and the feature whose whole point is outliving the conversation did not outlive
the process. Untested storage code is how that happens.
"""

import pytest

from pocketchange import counterparties as cp
from pocketchange import dynamo, memory
from pocketchange.policy import RUPEE


@pytest.fixture
def mocked_aws(monkeypatch):
    """A DynamoDB with the project's table already created."""
    from moto import mock_aws

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv(dynamo.ENDPOINT_ENV, raising=False)

    with mock_aws():
        dynamo.reset_cache()
        dynamo.create_table_if_absent()
        yield
    dynamo.reset_cache()


# --- the counterparty book --------------------------------------------------


@pytest.fixture(params=["memory", "dynamo"])
def book(request, mocked_aws):
    if request.param == "memory":
        return cp.InMemoryCounterparties()
    return cp.DynamoCounterparties()


def test_a_settlement_is_remembered(book):
    row = book.record("acme", amount_paise=500 * RUPEE, mandate_id="m1")
    assert row.id == "acme"
    assert row.orders == 1
    assert row.total_paise == 500 * RUPEE
    assert row.mandates == 1


def test_repeat_business_accumulates(book):
    book.record("acme", amount_paise=500 * RUPEE, mandate_id="m1")
    row = book.record("acme", amount_paise=300 * RUPEE, mandate_id="m2")
    assert row.orders == 2
    assert row.total_paise == 800 * RUPEE
    assert row.mandates == 2


def test_the_same_mandate_paying_twice_counts_once(book):
    """Mandate ids are kept as a set rather than counted.

    The figure answers "how many separate authorisations has this supplier been
    paid under", and a supplier paid twice under one mandate has been trusted
    once. On DynamoDB this is a native string set, so the union is atomic.
    """
    book.record("acme", amount_paise=500 * RUPEE, mandate_id="m1")
    row = book.record("acme", amount_paise=500 * RUPEE, mandate_id="m1")
    assert row.orders == 2
    assert row.mandates == 1


def test_a_flag_is_counted_without_inventing_an_order(book):
    """A refusal is not a sale. Flagging must not make the book report trade
    that never happened."""
    row = book.flag("acme", cp.REFUSED)
    assert row.refused == 1
    assert row.orders == 0
    assert row.total_paise == 0


@pytest.mark.parametrize("kind", cp.FLAGS)
def test_every_flag_round_trips(book, kind):
    book.flag("acme", kind)
    stored = book.lookup("acme")
    assert getattr(stored, "injections" if kind == cp.INJECTION else kind) == 1


def test_an_unknown_flag_is_refused(book):
    with pytest.raises(ValueError):
        book.flag("acme", "not-a-flag")


def test_an_unknown_counterparty_is_none_rather_than_an_error(book):
    """The monitor asks about parties it has never seen on most payments."""
    assert book.lookup("never-heard-of-them") is None


def test_a_blank_counterparty_is_refused(book):
    with pytest.raises(ValueError):
        book.record("   ", amount_paise=100, mandate_id="m1")


def test_all_is_ordered_by_how_much_business_they_have_done(book):
    book.record("small", amount_paise=100 * RUPEE, mandate_id="m1")
    for index in range(3):
        book.record("big", amount_paise=100 * RUPEE, mandate_id=f"m{index}")

    assert [row.id for row in book.all()] == ["big", "small"]


# --- the standing-order bank ------------------------------------------------


@pytest.fixture(params=["memory", "dynamo"])
def bank(request, mocked_aws):
    if request.param == "memory":
        return memory.InMemoryBank()
    return memory.DynamoBank()


def an_order(**kw):
    base = dict(
        instruction="keep the stationery cupboard stocked",
        department="facilities",
        period="month",
        period_budget_paise=10_000 * RUPEE,
        rules=(memory.ReorderRule(sku="PEN-BLU-1", reorder_point=10, target_level=50),),
    )
    base.update(kw)
    return memory.new_order(**base)


def test_an_order_survives_a_round_trip(bank):
    """The whole point of the durable bank, and what it never actually did."""
    order = bank.remember(an_order())
    recalled = bank.recall(order.id)

    assert recalled.id == order.id
    assert recalled.instruction == order.instruction
    assert recalled.period_budget_paise == order.period_budget_paise
    assert [rule.sku for rule in recalled.rules] == ["PEN-BLU-1"]
    assert recalled.rules[0].reorder_point == 10
    assert recalled.created_at == order.created_at


def test_recalling_an_unknown_order_fails_closed(bank):
    with pytest.raises(memory.UnknownOrder):
        bank.recall("so_nosuchthing")


def test_an_unapproved_order_does_not_run(bank):
    """Inert until a person authorises it. A standing order creates recurring
    authority from one sentence, in the mode where nobody is watching."""
    bank.remember(an_order())
    assert bank.standing_orders() == []
    assert len(bank.standing_orders(include_pending=True)) == 1


def test_approval_lets_it_run_and_is_recorded(bank):
    order = bank.remember(an_order())
    approved = bank.approve(order.id, by="dakshmehta")

    assert approved.approved is True
    assert any("approved by dakshmehta" in note for note in approved.notes)
    assert [o.id for o in bank.standing_orders()] == [order.id]


def test_orders_can_be_filtered_by_department(bank):
    first = bank.remember(an_order(department="facilities"))
    second = bank.remember(an_order(department="canteen"))
    bank.approve(first.id)
    bank.approve(second.id)

    assert [o.id for o in bank.standing_orders(department="canteen")] == [second.id]


def test_deactivating_takes_it_out_of_circulation(bank):
    order = bank.remember(an_order())
    bank.approve(order.id)
    bank.deactivate(order.id)

    assert bank.standing_orders() == []
    assert bank.standing_orders(include_pending=True) == []


# --- the period budget ------------------------------------------------------


def test_a_check_records_what_it_spent(bank):
    order = bank.remember(an_order())
    after = bank.record_check(order.id, spent_paise=2_000 * RUPEE, note="restocked pens")

    assert after.spent_this_period_paise == 2_000 * RUPEE
    assert after.remaining_paise == 8_000 * RUPEE
    assert after.checks == 1
    assert "restocked pens" in after.notes


def test_the_period_budget_is_enforced(bank):
    """The same class of invariant as the ledger's cap, and it has to hold on
    whichever backend is underneath."""
    order = bank.remember(an_order(period_budget_paise=1_000 * RUPEE))
    bank.record_check(order.id, spent_paise=900 * RUPEE, note="most of it")

    with pytest.raises(memory.MemoryError_):
        bank.record_check(order.id, spent_paise=200 * RUPEE, note="over")

    assert bank.recall(order.id).spent_this_period_paise == 900 * RUPEE


def test_an_elapsed_period_resets_the_budget(bank):
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    order = an_order(period_budget_paise=1_000 * RUPEE)
    long_ago = datetime.now(timezone.utc) - timedelta(days=400)
    bank.remember(replace(order, period_started_at=long_ago,
                          spent_this_period_paise=1_000 * RUPEE))

    after = bank.record_check(order.id, spent_paise=500 * RUPEE, note="new period")
    assert after.spent_this_period_paise == 500 * RUPEE


def test_notes_do_not_grow_without_bound(bank):
    """Twenty is the cap. An order checked nightly for a year would otherwise
    carry a document that grows until it stops fitting in a row."""
    order = bank.remember(an_order())
    for index in range(25):
        bank.record_check(order.id, spent_paise=1, note=f"check {index}")

    assert len(bank.recall(order.id).notes) == 20
