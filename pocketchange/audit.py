"""Append-only hash-linked audit chain and completion blocks.

Razorpay's brief asks to "show the audit trail". This is it.

Every decision is recorded - allowed and refused alike, each with its reason.
A log that only records successes cannot demonstrate that an attack was stopped,
and demonstrating exactly that is the whole demo.

Entries are hash-linked: each carries the hash of the one before it, so altering
or removing any entry breaks every hash after it. That does not prevent tampering
by whoever holds the log, but it makes tampering *evident*, which is the property
an auditor actually needs.

The technique is the same one a split tally stick used - notches cut across two
halves that had to match when rejoined. Seven hundred years older than SHA-256,
identical idea.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .idempotency import canonical

GENESIS = "0" * 64


class Decision(str, Enum):
    """What the gateway concluded.

    ESCALATED is separate from DENIED on purpose: the monitor asking a human is
    not the same event as a rule refusing, and collapsing them would hide how
    often the second layer actually fires.
    """

    ALLOWED = "allowed"
    DENIED = "denied"
    ESCALATED = "escalated"


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    at: datetime
    mandate_id: str
    actor: str
    tool: str
    amount_paise: int
    decision: Decision
    reason: str
    context: str
    prev_hash: str
    detail: dict[str, Any] = field(default_factory=dict)
    entry_hash: str = ""

    def compute_hash(self) -> str:
        """Hash of everything except the hash field itself.

        Order-independent, because canonical() sorts keys - so the digest
        depends on the content and not on how the dict happened to be built.
        """
        body = asdict(self)
        body.pop("entry_hash", None)
        body["decision"] = self.decision.value
        body["at"] = self.at.isoformat()
        return hashlib.sha256(canonical(body).encode()).hexdigest()


class AuditLog:
    """An append-only chain. Nothing here can update or delete."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[AuditEntry] = []

    def append(
        self,
        *,
        mandate_id: str,
        actor: str,
        tool: str,
        decision: Decision,
        reason: str,
        context: str = "",
        amount_paise: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Record one decision and link it to the chain."""
        with self._lock:
            prev = self._entries[-1].entry_hash if self._entries else GENESIS
            draft = AuditEntry(
                seq=len(self._entries),
                at=datetime.now(timezone.utc),
                mandate_id=mandate_id,
                actor=actor,
                tool=tool,
                amount_paise=amount_paise,
                decision=decision,
                reason=reason,
                context=context,
                prev_hash=prev,
                detail=detail or {},
            )
            entry = AuditEntry(**{**draft.__dict__, "entry_hash": draft.compute_hash()})
            self._entries.append(entry)
            return entry

    def verify(self) -> None:
        """Walk the chain. Raises on the first entry that does not hold.

        Checks both links: that each entry's own hash still matches its content,
        and that it points at its predecessor. The first catches edited entries,
        the second catches removed ones.
        """
        expected_prev = GENESIS
        for i, entry in enumerate(self._entries):
            if entry.seq != i:
                raise AuditTampered(f"entry {i} claims seq {entry.seq}")
            if entry.prev_hash != expected_prev:
                raise AuditTampered(f"entry {i} does not follow its predecessor")
            if entry.entry_hash != entry.compute_hash():
                raise AuditTampered(f"entry {i} content does not match its hash")
            expected_prev = entry.entry_hash

    def entries(self, mandate_id: str | None = None) -> list[AuditEntry]:
        with self._lock:
            if mandate_id is None:
                return list(self._entries)
            return [e for e in self._entries if e.mandate_id == mandate_id]

    @property
    def head(self) -> str:
        """Current tip. Publishing this pins the whole history."""
        with self._lock:
            return self._entries[-1].entry_hash if self._entries else GENESIS

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class AuditTampered(Exception):
    """The chain does not verify. Something was edited or removed."""
