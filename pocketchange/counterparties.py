"""Who we have actually paid, and how often.

The one reputation signal a seller cannot write.

Everything the buyer currently knows about a counterparty comes from the
counterparty. `merchant/sellers.py` computes `is_established` from
`trading_months >= 12 and review_count >= 100`, and both numbers sit in the
seller's own record; `agent/tools.py:seller_reputation` hands them to the
shopper. A hostile supplier sets review_count to a hundred thousand and is
"established" for free. That is the Sybil shape SoK calls Identity-to-Market,
and it is why `eval/vectors.py` had it marked not applicable - we had no
counter-evidence to offer.

This is the counter-evidence. It is written by the GATEWAY at settlement, from
payments that actually cleared, so it cannot be inflated by anyone outside this
process. A supplier can claim anything about itself; it cannot make us have paid
it before.

Be precise about what is and is not trusted:

  the IDENTITY  is declared on the payment by the agent, and the agent is the
                component we assume is compromised. It can misattribute.
  the HISTORY   is ours. Orders, totals and first-seen dates come from our own
                settled payments, and no amount of seller-side fabrication
                touches them.

So this does not answer "is this supplier honest". It answers "have we, in fact,
dealt with whoever this payment says it is dealing with" - which is exactly the
question the review-count signal cannot be trusted to answer.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True)
class Counterparty:
    """Our own record of dealings with one party."""

    id: str
    first_paid: datetime
    last_paid: datetime
    orders: int
    total_paise: int
    mandates: int          # distinct mandates that have paid them

    # --- how it went ---------------------------------------------------------
    #
    # An order count on its own is not a reputation, it is a tally, and a tally
    # is exactly what a bad counterparty wants you reading. Forty settled orders
    # alongside five escalations and two human refusals is a WORSE record than no
    # history at all, and a summary that reports only the forty reproduces the
    # sybil problem one level up - the thing this module exists to fix.
    escalated: int = 0     # the monitor stopped a payment to them for a person
    refused: int = 0       # the gateway denied a payment naming them
    vetoed: int = 0        # a HUMAN looked and said no. the strongest signal here
    injections: int = 0    # pages attributed to them carried instruction-shaped text

    @property
    def trouble(self) -> int:
        """Everything that went wrong, however it went wrong."""
        return self.escalated + self.refused + self.vetoed + self.injections

    @property
    def only_once(self) -> bool:
        """Exactly one prior dealing. Thin history, but not no history.

        Note what this is NOT: "this is their first payment". A record only
        exists once something has cleared, so the never-dealt-with case is the
        ABSENCE of a row - see NEW - not a row with one order in it. Conflating
        the two made a supplier we had already paid keep reading as unknown.
        """
        return self.orders == 1

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "first_paid": self.first_paid.isoformat(),
            "last_paid": self.last_paid.isoformat(),
            "orders": self.orders,
            "total_paise": self.total_paise,
            "mandates": self.mandates,
            "only_once": self.only_once,
            "escalated": self.escalated,
            "refused": self.refused,
            "vetoed": self.vetoed,
            "injections": self.injections,
            "trouble": self.trouble,
        }

    def describe(self) -> str:
        """One line for the monitor, describing the record as it already stands.

        Trouble comes FIRST, and deliberately. A model reading "paid 40 times,
        12,00,000 rupees" and then, eventually, "2 refused by a person" will
        anchor on the forty. The reverse ordering costs nothing and stops a long
        tally being used as cover.

        Never says "never": a Counterparty only exists because a payment cleared,
        so absence of a record is the no-history case, reported by the caller.
        """
        rupees = f"{self.total_paise // 100:,} rupees"
        days = max(0, (self.last_paid - self.first_paid).days)
        span = f" over {days} days" if days else ""
        if self.orders == 0:
            settled = "never paid"
        elif self.orders == 1:
            settled = f"paid once before, {rupees}"
        else:
            settled = f"paid {self.orders} times{span}, {rupees} in total"

        if not self.trouble:
            return settled

        bad = []
        if self.vetoed:
            bad.append(f"{self.vetoed} refused by a person")
        if self.injections:
            bad.append(f"{self.injections} page(s) from them carried injected instructions")
        if self.escalated:
            bad.append(f"{self.escalated} held for review")
        if self.refused:
            bad.append(f"{self.refused} refused by the gateway")
        return f"CONCERNS: {'; '.join(bad)} - and {settled}"


UNKNOWN = "not stated"
NEW = "never paid before"


# What can go wrong, and be remembered.
ESCALATED = "escalated"   # the monitor stopped a payment for a person
REFUSED = "refused"       # the gateway denied a payment naming them
VETOED = "vetoed"         # a person looked and said no
INJECTION = "injections"  # a page attributed to them carried instructions
FLAGS = (ESCALATED, REFUSED, VETOED, INJECTION)


class CounterpartyBook(Protocol):
    def record(self, counterparty: str, *, amount_paise: int, mandate_id: str) -> Counterparty: ...
    def flag(self, counterparty: str, kind: str) -> Counterparty: ...
    def lookup(self, counterparty: str) -> Counterparty | None: ...
    def all(self) -> list[Counterparty]: ...


class InMemoryCounterparties:
    """Process-local. Loses history on restart, which is honest about what it is."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, Counterparty] = {}
        self._mandates: dict[str, set[str]] = {}

    def record(self, counterparty: str, *, amount_paise: int, mandate_id: str) -> Counterparty:
        key = counterparty.strip()
        if not key:
            raise ValueError("a counterparty needs an identifier to be remembered")
        now = datetime.now(timezone.utc)
        with self._lock:
            seen = self._mandates.setdefault(key, set())
            seen.add(mandate_id)
            prior = self._rows.get(key)
            # replace(), not a fresh construction: building a new Counterparty
            # here silently dropped every flag, so a party we had refused or
            # caught injecting became clean again the moment they were paid -
            # rewarding exactly the behaviour the flags exist to remember.
            base = prior or Counterparty(key, now, now, 0, 0, 0)
            row = replace(
                base,
                first_paid=prior.first_paid if prior else now,
                last_paid=now,
                orders=base.orders + 1,
                total_paise=base.total_paise + amount_paise,
                mandates=len(seen),
            )
            self._rows[key] = row
            return row

    def flag(self, counterparty: str, kind: str) -> Counterparty:
        """Remember that something went wrong with this party.

        A flag needs no settled payment behind it - that is the point. A supplier
        whose pages carry injected instructions, or whose payment a person
        refused, has a record with us even though we never paid them, and that
        record is worth more than the absence of one.
        """
        key = counterparty.strip()
        if not key:
            raise ValueError("a counterparty needs an identifier to be flagged")
        if kind not in FLAGS:
            raise ValueError(f"unknown flag {kind!r}; expected one of {FLAGS}")
        now = datetime.now(timezone.utc)
        with self._lock:
            prior = self._rows.get(key)
            base = prior or Counterparty(key, now, now, 0, 0, 0)
            row = replace(base, **{kind: getattr(base, kind) + 1})
            self._rows[key] = row
            return row

    def lookup(self, counterparty: str) -> Counterparty | None:
        return self._rows.get(counterparty.strip())

    def all(self) -> list[Counterparty]:
        with self._lock:
            return sorted(self._rows.values(), key=lambda c: -c.orders)

    def __len__(self) -> int:
        return len(self._rows)


class DynamoCounterparties:
    """Durable, so the record survives a restart and spans deployments.

    Layout - one partition, one row per counterparty:

        pk                sk        attributes
        COUNTERPARTY      <id>      first_paid, last_paid, orders, total_paise,
                                    mandates (string set), and the flag counters

    One partition rather than one per counterparty, because `all()` is what the
    dashboard asks for and a shared partition makes that a Query instead of a
    Scan. It is a hot partition for writes, which at the rate a supplier book
    receives settlements is not a rate at all.

    Every mutation here is a single UpdateItem. The backend this replaced needed
    a read-modify-write transaction to add a mandate id to a list without losing
    a concurrent one; DynamoDB has native string sets, so `ADD mandates :one`
    does the union atomically and the transaction disappears.

    The set of mandate ids is kept rather than a count, so the figure stays
    correct when the same mandate pays a supplier twice.
    """

    PARTITION = "COUNTERPARTY"

    def __init__(self, table=None) -> None:
        from . import dynamo

        self._table = table if table is not None else dynamo.table()

    def _key(self, counterparty: str) -> dict:
        return {"pk": self.PARTITION, "sk": counterparty}

    def record(self, counterparty: str, *, amount_paise: int, mandate_id: str) -> Counterparty:
        key = counterparty.strip()
        if not key:
            raise ValueError("a counterparty needs an identifier to be remembered")
        now = datetime.now(timezone.utc)

        updated = self._table.update_item(
            Key=self._key(key),
            UpdateExpression=(
                "SET first_paid = if_not_exists(first_paid, :now), last_paid = :now "
                "ADD orders :one, total_paise :amount, mandates :mandate"
            ),
            ExpressionAttributeValues={
                ":now": now.isoformat(),
                ":one": 1,
                ":amount": amount_paise,
                ":mandate": {mandate_id},
            },
            ReturnValues="ALL_NEW",
        )["Attributes"]
        return _row_to_counterparty(key, updated)

    def flag(self, counterparty: str, kind: str) -> Counterparty:
        key = counterparty.strip()
        if not key:
            raise ValueError("a counterparty needs an identifier to be flagged")
        if kind not in FLAGS:
            raise ValueError(f"unknown flag {kind!r}; expected one of {FLAGS}")
        now = datetime.now(timezone.utc)

        # The flag counter name is interpolated into the expression, so it goes
        # through ExpressionAttributeNames rather than into the string. The
        # membership check above already constrains it to FLAGS; this is the
        # second lock on the same door, and the door is the one an injected
        # listing would try.
        updated = self._table.update_item(
            Key=self._key(key),
            UpdateExpression=(
                "SET first_paid = if_not_exists(first_paid, :now), "
                "last_paid = if_not_exists(last_paid, :now) "
                "ADD #flag :one"
            ),
            ExpressionAttributeNames={"#flag": kind},
            ExpressionAttributeValues={":now": now.isoformat(), ":one": 1},
            ReturnValues="ALL_NEW",
        )["Attributes"]
        return _row_to_counterparty(key, updated)

    def lookup(self, counterparty: str) -> Counterparty | None:
        key = counterparty.strip()
        item = self._table.get_item(Key=self._key(key)).get("Item")
        if not item:
            return None
        return _row_to_counterparty(key, item)

    def all(self) -> list[Counterparty]:
        from boto3.dynamodb.conditions import Key as KeyCondition

        rows: list[Counterparty] = []
        kwargs = {"KeyConditionExpression": KeyCondition("pk").eq(self.PARTITION)}
        while True:
            page = self._table.query(**kwargs)
            rows.extend(
                _row_to_counterparty(str(item["sk"]), item)
                for item in page.get("Items", [])
            )
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                break
            kwargs["ExclusiveStartKey"] = cursor
        return sorted(rows, key=lambda c: -c.orders)

    def __len__(self) -> int:
        return len(self.all())


def _as_datetime(value) -> datetime:
    """Whatever the store handed back, as an aware datetime.

    DynamoDB has no date type, so these arrive as ISO strings. The in-memory
    book holds real datetimes. Both have to read the same way, because the
    dashboard cannot tell which backend it is looking at - and neither should a
    reader of this file have to.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _row_to_counterparty(key: str, row: dict) -> Counterparty:
    return Counterparty(
        id=key,
        first_paid=_as_datetime(row.get("first_paid")),
        last_paid=_as_datetime(row.get("last_paid")),
        orders=int(row.get("orders", 0)),
        total_paise=int(row.get("total_paise", 0)),
        mandates=len(row.get("mandates") or ()),
        escalated=int(row.get("escalated", 0)),
        refused=int(row.get("refused", 0)),
        vetoed=int(row.get("vetoed", 0)),
        injections=int(row.get("injections", 0)),
    )


def from_env() -> CounterpartyBook:
    """DynamoDB when one is configured, in-memory otherwise.

    Same degradation as the ledger: local work and tests must never need cloud
    credentials, and unreachable storage must not stop a payment.
    """
    from . import dynamo

    if not dynamo.configured():
        return InMemoryCounterparties()
    try:
        return DynamoCounterparties()
    except Exception:  # noqa: BLE001
        return InMemoryCounterparties()
