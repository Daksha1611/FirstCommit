"""Razorpay test-mode client. Reachable only from the gateway.

Deliberately thin. This module is the one place in the system that holds a
payment credential, and the agent must never be able to import it - if it can
call this directly, every check upstream is decoration.

On idempotency: Razorpay offers X-Payout-Idempotency, X-Transfer-Idempotency and
X-Refund-Idempotency, but publishes no idempotency header for Orders creation,
which is the endpoint a checkout uses. We send `receipt` as our fingerprint so
duplicates are at least *visible* in their dashboard, and rely on our own ledger
for the actual guarantee. See idempotency.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

API_ROOT = "https://api.razorpay.com/v1"


class PaymentError(Exception):
    """Razorpay refused, or could not be reached."""


@dataclass(frozen=True)
class Order:
    id: str
    amount_paise: int
    currency: str
    receipt: str
    status: str
    raw: dict[str, Any]


class PaymentRail(Protocol):
    """What the gateway needs. Implemented for real and for tests."""

    def create_order(self, *, amount_paise: int, receipt: str, notes: dict[str, str]) -> Order: ...


class RazorpayClient:
    """Talks to Razorpay over HTTP Basic auth."""

    def __init__(self, key_id: str, key_secret: str, *, timeout: float = 10.0) -> None:
        if not key_id or not key_secret:
            raise ValueError("Razorpay credentials are required")
        self._key_id = key_id
        self._auth = (key_id, key_secret)
        self._timeout = timeout

    @property
    def is_test_mode(self) -> bool:
        return self._key_id.startswith("rzp_test_")

    def create_order(self, *, amount_paise: int, receipt: str, notes: dict[str, str]) -> Order:
        """Create an order. Amount is paise, as Razorpay expects.

        Razorpay caps `receipt` at 40 characters, so our 64-hex idempotency
        fingerprint has to be truncated. That is fine - the receipt is a
        human-facing cross-reference, not the guarantee. The guarantee is the
        ledger, which keeps the full key.
        """
        payload = {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": receipt[:40],
            "notes": notes,
        }
        try:
            response = httpx.post(
                f"{API_ROOT}/orders", json=payload, auth=self._auth, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise PaymentError(f"could not reach Razorpay: {exc}") from exc

        if response.status_code >= 400:
            # Surface Razorpay's own description; their error shapes vary, so
            # fall back to the raw body rather than guessing at a schema.
            try:
                detail = response.json().get("error", {}).get("description", response.text)
            except Exception:  # noqa: BLE001
                detail = response.text
            raise PaymentError(f"Razorpay rejected the order ({response.status_code}): {detail}")

        body = response.json()
        return Order(
            id=body["id"],
            amount_paise=body["amount"],
            currency=body["currency"],
            receipt=body.get("receipt", ""),
            status=body.get("status", "created"),
            raw=body,
        )


class FakeRail:
    """A stand-in that never touches the network.

    Tests must be able to exercise the full check order without creating real
    orders, and without credentials being present at all.
    """

    def __init__(self) -> None:
        self.orders: list[Order] = []

    @property
    def is_test_mode(self) -> bool:
        return True

    def create_order(self, *, amount_paise: int, receipt: str, notes: dict[str, str]) -> Order:
        order = Order(
            id=f"order_fake_{len(self.orders):04d}",
            amount_paise=amount_paise,
            currency="INR",
            receipt=receipt[:40],
            status="created",
            raw={"fake": True, "notes": notes},
        )
        self.orders.append(order)
        return order


def from_env() -> PaymentRail:
    """Real client if credentials are set, otherwise the fake.

    Refuses live keys outright. Nothing in this project should ever move real
    money, and a mistyped environment variable is not a good enough reason to
    find out otherwise.
    """
    key_id = os.getenv("RAZORPAY_KEY_ID", "")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        return FakeRail()
    if key_id.startswith("rzp_live_"):
        raise ValueError(
            "RAZORPAY_KEY_ID is a live key. Pocket Change is test-mode only; "
            "generate test credentials instead."
        )
    return RazorpayClient(key_id, key_secret)
