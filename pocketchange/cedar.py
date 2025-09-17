"""Cedar: the rules this system chose, as opposed to the ones it proved.

The money path has always made a distinction in its comments that it could not
make in its code. Some of what `/pay` enforces is a cryptographic guarantee -
scope, attenuation, expiry, depth - and some of it is a rule someone decided to
apply at the enforcement point. Both were `if` statements, indistinguishable to
a reader, scattered through two thousand lines.

Cedar makes the second kind legible. `policies/pay.cedar` holds them, a reviewer
can read the whole authorisation policy in under a minute, and changing what the
system permits no longer means editing the function that moves the money.

Two things this deliberately does NOT do.

It does not re-decide anything the token already settled. A policy engine asked
to confirm a signature is a second opinion on a fact, and the temptation to move
scope checks in here should be resisted: they are enforced by cryptography and a
Cedar rule that agreed with them would be decoration that could drift.

It does not check whether a request is well-formed. The empty-context refusal
stays in Python, because "did the caller state a reason" is a question about the
shape of the request, not about whether this principal may act on this resource.
Cedar answers the second question only.

Performance. The enforcement path is timed and quoted in this project's own
results, so the policy set is parsed once at import into a `PolicySet` handle and
reused - parsing is the dominant cost of an authorisation call, and doing it per
payment would have put a millisecond into the number the README reports.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

POLICY_DIR = Path(__file__).resolve().parent.parent / "policies"


@dataclass(frozen=True)
class Refusal:
    """A denial, carrying the message and status the gateway should answer with.

    The message text is part of the system's contract - the console prints it and
    an agent reads it to decide whether to adapt or stop - so it lives beside the
    rule rather than being reconstructed at the call site.
    """

    policy_id: str
    reason: str
    status: int


# Keyed by the @id annotation in the policy file. A rule that denies without an
# entry here is a bug, and `refuse()` says so rather than inventing a message.
REFUSALS = {
    "broker-may-not-spend": Refusal(
        "broker-may-not-spend", "a broker may delegate, not spend", 403),
    "payout-is-not-enabled": Refusal(
        "payout-is-not-enabled", "payout is not enabled on this deployment", 501),
}


def policy_text() -> str:
    """Every .cedar file, concatenated in a stable order."""
    return "\n".join(
        path.read_text() for path in sorted(POLICY_DIR.glob("*.cedar"))
    )


def _annotation_order(text: str) -> list[str]:
    """The @id annotations, in source order.

    Cedar identifies policies positionally - policy0, policy1 - and the binding
    reports those ids in its diagnostics rather than the annotations. Source
    order is what maps one to the other. This is only ever used to turn a
    decision into a message, and every rule has a test that asserts the whole
    round trip, so a reordering that broke the mapping would fail loudly rather
    than quietly refuse with the wrong reason.
    """
    return re.findall(r'@id\("([^"]+)"\)', text)


@lru_cache(maxsize=1)
def _compiled():
    from cedarpy import PolicySet

    text = policy_text()
    return PolicySet.from_str(text), _annotation_order(text)


def available() -> bool:
    """Whether Cedar can be consulted in this process."""
    try:
        import cedarpy  # noqa: F401
    except ImportError:
        return False
    return POLICY_DIR.is_dir() and bool(policy_text().strip())


def payout_enabled() -> bool:
    """A deployment switch, off unless someone turns it on deliberately."""
    return os.environ.get("POCKETCHANGE_PAYOUT_ENABLED", "").strip().lower() in (
        "1", "true", "yes")


def decide(*, action: str, role: str, mandate_id: str) -> Refusal | None:
    """Ask the policy whether this principal may do this. None means yes.

    Fails CLOSED for `pay` and `payout` if Cedar is unavailable, which is the
    opposite of how the monitor fails and deliberately so. The monitor is a
    second opinion and blocking every payment because it is unreachable would
    make the system less useful than having no monitor. This is not a second
    opinion - it is the only thing standing between a broker and the money -
    so an unanswerable policy question refuses.
    """
    if not available():
        return Refusal(
            "policy-unavailable",
            "authorisation policy could not be evaluated",
            503,
        )

    from cedarpy import Decision, is_authorized

    policies, order = _compiled()
    request = {
        "principal": f'Agent::"{_escape(role)}"',
        "action": f'Action::"{_escape(action)}"',
        "resource": f'Mandate::"{_escape(mandate_id)}"',
        "context": {"payout_enabled": payout_enabled()},
    }
    entities = [
        {"uid": {"type": "Agent", "id": role},
         "attrs": {"role": role}, "parents": []},
        {"uid": {"type": "Mandate", "id": mandate_id}, "attrs": {}, "parents": []},
    ]

    result = is_authorized(request, policies, entities)
    if result.decision is Decision.Allow:
        return None

    for policy_id in result.diagnostics.reasons:
        named = _named(policy_id, order)
        if named in REFUSALS:
            return REFUSALS[named]

    # Denied by a rule with no message. Refuse rather than allow, and name the
    # policy so the gap is findable.
    return Refusal(
        "unnamed-denial",
        f"refused by authorisation policy ({','.join(result.diagnostics.reasons)})",
        403,
    )


def _named(policy_id: str, order: list[str]) -> str:
    """`policy3` -> the third @id annotation."""
    match = re.fullmatch(r"policy(\d+)", policy_id)
    if not match:
        return policy_id
    index = int(match.group(1))
    return order[index] if index < len(order) else policy_id


def _escape(value: str) -> str:
    """Cedar entity ids are quoted strings; a quote or backslash must not end one.

    Every value reaching here is already constrained - a role is one of three
    literals and a mandate id is a hex digest - but this is the money path, and
    a sanitiser that is only correct because of what its callers happen to pass
    is one refactor away from not being.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')
