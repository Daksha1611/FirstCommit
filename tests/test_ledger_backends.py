"""Both ledgers, the same assertions.

The gateway is written against a Protocol and "never learns which backend it
has". That is a claim, and until this file it was only ever exercised against
the in-memory one - the durable backend had no tests at all, which for the
module the project describes as "the file the project exists for" is the gap
worth closing first.

So every property here runs twice: once against `MemoryLedger`, once against
`DynamoLedger` on a mocked DynamoDB. A backend that disagrees with the other
about what a ledger does fails, which is the only way the substitution claim
means anything.

No AWS account and no network: moto intercepts botocore. The same tests pass
against DynamoDB Local by exporting POCKETCHANGE_DDB_ENDPOINT.
"""

import pytest

from pocketchange import dynamo
from pocketchange.ledger import (
    DynamoLedger,
    InsufficientBudget,
    MemoryLedger,
    UnknownMandate,
    UnknownReservation,
)
from pocketchange.policy import RUPEE


@pytest.fixture(params=["memory", "dynamo"])
def ledger(request, monkeypatch):
    if request.param == "memory":
        yield MemoryLedger()
        return

    from moto import mock_aws

    # moto intercepts botocore, but boto3 still insists on finding credentials
    # before it will build a client, and conftest has deliberately removed them.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv(dynamo.ENDPOINT_ENV, raising=False)

    with mock_aws():
        dynamo.reset_cache()
        dynamo.create_table_if_absent()
        yield DynamoLedger()
    dynamo.reset_cache()


# --- the headline ----------------------------------------------------------


def test_cumulative_spend_is_refused(ledger):
    """Two payments of 3596 against a 6000 cap.

    Each is individually valid and a token as specified permits both. The ledger
    is what refuses the second.
    """
    ledger.open("m1", 600_000 * RUPEE)

    first = ledger.reserve("m1", 359_600 * RUPEE, "cart-a")
    ledger.commit(first.id)

    with pytest.raises(InsufficientBudget) as caught:
        ledger.reserve("m1", 359_600 * RUPEE, "cart-b")

    assert caught.value.available == 240_400 * RUPEE
    assert ledger.state("m1").committed_paise == 359_600 * RUPEE


def test_a_reservation_blocks_concurrent_overspend(ledger):
    """The race the two-phase design exists to close.

    Nothing has been charged yet, but the held amount is already unavailable.
    On DynamoDB the check and the decrement are one conditional update, so the
    window this closes does not exist rather than being kept shut.
    """
    ledger.open("m1", 400_000 * RUPEE)
    ledger.reserve("m1", 240_000 * RUPEE, "cart-a")

    with pytest.raises(InsufficientBudget):
        ledger.reserve("m1", 240_000 * RUPEE, "cart-b")


def test_an_over_budget_refusal_carries_the_numbers(ledger):
    """The audit trail and the dashboard both show the user why, rather than a
    bare refusal, so the figures have to survive the backend."""
    ledger.open("m1", 100_000 * RUPEE)
    ledger.reserve("m1", 60_000 * RUPEE, "cart-a")

    with pytest.raises(InsufficientBudget) as caught:
        ledger.reserve("m1", 60_000 * RUPEE, "cart-b")

    refusal = caught.value
    assert refusal.cap == 100_000 * RUPEE
    assert refusal.reserved == 60_000 * RUPEE
    assert refusal.committed == 0
    assert refusal.requested == 60_000 * RUPEE
    assert refusal.available == 40_000 * RUPEE


# --- reserve, commit, release ----------------------------------------------


def test_committing_turns_a_hold_into_spend(ledger):
    ledger.open("m1", 400_000 * RUPEE)
    held = ledger.reserve("m1", 240_000 * RUPEE, "cart-a")

    assert ledger.state("m1").reserved_paise == 240_000 * RUPEE
    ledger.commit(held.id)

    after = ledger.state("m1")
    assert after.committed_paise == 240_000 * RUPEE
    assert after.reserved_paise == 0


def test_release_returns_budget_and_allows_retry(ledger):
    ledger.open("m1", 400_000 * RUPEE)
    held = ledger.reserve("m1", 240_000 * RUPEE, "cart-a")
    ledger.release(held.id)

    assert ledger.state("m1").available_paise == 400_000 * RUPEE
    # The same key is retryable: a payment that never happened should be, and
    # only a *successful* charge stays pinned.
    ledger.reserve("m1", 240_000 * RUPEE, "cart-a")


def test_a_settled_reservation_cannot_be_settled_again(ledger):
    """Committing twice would double-count the spend."""
    ledger.open("m1", 400_000 * RUPEE)
    held = ledger.reserve("m1", 100_000 * RUPEE, "cart-a")
    ledger.commit(held.id)

    with pytest.raises(UnknownReservation):
        ledger.commit(held.id)
    assert ledger.state("m1").committed_paise == 100_000 * RUPEE


def test_a_settled_reservation_cannot_be_released(ledger):
    """Releasing after settlement would hand back money already spent."""
    ledger.open("m1", 400_000 * RUPEE)
    held = ledger.reserve("m1", 100_000 * RUPEE, "cart-a")
    ledger.commit(held.id)

    with pytest.raises(UnknownReservation):
        ledger.release(held.id)
    assert ledger.state("m1").committed_paise == 100_000 * RUPEE


# --- idempotency -----------------------------------------------------------


def test_reserve_is_idempotent_on_key(ledger):
    """An in-TTL replay gets the original reservation, not a second charge."""
    ledger.open("m1", 400_000 * RUPEE)
    a = ledger.reserve("m1", 160_000 * RUPEE, "same-cart")
    b = ledger.reserve("m1", 160_000 * RUPEE, "same-cart")

    assert a.id == b.id
    assert a.amount_paise == b.amount_paise
    assert ledger.state("m1").reserved_paise == 160_000 * RUPEE


# --- failing closed --------------------------------------------------------


def test_an_unknown_mandate_fails_closed(ledger):
    with pytest.raises(UnknownMandate):
        ledger.reserve("never-opened", 1, "k")


def test_reading_an_unknown_mandate_raises(ledger):
    with pytest.raises(UnknownMandate):
        ledger.state("never-opened")


def test_an_unknown_reservation_fails_closed(ledger):
    with pytest.raises(UnknownReservation):
        ledger.commit("no-such-reservation")


def test_the_cap_cannot_be_raised_by_reopening(ledger):
    """Otherwise a compromised agent raises its own limit by asking twice."""
    ledger.open("m1", 200_000 * RUPEE)
    with pytest.raises(Exception, match="refusing to reopen"):
        ledger.open("m1", 2_000_000 * RUPEE)
    assert ledger.state("m1").cap_paise == 200_000 * RUPEE


def test_reopening_at_the_same_cap_is_harmless(ledger):
    """`open` is called on every run and must stay safe to repeat."""
    ledger.open("m1", 200_000 * RUPEE)
    ledger.reserve("m1", 50_000 * RUPEE, "cart-a")
    ledger.open("m1", 200_000 * RUPEE)
    assert ledger.state("m1").reserved_paise == 50_000 * RUPEE


def test_a_negative_cap_is_refused(ledger):
    with pytest.raises(ValueError):
        ledger.open("m1", -1)


@pytest.mark.parametrize("amount", [0, -1])
def test_a_non_positive_reservation_is_refused(ledger, amount):
    ledger.open("m1", 100_000 * RUPEE)
    with pytest.raises(ValueError):
        ledger.reserve("m1", amount, "cart-a")


def test_an_empty_idempotency_key_is_refused(ledger):
    ledger.open("m1", 100_000 * RUPEE)
    with pytest.raises(ValueError):
        ledger.reserve("m1", 1000, "   ")
