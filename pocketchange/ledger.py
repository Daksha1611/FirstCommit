"""Cumulative spend ledger and two-phase reservations.

This is the file the project exists for.

AIP's budget check asks "is this amount under the cap?" - a question about one
payment in isolation. It cannot ask "has the cap already been spent?", because a
token is a static document and knows nothing about history. The paper says so
outright: the verifier "does not track cumulative spend... Aggregate spend
enforcement is the runtime's responsibility, not the token's."

So an agent holding a valid 500-rupee mandate can spend 500 rupees, repeatedly,
forever, and every single payment verifies correctly. This module is what makes
that impossible.

Two-phase reservation, not a simple counter, because the gap between "check the
budget" and "charge the card" is where money is lost. Two concurrent payments
can both read a 900-rupee balance, both conclude 899 is affordable, and both
charge. Reserving inside a lock closes that window: the second request sees the
first one's reservation even though nothing has been charged yet.
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol


class LedgerError(Exception):
    """Base for everything this module refuses."""


class InsufficientBudget(LedgerError):
    """The mandate cannot cover this on top of what it has already spent.

    Carries the numbers so the audit trail and the dashboard can show the user
    exactly why, rather than a bare refusal.
    """

    def __init__(self, *, requested: int, available: int, cap: int, committed: int, reserved: int):
        self.requested = requested
        self.available = available
        self.cap = cap
        self.committed = committed
        self.reserved = reserved
        super().__init__(
            f"requested {requested}p but only {available}p available "
            f"(cap {cap}p, committed {committed}p, reserved {reserved}p)"
        )


class UnknownMandate(LedgerError):
    """No mandate opened under this id. Fail closed rather than assume a cap."""


class UnknownReservation(LedgerError):
    """No reservation under this id, or it was already settled."""


@dataclass(frozen=True)
class Reservation:
    """A hold placed on part of a mandate's budget, not yet charged."""

    id: str
    mandate_id: str
    amount_paise: int
    idempotency_key: str
    created_at: datetime
    settled: bool = False


@dataclass(frozen=True)
class LedgerState:
    """What a mandate has left. Everything in integer paise."""

    mandate_id: str
    cap_paise: int
    committed_paise: int
    reserved_paise: int

    @property
    def available_paise(self) -> int:
        return self.cap_paise - self.committed_paise - self.reserved_paise


class Ledger(Protocol):
    """The interface the gateway depends on.

    MemoryLedger implements it for development and tests; DynamoLedger implements
    it against real storage. The gateway never learns which it has, so swapping
    backends changes one line of wiring and no logic.

    tests/test_ledger_backends.py holds both to the same assertions, because
    that claim is only worth making if something checks it.
    """

    def open(self, mandate_id: str, cap_paise: int) -> LedgerState: ...
    def reserve(self, mandate_id: str, amount_paise: int, idempotency_key: str) -> Reservation: ...
    def commit(self, reservation_id: str) -> LedgerState: ...
    def release(self, reservation_id: str) -> LedgerState: ...
    def state(self, mandate_id: str) -> LedgerState: ...


@dataclass
class _Mandate:
    cap_paise: int
    committed_paise: int = 0
    reservations: dict[str, Reservation] = field(default_factory=dict)

    @property
    def reserved_paise(self) -> int:
        return sum(r.amount_paise for r in self.reservations.values() if not r.settled)


class MemoryLedger:
    """In-process ledger. Correct, not durable.

    Every mutation happens under one lock. That is heavy-handed and completely
    right for money: a ledger that is fast and occasionally wrong is worse than
    one that is slow and never is.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mandates: dict[str, _Mandate] = {}
        self._by_reservation: dict[str, str] = {}   # reservation id -> mandate id
        self._by_idem: dict[str, str] = {}          # idempotency key -> reservation id

    def open(self, mandate_id: str, cap_paise: int) -> LedgerState:
        """Register a mandate's ceiling. Safe to call repeatedly.

        Reopening with a different cap is refused. Otherwise a compromised agent
        could simply raise its own limit by asking twice.
        """
        if cap_paise < 0:
            raise ValueError(f"negative cap: {cap_paise}")
        with self._lock:
            existing = self._mandates.get(mandate_id)
            if existing is not None:
                if existing.cap_paise != cap_paise:
                    raise LedgerError(
                        f"mandate {mandate_id[:12]} already open at {existing.cap_paise}p, "
                        f"refusing to reopen at {cap_paise}p"
                    )
            else:
                self._mandates[mandate_id] = _Mandate(cap_paise=cap_paise)
            return self._state(mandate_id)

    def reserve(self, mandate_id: str, amount_paise: int, idempotency_key: str) -> Reservation:
        """Hold budget against the cap, or refuse.

        Idempotent on the key. A repeat of the same request returns the original
        reservation rather than a second one - which is what stops an in-TTL
        replay from being charged twice. The check lives here, in the same lock
        that guards the budget, because splitting them reintroduces the race
        this method exists to close.
        """
        if amount_paise <= 0:
            raise ValueError(f"reservation must be positive, got {amount_paise}")
        if not idempotency_key.strip():
            raise ValueError("idempotency key is required")

        with self._lock:
            prior_id = self._by_idem.get(idempotency_key)
            if prior_id is not None:
                mandate = self._mandates[self._by_reservation[prior_id]]
                return mandate.reservations[prior_id]

            mandate = self._mandates.get(mandate_id)
            if mandate is None:
                raise UnknownMandate(f"no mandate opened for {mandate_id[:12]}")

            available = mandate.cap_paise - mandate.committed_paise - mandate.reserved_paise
            if amount_paise > available:
                raise InsufficientBudget(
                    requested=amount_paise,
                    available=available,
                    cap=mandate.cap_paise,
                    committed=mandate.committed_paise,
                    reserved=mandate.reserved_paise,
                )

            reservation = Reservation(
                id=uuid.uuid4().hex,
                mandate_id=mandate_id,
                amount_paise=amount_paise,
                idempotency_key=idempotency_key,
                created_at=datetime.now(timezone.utc),
            )
            mandate.reservations[reservation.id] = reservation
            self._by_reservation[reservation.id] = mandate_id
            self._by_idem[idempotency_key] = reservation.id
            return reservation

    def commit(self, reservation_id: str) -> LedgerState:
        """The payment succeeded. Turn the hold into spend."""
        with self._lock:
            mandate_id, mandate, reservation = self._locate(reservation_id)
            mandate.committed_paise += reservation.amount_paise
            mandate.reservations[reservation_id] = Reservation(
                **{**reservation.__dict__, "settled": True}
            )
            return self._state(mandate_id)

    def release(self, reservation_id: str) -> LedgerState:
        """The payment failed. Give the budget back.

        The idempotency key is released too. A payment that never happened
        should be retryable - only a *successful* charge must stay pinned.
        """
        with self._lock:
            mandate_id, mandate, reservation = self._locate(reservation_id)
            del mandate.reservations[reservation_id]
            del self._by_reservation[reservation_id]
            self._by_idem.pop(reservation.idempotency_key, None)
            return self._state(mandate_id)

    def state(self, mandate_id: str) -> LedgerState:
        with self._lock:
            return self._state(mandate_id)

    # --- internals, all called with the lock already held ------------------

    def _locate(self, reservation_id: str) -> tuple[str, _Mandate, Reservation]:
        mandate_id = self._by_reservation.get(reservation_id)
        if mandate_id is None:
            raise UnknownReservation(f"no open reservation {reservation_id[:12]}")
        mandate = self._mandates[mandate_id]
        reservation = mandate.reservations[reservation_id]
        if reservation.settled:
            raise UnknownReservation(f"reservation {reservation_id[:12]} already settled")
        return mandate_id, mandate, reservation

    def _state(self, mandate_id: str) -> LedgerState:
        mandate = self._mandates.get(mandate_id)
        if mandate is None:
            raise UnknownMandate(f"no mandate opened for {mandate_id[:12]}")
        return LedgerState(
            mandate_id=mandate_id,
            cap_paise=mandate.cap_paise,
            committed_paise=mandate.committed_paise,
            reserved_paise=mandate.reserved_paise,
        )


# --- DynamoDB -------------------------------------------------------------
#
# Same protocol, durable storage. The gateway never learns which backend it has.
#
# The backend this replaced needed a transaction that read the mandate, did the
# budget arithmetic in Python and wrote the result back, and its own comment
# admitted the fit was poor: the mandate row is a hot document, and "a relational
# database with SELECT ... FOR UPDATE would express this invariant more
# naturally".
#
# DynamoDB expresses it in one request. The condition and the decrement are the
# same operation:
#
#     SET       reserved_paise  = reserved_paise  + :amount,
#               available_paise = available_paise - :amount
#     IF        available_paise >= :amount
#
# There is no window between the check and the write, because there is no
# "between". Closing that window is the entire reason this module exists - see
# the header - and it is now the database enforcing it rather than our careful
# use of one.
#
# Which is why `available_paise` is stored rather than derived. It is exactly
# `cap - committed - reserved` and looks like the kind of denormalisation that
# rots, but a condition expression cannot do arithmetic - DynamoDB allows it in
# SET and nowhere else, so `cap_paise - committed_paise - reserved_paise >=
# :amount` is not a condition that can be written. Keeping the difference in a
# column is what makes the guard a single atomic comparison instead of a
# read, a subtraction in Python, and a hope.
#
# Every mutation below maintains the invariant in the same write that changes
# its inputs, so the two cannot drift: reserve moves available to reserved,
# commit moves reserved to committed and leaves available alone, release puts
# it back.
#
# See pocketchange/dynamo.py for the table layout.


class DynamoLedger:
    """Durable ledger. Every mutation is one conditional write or one transaction."""

    def __init__(self, table=None) -> None:
        from . import dynamo

        self._table = table if table is not None else dynamo.table()
        self._dynamo = dynamo

    # --- keys --------------------------------------------------------------

    @staticmethod
    def _mandate_key(mandate_id: str) -> dict:
        return {"pk": f"MANDATE#{mandate_id}", "sk": "STATE"}

    @staticmethod
    def _reservation_key(mandate_id: str, reservation_id: str) -> dict:
        return {"pk": f"MANDATE#{mandate_id}", "sk": f"RES#{reservation_id}"}

    @staticmethod
    def _idem_key(mandate_id: str, idempotency_key: str) -> dict:
        return {"pk": f"MANDATE#{mandate_id}", "sk": f"IDEM#{idempotency_key}"}

    @staticmethod
    def _index_key(reservation_id: str) -> dict:
        return {"pk": f"RES#{reservation_id}", "sk": "INDEX"}

    # --- the protocol ------------------------------------------------------

    def open(self, mandate_id: str, cap_paise: int) -> LedgerState:
        """Register a mandate's ceiling. Safe to call repeatedly.

        Reopening with a different cap is refused. Otherwise a compromised agent
        could raise its own limit by asking twice - so the condition is "this row
        does not exist", and a collision is inspected rather than overwritten.
        """
        if cap_paise < 0:
            raise ValueError(f"negative cap: {cap_paise}")

        from botocore.exceptions import ClientError

        try:
            self._table.put_item(
                Item={
                    **self._mandate_key(mandate_id),
                    "cap_paise": cap_paise,
                    "committed_paise": 0,
                    "reserved_paise": 0,
                    "available_paise": cap_paise,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            existing = self.state(mandate_id)
            if existing.cap_paise != cap_paise:
                raise LedgerError(
                    f"mandate {mandate_id[:12]} already open at {existing.cap_paise}p, "
                    f"refusing to reopen at {cap_paise}p"
                ) from exc
        return self.state(mandate_id)

    def reserve(self, mandate_id: str, amount_paise: int, idempotency_key: str) -> Reservation:
        """Hold budget against the cap, or refuse.

        One transaction, four legs. The order of the legs is the order their
        failures are reported in, which is what lets a cancelled transaction be
        turned back into the right exception.
        """
        if amount_paise <= 0:
            raise ValueError(f"reservation must be positive, got {amount_paise}")
        if not idempotency_key.strip():
            raise ValueError("idempotency key is required")

        from botocore.exceptions import ClientError

        reservation_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)
        name = self._table.name

        try:
            self._table.meta.client.transact_write_items(TransactItems=[
                # Leg 0: the idempotency claim. Fails if this exact request was
                # already reserved, which is what stops an in-TTL replay from
                # being charged twice.
                {"Put": {
                    "TableName": name,
                    "Item": self._idem_key(mandate_id, idempotency_key)
                                    | {"reservation_id": reservation_id},
                    "ConditionExpression": "attribute_not_exists(pk)",
                }},
                # Leg 1: the budget. The check and the decrement are one
                # operation, so there is no window between them.
                {"Update": {
                    "TableName": name,
                    "Key": self._mandate_key(mandate_id),
                    "UpdateExpression":
                        "SET reserved_paise = reserved_paise + :amount, "
                        "available_paise = available_paise - :amount",
                    "ConditionExpression":
                        "attribute_exists(pk) AND available_paise >= :amount",
                    "ExpressionAttributeValues": {":amount": amount_paise},
                }},
                {"Put": {
                    "TableName": name,
                    "Item": {
                        **self._reservation_key(mandate_id, reservation_id),
                        "amount_paise": amount_paise,
                        "idempotency_key": idempotency_key,
                        "created_at": created_at.isoformat(),
                        "settled": False,
                    },
                }},
                # Leg 3: the reverse index, so settlement is O(1) from a
                # reservation id alone.
                {"Put": {
                    "TableName": name,
                    "Item": self._index_key(reservation_id)
                                    | {"mandate_id": mandate_id},
                }},
            ])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            return self._explain_failed_reservation(
                exc, mandate_id, amount_paise, idempotency_key)

        return Reservation(
            id=reservation_id,
            mandate_id=mandate_id,
            amount_paise=amount_paise,
            idempotency_key=idempotency_key,
            created_at=created_at,
        )

    def _explain_failed_reservation(
        self, exc, mandate_id: str, amount_paise: int, idempotency_key: str
    ) -> Reservation:
        """Turn a cancelled transaction back into the exception the caller expects.

        Three outcomes are handled completely differently upstream - a replay
        returns the original receipt, an over-budget request is a 402 carrying
        the numbers, an unknown mandate is a 404 - and a transaction reports all
        three as one cancellation. Reading the per-leg reasons is what keeps them
        distinguishable.
        """
        reasons = self._dynamo.cancellation_reasons(exc)
        failed = {
            index for index, code in enumerate(reasons)
            if code == "ConditionalCheckFailed"
        }

        if 0 in failed:
            # The idempotency claim lost: this request was already reserved, so
            # the honest answer is the original reservation.
            prior = self._table.get_item(
                Key=self._idem_key(mandate_id, idempotency_key)).get("Item")
            if prior:
                held = self._table.get_item(
                    Key=self._reservation_key(mandate_id, prior["reservation_id"])
                ).get("Item")
                if held:
                    return _as_reservation(mandate_id, held)

        if 1 in failed:
            # The budget condition lost. It covers two different facts - "no such
            # mandate" and "not enough left" - so ask which.
            current = self.state(mandate_id)   # raises UnknownMandate if absent
            raise InsufficientBudget(
                requested=amount_paise,
                available=current.available_paise,
                cap=current.cap_paise,
                committed=current.committed_paise,
                reserved=current.reserved_paise,
            ) from exc

        raise LedgerError(f"reservation refused: {'; '.join(reasons) or exc}") from exc

    def commit(self, reservation_id: str) -> LedgerState:
        """The payment succeeded. Turn the hold into spend."""
        from botocore.exceptions import ClientError

        mandate_id, reservation = self._locate(reservation_id)
        amount = int(reservation["amount_paise"])
        name = self._table.name
        try:
            self._table.meta.client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": name,
                    "Key": self._reservation_key(mandate_id, reservation_id),
                    "UpdateExpression": "SET settled = :yes",
                    # Settling twice would double-count the spend, so being
                    # unsettled is part of the condition rather than something
                    # checked beforehand and hoped to still hold.
                    "ConditionExpression": "attribute_exists(pk) AND settled = :no",
                    "ExpressionAttributeValues": {":yes": True, ":no": False},
                }},
                {"Update": {
                    "TableName": name,
                    "Key": self._mandate_key(mandate_id),
                    # available_paise is untouched: a settlement moves money
                    # from held to spent, and neither is available.
                    "UpdateExpression":
                        "SET committed_paise = committed_paise + :amount, "
                        "reserved_paise = reserved_paise - :amount",
                    "ExpressionAttributeValues": {":amount": amount},
                }},
            ])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            raise UnknownReservation(
                f"no open reservation {reservation_id[:12]}") from exc
        return self.state(mandate_id)

    def release(self, reservation_id: str) -> LedgerState:
        """The payment failed. Give the budget back.

        The idempotency claim is released too. A payment that never happened
        should be retryable - only a *successful* charge must stay pinned.
        """
        from botocore.exceptions import ClientError

        mandate_id, reservation = self._locate(reservation_id)
        amount = int(reservation["amount_paise"])
        name = self._table.name
        try:
            self._table.meta.client.transact_write_items(TransactItems=[
                {"Delete": {
                    "TableName": name,
                    "Key": self._reservation_key(mandate_id, reservation_id),
                    "ConditionExpression": "attribute_exists(pk) AND settled = :no",
                    "ExpressionAttributeValues": {":no": False},
                }},
                {"Delete": {
                    "TableName": name,
                    "Key": self._idem_key(
                        mandate_id, str(reservation["idempotency_key"])),
                }},
                {"Delete": {
                    "TableName": name,
                    "Key": self._index_key(reservation_id),
                }},
                {"Update": {
                    "TableName": name,
                    "Key": self._mandate_key(mandate_id),
                    "UpdateExpression":
                        "SET reserved_paise = reserved_paise - :amount, "
                        "available_paise = available_paise + :amount",
                    "ExpressionAttributeValues": {":amount": amount},
                }},
            ])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            raise UnknownReservation(
                f"no open reservation {reservation_id[:12]}") from exc
        return self.state(mandate_id)

    def state(self, mandate_id: str) -> LedgerState:
        item = self._table.get_item(Key=self._mandate_key(mandate_id)).get("Item")
        if not item:
            raise UnknownMandate(f"no mandate opened for {mandate_id[:12]}")
        return LedgerState(
            mandate_id=mandate_id,
            cap_paise=int(item["cap_paise"]),
            committed_paise=int(item["committed_paise"]),
            reserved_paise=int(item["reserved_paise"]),
        )

    def _locate(self, reservation_id: str) -> tuple[str, dict]:
        """Resolve a reservation id to its mandate via the reverse index."""
        entry = self._table.get_item(Key=self._index_key(reservation_id)).get("Item")
        if not entry:
            raise UnknownReservation(f"no open reservation {reservation_id[:12]}")
        mandate_id = str(entry["mandate_id"])
        reservation = self._table.get_item(
            Key=self._reservation_key(mandate_id, reservation_id)).get("Item")
        if not reservation or reservation.get("settled"):
            raise UnknownReservation(f"no open reservation {reservation_id[:12]}")
        return mandate_id, reservation


def _as_reservation(mandate_id: str, item: dict) -> Reservation:
    return Reservation(
        id=str(item["sk"]).removeprefix("RES#"),
        mandate_id=mandate_id,
        amount_paise=int(item["amount_paise"]),
        idempotency_key=str(item["idempotency_key"]),
        created_at=datetime.fromisoformat(str(item["created_at"])),
        settled=bool(item.get("settled", False)),
    )


# A note for the next person who reaches for boto3's TypeSerializer here.
#
# `transact_write_items` is a client-level call, and the client-level API takes
# DynamoDB's wire form - {"S": "x"} rather than "x" - so the obvious thing is to
# serialise every Item, Key and ExpressionAttributeValue on the way in. That was
# the first version of this file and every leg of every transaction failed with
# "unhashable type: dict".
#
# The reason: this client came from `Table.meta.client`, and boto3 attaches the
# document-interface transformation to the dynamodb *resource's* client. It
# serialises the parameters itself. Doing it first means doing it twice, and the
# resulting {"S": {"S": "x"}} is not a value DynamoDB can key on.
#
# So plain Python values go in, everywhere in this class, including the
# transactions. Use a client built by `boto3.client("dynamodb")` and the rule is
# the opposite one.


# Why the durable ledger was not used, when it was asked for and not obtained.
# None means nothing went wrong: either DynamoDB is in use, or it was never
# configured in the first place. Read by gateway.capabilities().
_FALLBACK_REASON: str | None = None


def fallback_reason() -> str | None:
    """The exception that put this process on the in-memory ledger, if any.

    A deployment that intended a durable ledger and silently got a volatile
    one is the worst outcome this module has, because everything keeps working
    right up until a restart eats the record of who was paid. So the reason is
    kept and reported rather than swallowed.
    """
    return _FALLBACK_REASON


def from_env() -> Ledger:
    """DynamoLedger when one is configured, MemoryLedger otherwise.

    Development and tests must never require cloud credentials, so absence of
    configuration degrades to in-memory rather than failing.

    Configured-but-broken is a different case from not-configured, and used to
    be indistinguishable: both returned a MemoryLedger and said nothing. That
    is how the first EC2 deployment ran its whole funnel against an in-memory
    ledger while a perfectly good DynamoDB table sat empty beside it - boto3
    could not resolve a region (see dynamo.region()), the constructor raised,
    and this function quietly handed back the volatile one. The fallback still
    happens, because an unreachable store must not stop local work, but it no
    longer happens quietly.
    """
    global _FALLBACK_REASON
    from . import dynamo

    if not dynamo.configured():
        _FALLBACK_REASON = None
        return MemoryLedger()
    try:
        ledger = DynamoLedger()
    except Exception as exc:  # noqa: BLE001 - unreachable storage must not stop local work
        _FALLBACK_REASON = f"{type(exc).__name__}: {exc}"
        return MemoryLedger()
    _FALLBACK_REASON = None
    return ledger
