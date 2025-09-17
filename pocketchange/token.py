"""Capability tokens: mint, attenuate, verify.

The spike (spike/biscuit_chain.py) proved this works. This is the same idea made
reusable, plus the one thing the spike did not need: a stable identifier for the
root authority, so a ledger can track spend across every token descended from it.

Three properties everything else depends on:

  * A token is an append-only stack of blocks, each cryptographically chained.
  * Verification requires every check in every block to pass.
  * Therefore authority can be narrowed by any holder, and widened by none.
"""

from __future__ import annotations

from datetime import datetime, timezone

from biscuit_auth import (
    AuthorizationError,
    AuthorizerBuilder,
    KeyPair,
    Biscuit,
    BiscuitBuilder,
    BiscuitValidationError,
    BlockBuilder,
    PrivateKey,
    PublicKey,
)

from .identity import Identity
from .policy import AUTHORITY, AUTHORIZER, DELEGATION, DELEGATION_WITH_EXPIRY, Grant, Operation


class TokenError(Exception):
    """Base for everything this module refuses."""


class Forged(TokenError):
    """The token is not authentic - wrong root key, or tampered with.

    Kept distinct from Denied on purpose. A forgery is an attack; a denial is
    the system working. Collapsing them into one error would make the audit log
    unable to tell those apart.
    """


class Denied(TokenError):
    """The token is genuine, but does not authorise this operation."""


def mint(
    identity: Identity,
    *,
    budget_paise: int,
    max_depth: int = 3,
    expires: datetime,
) -> Biscuit:
    """Create the root authority - the token the human principal signs.

    The checks written here are the outer limits. Nothing appended later can
    loosen them, so this is the only place a ceiling can be set.
    """
    if expires.tzinfo is None:
        raise ValueError("expires must be timezone-aware; use timezone.utc")
    if max_depth < 1:
        raise ValueError(f"max_depth must be at least 1, got {max_depth}")

    builder = BiscuitBuilder(
        AUTHORITY,
        {
            "identity": identity.aip_id,
            "max_depth": max_depth,
            "budget": budget_paise,
            "expires": expires,
        },
    )
    return builder.build(identity.private_key)


def attenuate(token: Biscuit, *, to: Identity | str, grant: Grant, context: str) -> Biscuit:
    """Hand narrower authority onward. Returns a new token; the original stands.

    `context` is AIP's mandatory non-empty field - the delegator states *why*.
    It exists for audit integrity, and it later becomes the evidence the
    semantic monitor reads when deciding whether an action matches its stated
    reason. A blank one would make both worthless, so it is refused here rather
    than tolerated and ignored.
    """
    if not context.strip():
        raise ValueError("AIP requires a non-empty context on every delegation block")

    delegate_id = to.aip_id if isinstance(to, Identity) else to
    params: dict[str, object] = {
        "delegate": delegate_id,
        "context": context.strip(),
        "tools": list(grant.tools),
        "budget": grant.budget_paise,
    }
    source = DELEGATION
    if grant.expires is not None:
        source = DELEGATION_WITH_EXPIRY
        params["expires"] = grant.expires

    return token.append(BlockBuilder(source, params))


def verify(token: Biscuit, op: Operation) -> None:
    """Check one operation against the whole chain. Returns quietly, or raises.

    No boolean is returned, deliberately. A caller cannot forget to check a
    return value that does not exist, so a denial cannot be silently treated as
    permission - it stops execution instead.
    """
    at = op.at or datetime.now(timezone.utc)
    authorizer = AuthorizerBuilder(
        AUTHORIZER,
        {"tool": op.tool, "amount": op.amount_paise, "depth": op.depth, "at": at},
    ).build(token)
    try:
        authorizer.authorize()
    except AuthorizationError as exc:
        raise Denied(f"{op.tool} for {op.amount_paise}p at depth {op.depth}: {exc}") from exc


def serialize(token: Biscuit) -> str:
    """To the base64 string that travels in an HTTP header."""
    return token.to_base64()


def deserialize(raw: str, root_public_key: PublicKey) -> Biscuit:
    """Back from base64, verifying the signature chain against the root key.

    This is where forgery is caught. Parsing and signature verification happen
    together - there is no way to get a usable token object without the
    signatures having already checked out.
    """
    try:
        return Biscuit.from_base64(raw, root_public_key)
    except BiscuitValidationError as exc:
        raise Forged(f"token does not verify against this root key: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - malformed input arrives as several types
        raise Forged(f"malformed token: {exc}") from exc


def mandate_id(token: Biscuit) -> str:
    """A stable identifier for the root authority this token descends from.

    Biscuit gives every block a revocation id derived from its signature. The
    first one belongs to the authority block, and every token appended from that
    root keeps it unchanged - so the payer's token and the shopper's token, being
    siblings, produce the same mandate id.

    That is exactly the grouping a cumulative spend ledger needs: one budget pool
    per human authorisation, shared across every agent acting under it. Without
    it each branch would get its own fresh allowance and the cap would mean
    nothing.
    """
    ids = token.revocation_ids  # a property, not a method
    if not ids:
        raise TokenError("token has no blocks")
    return ids[0]


def depth_of(token: Biscuit) -> int:
    """How many delegations deep this token is. Derived, never claimed.

    Block 0 is the authority; every attenuation appends exactly one more. Blocks
    cannot be removed without breaking the signature chain, so this number is as
    trustworthy as the token itself.

    That distinction is the whole point. The depth check in AUTHORITY reads a
    `depth($d)` fact supplied by the authorizer, and until now the gateway took
    that number from the request body - so the agent being depth-limited was the
    one filling in its own depth. At two layers the budget checks masked it. In a
    funnel that recurses eight deep, depth is a primary bound, and a bound the
    caller supplies is not a bound at all.
    """
    return token.block_count() - 1


BROKER_MARKER = "/broker"


def terminal_delegate(token: Biscuit) -> str | None:
    """Who the most recent block delegated to, if anyone.

    Read from the block's own Datalog source, so it reflects what was actually
    signed rather than what a caller claims.
    """
    count = token.block_count()
    if count < 2:
        return None
    source = token.block_source(count - 1) or ""
    for line in source.splitlines():
        line = line.strip()
        if line.startswith("delegate("):
            return line[len('delegate("'):].rstrip(');').rstrip('"')
    return None


def is_broker(token: Biscuit) -> bool:
    """Is this token held by a broker - something that may grant but not spend?

    IMPORTANT, and worth being precise about because it is the one place this
    system leans on policy rather than cryptography.

    Attenuation is monotonic: every authority a child holds, its parent held. So
    a token that can confer `pay` can also exercise `pay`. There is no way to
    express "may grant X without being able to do X" in the chain itself - proved
    directly: a broker scoped to ["delegate"] produces children that cannot pay,
    and one scoped to ["delegate","pay"] can pay itself.

    The bounded sub-mandates ARE cryptographic - a sub-payer capped at one
    seller's subtotal cannot exceed it no matter what. The broker/payer
    separation is NOT; it is enforced here, at the gateway, by identity. A
    compromised gateway loses this property. A compromised agent does not.
    """
    delegate = terminal_delegate(token)
    return bool(delegate and BROKER_MARKER in delegate)


def root_key_from(private_key: PrivateKey) -> PublicKey:
    """The public half, for handing to verifiers."""
    return KeyPair.from_private_key(private_key).public_key
