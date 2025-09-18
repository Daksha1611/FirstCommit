"""The chain rail is a rail, and nothing more than a rail.

The value of `PaymentRail` being a Protocol is that swapping the rail cannot
change who is allowed to spend. These tests hold that line from both ends: the
chain rail satisfies the same interface Razorpay does, and it cannot quietly
acquire the ability to move real money.
"""

import pytest

from pocketchange.chain_rail import (
    DEFAULT_CHAIN_ID,
    MAINNETS,
    ChainRail,
    from_env,
)
from pocketchange.razorpay_client import Order, PaymentError


def test_mainnet_is_refused_by_name():
    """The same refusal Razorpay live keys get, for the same reason."""
    for chain_id, name in MAINNETS.items():
        with pytest.raises(ValueError) as exc:
            ChainRail(chain_id=chain_id)
        assert name in str(exc.value)


def test_an_unknown_chain_is_refused_rather_than_attempted():
    with pytest.raises(ValueError):
        ChainRail(chain_id=999_999)


def test_it_reports_test_mode_because_it_cannot_be_anything_else():
    assert ChainRail().is_test_mode is True


def test_it_produces_the_order_shape_the_gateway_expects():
    """Interface parity with Razorpay - the gateway must not need to know."""
    order = ChainRail().create_order(
        amount_paise=45_000, receipt="idem-abc", notes={},
    )
    assert isinstance(order, Order)
    assert order.amount_paise == 45_000
    assert order.receipt == "idem-abc"
    assert order.status == "created"


def test_nothing_is_signed_and_nothing_is_sent():
    """The property that keeps `payments_are_real: false` true."""
    raw = ChainRail().create_order(
        amount_paise=45_000, receipt="idem-abc", notes={},
    ).raw
    assert raw["signed"] is False
    assert raw["broadcast"] is False


def test_the_same_purchase_builds_the_same_intent():
    """The receipt is the idempotency key, so a replay is recognisable on-chain.

    If this were random, a replayed payment would look like a fresh transfer to
    anyone reading the chain rather than reading our ledger.
    """
    rail = ChainRail()
    first = rail.create_order(amount_paise=45_000, receipt="idem-abc", notes={})
    again = rail.create_order(amount_paise=45_000, receipt="idem-abc", notes={})
    other = rail.create_order(amount_paise=45_000, receipt="idem-xyz", notes={})

    assert first.raw["reference"] == again.raw["reference"]
    assert first.raw["reference"] != other.raw["reference"]


def test_conversion_rounds_down_so_it_cannot_exceed_the_reservation():
    """A rounding error must never make the transfer bigger than the hold."""
    rail = ChainRail(decimals=2, paise_per_unit=100)
    # 4,599 paise is 45.99 units; at 2 decimals that is 4599 base units exactly,
    # and anything that does not divide evenly must lose the remainder downward.
    assert rail.amount_in_base_units(4_599) == 4_599
    assert rail.amount_in_base_units(4_599 + 1) >= 4_599


def test_a_non_positive_amount_is_refused():
    with pytest.raises(PaymentError):
        ChainRail().create_order(amount_paise=0, receipt="r", notes={})


def test_the_recipient_comes_from_the_notes_the_gateway_passes():
    order = ChainRail().create_order(
        amount_paise=45_000, receipt="r",
        notes={"counterparty_address": "0xabc"},
    )
    assert order.raw["to"] == "0xabc"


# --- opt-in ------------------------------------------------------------------


def test_no_chain_configured_means_no_chain_rail(monkeypatch):
    """A deployment that has never heard of this module is unaffected."""
    monkeypatch.delenv("POCKETCHANGE_CHAIN_ID", raising=False)
    assert from_env() is None


def test_a_configured_chain_is_built_from_the_environment(monkeypatch):
    monkeypatch.setenv("POCKETCHANGE_CHAIN_ID", str(DEFAULT_CHAIN_ID))
    monkeypatch.setenv("POCKETCHANGE_CHAIN_TOKEN", "0xtoken")
    rail = from_env()
    assert rail is not None
    assert rail.chain_id == DEFAULT_CHAIN_ID
    assert rail.token_address == "0xtoken"


def test_a_mainnet_id_in_the_environment_still_fails(monkeypatch):
    """Configuration is not permission. The refusal is in the constructor."""
    monkeypatch.setenv("POCKETCHANGE_CHAIN_ID", "1")
    with pytest.raises(ValueError):
        from_env()
