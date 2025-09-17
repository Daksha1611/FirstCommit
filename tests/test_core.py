"""Stage 02 and 03: tokens, ledger, idempotency, audit.

The spike's eleven gates proved the token layer. These add what the spike could
not test, because the spike had no state: cumulative spend and replay.
"""

from datetime import datetime, timedelta, timezone

import pytest

from pocketchange import identity, token
from pocketchange.audit import AuditLog, AuditTampered, Decision
from pocketchange.idempotency import ReplayStore, canonical, derive_key
from pocketchange.ledger import InsufficientBudget, MemoryLedger, UnknownMandate
from pocketchange.policy import RUPEE, Grant, Operation

NOW = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
EXPIRES = datetime(2026, 8, 31, tzinfo=timezone.utc)


@pytest.fixture
def principal():
    return identity.web("pocketchange.dev", "principal", keypair=_kp())


def _kp():
    from biscuit_auth import KeyPair

    return KeyPair()


@pytest.fixture
def root(principal):
    return token.mint(principal, budget_paise=2_000_000 * RUPEE, max_depth=3, expires=EXPIRES)


@pytest.fixture
def branches(root):
    """The sibling topology the spike forced on us."""
    payer = token.attenuate(
        root,
        to=identity.ephemeral(),
        grant=Grant(tools=("pay",), budget_paise=359_600 * RUPEE),
        context="paying for the assembled cart",
    )
    shopper = token.attenuate(
        root,
        to=identity.web("pocketchange.dev", "shopper"),
        grant=Grant(tools=("search", "cart"), budget_paise=600_000 * RUPEE),
        context="weekly grocery run",
    )
    return payer, shopper


# --- tokens ----------------------------------------------------------------


def test_payer_may_pay_within_cap(branches):
    payer, _ = branches
    token.verify(payer, Operation("pay", 359_600 * RUPEE, depth=1, at=NOW))


def test_shopper_may_not_pay(branches):
    """The injection defence, as a property rather than a hope."""
    _, shopper = branches
    with pytest.raises(token.Denied):
        token.verify(shopper, Operation("pay", 40_000 * RUPEE, depth=1, at=NOW))


def test_scope_cannot_be_widened(branches):
    payer, _ = branches
    with pytest.raises(token.Denied):
        token.verify(payer, Operation("payout", 40_000 * RUPEE, depth=1, at=NOW))


def test_tightest_budget_wins(branches):
    """Under the root's 5000 cap, over the payer's own 899."""
    payer, _ = branches
    with pytest.raises(token.Denied):
        token.verify(payer, Operation("pay", 480_000 * RUPEE, depth=1, at=NOW))


def test_expired_token_refused(branches):
    payer, _ = branches
    late = EXPIRES + timedelta(days=1)
    with pytest.raises(token.Denied):
        token.verify(payer, Operation("pay", 40_000 * RUPEE, depth=1, at=late))


def test_empty_context_refused(root):
    with pytest.raises(ValueError, match="non-empty context"):
        token.attenuate(
            root, to="x", grant=Grant(tools=("pay",), budget_paise=1), context="   "
        )


def test_forged_root_key_refused(branches):
    payer, _ = branches
    stranger = _kp().public_key
    with pytest.raises(token.Forged):
        token.deserialize(token.serialize(payer), stranger)


def test_roundtrip_through_base64(branches, principal):
    payer, _ = branches
    pub = token.root_key_from(principal.private_key)
    restored = token.deserialize(token.serialize(payer), pub)
    token.verify(restored, Operation("pay", 359_600 * RUPEE, depth=1, at=NOW))


def test_siblings_share_one_mandate(root, branches):
    """Both branches draw on the same budget pool.

    Without this the cap is meaningless - each branch would get its own fresh
    allowance and two agents could spend the limit twice over.
    """
    payer, shopper = branches
    assert token.mandate_id(payer) == token.mandate_id(shopper) == token.mandate_id(root)


# --- ledger: the contribution ----------------------------------------------


def test_cumulative_spend_is_enforced():
    """The headline.

    Two payments of 899 against a 1500 cap. Each is individually valid, and AIP
    as specified permits both. The ledger is what refuses the second.
    """
    ledger = MemoryLedger()
    ledger.open("m1", 600_000 * RUPEE)

    first = ledger.reserve("m1", 359_600 * RUPEE, "cart-a")
    ledger.commit(first.id)

    with pytest.raises(InsufficientBudget) as exc:
        ledger.reserve("m1", 359_600 * RUPEE, "cart-b")

    assert exc.value.available == 240_400 * RUPEE
    assert ledger.state("m1").committed_paise == 359_600 * RUPEE


def test_reservation_blocks_concurrent_overspend():
    """The race the two-phase design exists to close.

    Nothing has been charged yet, but the held amount is already unavailable.
    """
    ledger = MemoryLedger()
    ledger.open("m1", 400_000 * RUPEE)
    ledger.reserve("m1", 240_000 * RUPEE, "cart-a")

    with pytest.raises(InsufficientBudget):
        ledger.reserve("m1", 240_000 * RUPEE, "cart-b")


def test_release_returns_budget_and_allows_retry():
    ledger = MemoryLedger()
    ledger.open("m1", 400_000 * RUPEE)
    held = ledger.reserve("m1", 240_000 * RUPEE, "cart-a")
    ledger.release(held.id)

    assert ledger.state("m1").available_paise == 400_000 * RUPEE
    ledger.reserve("m1", 240_000 * RUPEE, "cart-a")  # same key retryable after failure


def test_reserve_is_idempotent_on_key():
    """An in-TTL replay gets the original reservation, not a second charge."""
    ledger = MemoryLedger()
    ledger.open("m1", 400_000 * RUPEE)
    a = ledger.reserve("m1", 160_000 * RUPEE, "same-cart")
    b = ledger.reserve("m1", 160_000 * RUPEE, "same-cart")

    assert a.id == b.id
    assert ledger.state("m1").reserved_paise == 160_000 * RUPEE


def test_unknown_mandate_fails_closed():
    with pytest.raises(UnknownMandate):
        MemoryLedger().reserve("never-opened", 1, "k")


def test_cap_cannot_be_raised_by_reopening():
    ledger = MemoryLedger()
    ledger.open("m1", 200_000 * RUPEE)
    with pytest.raises(Exception, match="refusing to reopen"):
        ledger.open("m1", 2_000_000 * RUPEE)


# --- idempotency -----------------------------------------------------------


def test_key_is_order_independent():
    a = derive_key(mandate_id="m", tool="pay", payload={"x": 1, "y": 2})
    b = derive_key(mandate_id="m", tool="pay", payload={"y": 2, "x": 1})
    assert a == b


def test_key_changes_with_content():
    a = derive_key(mandate_id="m", tool="pay", payload={"amount": 100})
    b = derive_key(mandate_id="m", tool="pay", payload={"amount": 101})
    assert a != b


def test_key_is_scoped_to_mandate():
    a = derive_key(mandate_id="m1", tool="pay", payload={"amount": 100})
    b = derive_key(mandate_id="m2", tool="pay", payload={"amount": 100})
    assert a != b


def test_replay_returns_original_result():
    store = ReplayStore()
    store.record("k", {"order": "order_1"})
    assert store.record("k", {"order": "order_2"}).result == {"order": "order_1"}


def test_forget_allows_retry_after_failure():
    store = ReplayStore()
    store.record("k", {"order": "order_1"})
    store.forget("k")
    assert store.get("k") is None


def test_canonical_is_stable():
    assert canonical({"b": 1, "a": 2}) == canonical({"a": 2, "b": 1})


# --- audit -----------------------------------------------------------------


def test_chain_verifies():
    log = AuditLog()
    log.append(mandate_id="m", actor="payer", tool="pay",
               decision=Decision.ALLOWED, reason="within budget", amount_paise=100)
    log.append(mandate_id="m", actor="payer", tool="pay",
               decision=Decision.DENIED, reason="cumulative budget exhausted")
    log.verify()
    assert len(log) == 2


def test_denials_are_recorded():
    """A log of successes only cannot show that an attack was stopped."""
    log = AuditLog()
    log.append(mandate_id="m", actor="shopper", tool="payout",
               decision=Decision.DENIED, reason="scope never granted")
    assert log.entries()[0].decision is Decision.DENIED


def test_tampering_breaks_the_chain():
    log = AuditLog()
    log.append(mandate_id="m", actor="a", tool="pay",
               decision=Decision.ALLOWED, reason="ok", amount_paise=100)
    log.append(mandate_id="m", actor="a", tool="pay",
               decision=Decision.ALLOWED, reason="ok", amount_paise=200)

    victim = log._entries[0]
    log._entries[0] = type(victim)(**{**victim.__dict__, "amount_paise": 999999})

    with pytest.raises(AuditTampered):
        log.verify()
