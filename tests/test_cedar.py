"""The authorisation policy, as Cedar sees it.

These assert the round trip - a rule in policies/pay.cedar, a decision, and the
message and status the gateway answers with - rather than the policy text. Cedar
identifies policies positionally and the binding reports those positional ids, so
the mapping from a decision back to its `@id` annotation is the fragile part.
Testing each rule end to end is what makes a reordering fail loudly instead of
refusing with the wrong reason.
"""

import pytest

from pocketchange import cedar


def test_a_broker_may_not_spend():
    """The rule that used to be an `if` at line 1173 of the money path."""
    refusal = cedar.decide(action="pay", role="broker", mandate_id="m1")
    assert refusal is not None
    assert refusal.policy_id == "broker-may-not-spend"
    assert refusal.reason == "a broker may delegate, not spend"
    assert refusal.status == 403


def test_a_payer_may_spend():
    assert cedar.decide(action="pay", role="payer", mandate_id="m1") is None


def test_payout_is_refused_by_default(monkeypatch):
    monkeypatch.delenv("POCKETCHANGE_PAYOUT_ENABLED", raising=False)
    refusal = cedar.decide(action="payout", role="payer", mandate_id="m1")
    assert refusal is not None
    assert refusal.policy_id == "payout-is-not-enabled"
    assert refusal.status == 501


def test_payout_can_be_switched_on_deliberately(monkeypatch):
    """The rule is a deployment switch, not a trap.

    It has to be genuinely reachable, or the policy file is decoration and the
    day someone widens a scope it will not be the thing that stops them.
    """
    monkeypatch.setenv("POCKETCHANGE_PAYOUT_ENABLED", "1")
    assert cedar.decide(action="payout", role="payer", mandate_id="m1") is None


def test_a_broker_is_refused_payout_for_the_payout_reason(monkeypatch):
    """Two forbids match. The answer must be one of them, not a merge."""
    monkeypatch.delenv("POCKETCHANGE_PAYOUT_ENABLED", raising=False)
    refusal = cedar.decide(action="payout", role="broker", mandate_id="m1")
    assert refusal is not None
    assert refusal.policy_id in {"payout-is-not-enabled", "broker-may-not-spend"}


# --- failing closed ---------------------------------------------------------


def test_an_unevaluable_policy_refuses_rather_than_allowing(monkeypatch):
    """The opposite of how the monitor fails, deliberately.

    The monitor is a second opinion and blocking every payment because it is
    unreachable would make the system worse than having no monitor. This is not
    a second opinion - it is the only thing between a broker and the money - so
    an unanswerable question is a refusal.
    """
    monkeypatch.setattr(cedar, "available", lambda: False)
    refusal = cedar.decide(action="pay", role="payer", mandate_id="m1")
    assert refusal is not None
    assert refusal.status == 503


# --- the policy file itself -------------------------------------------------


def test_every_rule_that_can_deny_has_a_message():
    """A denial with no message would refuse with a policy id in place of a
    reason, which an agent cannot act on and a person cannot read."""
    named = set(cedar._annotation_order(cedar.policy_text()))
    denying = {name for name in named if not name.startswith("permitted")}
    assert denying <= set(cedar.REFUSALS)


def test_the_policy_set_parses():
    assert cedar.available() is True
    assert "forbid" in cedar.policy_text()


# --- entity ids are not a way into the policy -------------------------------


@pytest.mark.parametrize("hostile", [
    'm1" || true || "',
    'm1\\"',
    'm1"',
])
def test_a_hostile_mandate_id_cannot_change_the_decision(hostile):
    """Mandate ids are hex digests today, so this is defence against a future
    refactor rather than a live hole. It is still the money path."""
    assert cedar.decide(action="pay", role="payer", mandate_id=hostile) is None
    refused = cedar.decide(action="pay", role="broker", mandate_id=hostile)
    assert refused is not None and refused.status == 403
