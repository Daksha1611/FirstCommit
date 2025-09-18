"""Settlement on a chain, as one more PaymentRail.

The whole point of putting `PaymentRail` behind a Protocol is that the layer
above it - tokens, bounds, the funnel, the monitor, the audit - does not care
where money ends up. Razorpay and the chain are peers here, and neither one gets
to influence who is allowed to spend. Authority is decided before either is
reached.

WHAT THIS DOES AND DOES NOT DO

`PaymentRail.create_order` creates an *intent to pay*, not a transfer. Razorpay
Orders work that way and so does this: it builds an unsigned transaction and
returns it. It holds no private key, signs nothing, and opens no socket.

That is not a shortcut taken for the demo. The gateway's own capability report
says `payments_are_real: false`, and a rail that quietly broadcast a real
transaction would make that line false while every test still passed. If this
ever settles for real, it should be because somebody deliberately wired a signer
in, not because a rail was swapped underneath a system that still claims it
moves no money.

TESTNET ONLY, THE SAME WAY LIVE KEYS ARE REFUSED

`razorpay_client.from_env` refuses `rzp_live_` outright rather than trusting an
environment variable to be right. A mainnet chain id is the same mistake wearing
different clothes, so it is refused in the same place and for the same reason.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from .razorpay_client import Order, PaymentError

# Chains this is allowed to address. Test networks, deliberately.
#
# Mainnet ids are listed so the refusal can name what it caught - an id that is
# merely unknown is a typo, while an id that is Ethereum mainnet is somebody
# about to move real money and deserves a different sentence.
TESTNETS: dict[int, str] = {
    11155111: "ethereum-sepolia",
    84532: "base-sepolia",
    421614: "arbitrum-sepolia",
    80002: "polygon-amoy",
    1301: "unichain-sepolia",
}

MAINNETS: dict[int, str] = {
    1: "ethereum",
    8453: "base",
    42161: "arbitrum",
    137: "polygon",
}

DEFAULT_CHAIN_ID = 84532          # Base Sepolia
DEFAULT_DECIMALS = 6              # a USDC-shaped test stablecoin


class ChainRail:
    """Builds an unsigned settlement intent for an EVM chain.

    Deliberately shaped like `FakeRail` rather than like `RazorpayClient`: it
    holds no credentials, performs no I/O, and is therefore safe to exercise in
    the test suite without a network or a funded account.
    """

    def __init__(
        self,
        *,
        chain_id: int = DEFAULT_CHAIN_ID,
        token_address: str = "",
        payer: str = "",
        decimals: int = DEFAULT_DECIMALS,
        paise_per_unit: int = 100,
    ) -> None:
        if chain_id in MAINNETS:
            raise ValueError(
                f"chain {chain_id} is {MAINNETS[chain_id]} mainnet. This rail "
                "settles on test networks only, for the same reason the "
                "Razorpay client refuses rzp_live_ keys."
            )
        if chain_id not in TESTNETS:
            raise ValueError(
                f"unknown chain id {chain_id}; known test networks are "
                + ", ".join(f"{k} ({v})" for k, v in TESTNETS.items())
            )
        if paise_per_unit <= 0:
            raise ValueError("paise_per_unit must be positive")

        self.chain_id = chain_id
        self.network = TESTNETS[chain_id]
        self.token_address = token_address
        self.payer = payer
        self.decimals = decimals
        # There is no oracle here and there should not be one: a made-up
        # exchange rate that looks authoritative is worse than one that is
        # obviously a constant. It is recorded on every intent so a reader can
        # see what the number meant.
        self.paise_per_unit = paise_per_unit

    @property
    def is_test_mode(self) -> bool:
        """Always. The constructor refuses every chain for which it is not."""
        return True

    def amount_in_base_units(self, amount_paise: int) -> int:
        """Paise to the token's smallest unit, rounding down.

        Rounds DOWN so a conversion can never produce a transfer larger than
        the amount the ledger reserved. The remainder is lost to rounding, which
        is the correct direction for it to be lost in.
        """
        units = amount_paise / self.paise_per_unit
        return int(units * (10 ** self.decimals))

    def create_order(
        self, *, amount_paise: int, receipt: str, notes: dict[str, str]
    ) -> Order:
        """Build the unsigned transfer. Signs nothing, sends nothing."""
        if amount_paise <= 0:
            raise PaymentError(f"amount must be positive, got {amount_paise}")

        recipient = notes.get("counterparty_address", "")
        value = self.amount_in_base_units(amount_paise)

        # The reference is derived from the receipt, which the gateway sets to
        # the idempotency key - so the same purchase produces the same intent
        # every time, and a replay is recognisable on-chain rather than only in
        # our own memory.
        reference = "0x" + hashlib.sha256(receipt.encode()).hexdigest()

        intent = {
            "chain_id": self.chain_id,
            "network": self.network,
            "from": self.payer,
            "token": self.token_address,
            "to": recipient,
            "value": str(value),
            "decimals": self.decimals,
            "reference": reference,
            "signed": False,
            "broadcast": False,
            # Restated on every intent rather than documented once, because this
            # is the field that stops a reader assuming money moved.
            "note": "unsigned settlement intent; this rail holds no key",
            "paise": amount_paise,
            "paise_per_unit": self.paise_per_unit,
            "notes": notes,
        }

        return Order(
            id=f"intent_{reference[2:18]}",
            amount_paise=amount_paise,
            currency="TEST",
            receipt=receipt[:40],
            status="created",
            raw=intent,
        )


def from_env() -> ChainRail | None:
    """A chain rail if one is configured, otherwise None.

    Returns None rather than raising so `gateway.State` can keep Razorpay as the
    default and treat the chain as opt-in. A deployment that has never heard of
    this module behaves exactly as it did.
    """
    if not os.getenv("POCKETCHANGE_CHAIN_ID"):
        return None
    return ChainRail(
        chain_id=int(os.environ["POCKETCHANGE_CHAIN_ID"]),
        token_address=os.getenv("POCKETCHANGE_CHAIN_TOKEN", ""),
        payer=os.getenv("POCKETCHANGE_CHAIN_PAYER", ""),
    )
