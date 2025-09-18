"""Payments suspended pending a human decision.

Before this, an ESCALATE verdict released the reservation and returned 409. That
is a refusal wearing a nicer word: there was no way for a person to say yes, so
"escalate to a human" described something the system could not do.

The shape is RabbitHole's `interrupt_before` plus a checkpointer, expressed over
HTTP: the run stops, its state is held, a person decides, and it resumes from
where it stopped.

The reservation is HELD while an approval is pending, which is the whole point.
Releasing it would let other spending consume the budget the pending payment is
waiting on, so a human saying yes ten minutes later could fail for reasons that
had nothing to do with their decision.

Held budget must also not leak. Every pending approval has a deadline, and an
expired one is denied and released rather than waiting forever.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

DEFAULT_TTL = timedelta(minutes=15)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class ApprovalError(Exception):
    """Base for everything this module refuses."""


class UnknownApproval(ApprovalError):
    """No approval under that id."""


class AlreadyResolved(ApprovalError):
    """Someone already decided this one. Decisions are not revisited."""


@dataclass
class PendingApproval:
    """Something waiting on a person.

    Two kinds, sharing one queue, one endpoint and one CLI:

      payment  a purchase paused mid-flight; the reservation is held until
               someone decides
      policy   a standing instruction awaiting authorisation before it may run
               unattended

    They are here together because a person answering their approvals queue
    should not need to know which mechanism produced the question - and because
    a second approval path would be a second place to get suspend-and-resume
    subtly wrong.
    """

    id: str
    mandate_id: str
    amount_paise: int
    cart: dict[str, Any]
    context: str
    reason: str                     # why this needs a person
    created_at: datetime
    expires_at: datetime
    kind: str = "payment"           # payment | policy
    # Carried through the pause so a payment resumed by a human records the same
    # dealing as one that never stopped. Without it an escalated purchase would
    # vanish from the counterparty record precisely when a person looked at it.
    #
    # Placed after the non-default fields, because a defaulted field before one
    # without a default is a TypeError at import - the same ordering trap this
    # dataclass fell into once already.
    counterparty: str = ""
    reservation_id: str = ""        # payment only
    idempotency_key: str = ""       # payment only
    subject_id: str = ""            # policy only: the standing order id
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_at: datetime | None = None
    decided_by: str = ""
    note: str = ""

    @property
    def is_open(self) -> bool:
        return self.status is ApprovalStatus.PENDING

    def summary(self) -> dict[str, Any]:
        return {
            "approval_id": self.id,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "mandate_id": self.mandate_id,
            "amount_paise": self.amount_paise,
            "cart": self.cart,
            "reason": self.reason,
            "agent_claim": self.context,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "note": self.note,
        }


class ApprovalStore:
    """In-process, and shaped like ledger.Ledger so DynamoDB can replace it."""

    def __init__(self, ttl: timedelta = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._items: dict[str, PendingApproval] = {}

    def open(
        self, *, mandate_id: str, amount_paise: int, cart: dict[str, Any],
        context: str, reason: str, kind: str = "payment",
        reservation_id: str = "", idempotency_key: str = "", subject_id: str = "",
        counterparty: str = "", ttl: timedelta | None = None,
    ) -> PendingApproval:
        now = datetime.now(timezone.utc)
        approval = PendingApproval(
            id=f"ap_{uuid.uuid4().hex[:10]}",
            mandate_id=mandate_id,
            kind=kind,
            reservation_id=reservation_id,
            idempotency_key=idempotency_key,
            subject_id=subject_id,
            amount_paise=amount_paise,
            cart=dict(cart),
            context=context,
            reason=reason,
            counterparty=counterparty,
            created_at=now,
            # A policy can wait longer than a held payment: nothing is locked up
            # while a person thinks about it.
            expires_at=now + (ttl or (self._ttl * 4 if kind == "policy" else self._ttl)),
        )
        with self._lock:
            self._items[approval.id] = approval
        return approval

    def get(self, approval_id: str) -> PendingApproval:
        with self._lock:
            if approval_id not in self._items:
                raise UnknownApproval(f"no approval {approval_id}")
            return self._items[approval_id]

    def resolve(
        self, approval_id: str, *, approved: bool, by: str = "human", note: str = ""
    ) -> PendingApproval:
        """Record a decision. Refuses to revisit one already made."""
        with self._lock:
            approval = self._items.get(approval_id)
            if approval is None:
                raise UnknownApproval(f"no approval {approval_id}")
            if not approval.is_open:
                raise AlreadyResolved(
                    f"{approval_id} was already {approval.status.value}"
                )
            approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
            approval.decided_at = datetime.now(timezone.utc)
            approval.decided_by = by
            approval.note = note
            return approval

    def pending(self) -> list[PendingApproval]:
        with self._lock:
            return [a for a in self._items.values() if a.is_open]

    def expired(self) -> list[PendingApproval]:
        """Mark and return approvals nobody answered in time.

        The caller releases their reservations - this store does not know about
        the ledger, and should not.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            stale = [a for a in self._items.values() if a.is_open and a.expires_at <= now]
            for approval in stale:
                approval.status = ApprovalStatus.EXPIRED
                approval.decided_at = now
                approval.decided_by = "timeout"
                approval.note = "nobody answered before the deadline"
            return stale

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
