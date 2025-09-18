"""Exercise the real DynamoDB ledger: the same invariants, durable storage.

The test suite proves these against a mocked DynamoDB, which is fast and runs
anywhere and cannot tell you that your table exists, your credentials resolve,
or your condition expressions are ones the real service accepts. This can. Run
it once before a demo.

Against DynamoDB Local, no AWS account needed:

    docker run -p 8000:8000 amazon/dynamodb-local
    export POCKETCHANGE_DDB_ENDPOINT=http://localhost:8000
    .venv/bin/python scripts/smoke_dynamodb.py

Against a real table:

    export AWS_REGION=ap-south-1
    .venv/bin/python scripts/smoke_dynamodb.py
"""

import uuid

from pocketchange import config, dynamo
from pocketchange.ledger import DynamoLedger, InsufficientBudget
from pocketchange.policy import RUPEE

config.load()

if not dynamo.configured():
    raise SystemExit(
        "No DynamoDB configured. Set POCKETCHANGE_DDB_ENDPOINT for a local one, "
        "or run `aws configure` for a real table.\n"
        "(If POCKETCHANGE_NO_DYNAMODB is set, unset it.)"
    )

created = dynamo.create_table_if_absent()
ledger = DynamoLedger()
mandate = f"smoke-{uuid.uuid4().hex[:12]}"
ok = 0


def check(label: str, condition: bool) -> None:
    global ok
    ok += bool(condition)
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}")


print(f"\ntable    {dynamo.table_name()}{'  (created)' if created else ''}")
print(f"endpoint {dynamo.endpoint() or 'aws'}")
print(f"mandate  {mandate}\n")

state = ledger.open(mandate, 600_000 * RUPEE)
check("opened with a 6000 rupee cap", state.cap_paise == 600_000 * RUPEE)

first = ledger.reserve(mandate, 359_600 * RUPEE, "cart-a")
check("reserve holds budget before any charge",
      ledger.state(mandate).reserved_paise == 359_600 * RUPEE)

again = ledger.reserve(mandate, 359_600 * RUPEE, "cart-a")
check("same key returns the same reservation", again.id == first.id)
check("and does not double-hold", ledger.state(mandate).reserved_paise == 359_600 * RUPEE)

state = ledger.commit(first.id)
check("commit turns the hold into spend",
      state.committed_paise == 359_600 * RUPEE and state.reserved_paise == 0)

try:
    ledger.reserve(mandate, 359_600 * RUPEE, "cart-b")
    check("cumulative spend refuses the second payment", False)
except InsufficientBudget as exc:
    # The headline. Both payments are individually valid and a token as
    # specified permits both; the conditional write is what refuses the second.
    check("cumulative spend refuses the second payment", exc.available == 240_400 * RUPEE)

held = ledger.reserve(mandate, 200_000 * RUPEE, "cart-c")
ledger.release(held.id)
state = ledger.state(mandate)
check("release returns the budget",
      state.reserved_paise == 0 and state.available_paise == 240_400 * RUPEE)

retry = ledger.reserve(mandate, 200_000 * RUPEE, "cart-c")
check("a released key is retryable", retry.id != held.id)
ledger.release(retry.id)

# The stored `available_paise` is a denormalisation - a condition expression
# cannot do arithmetic, so the guard needs the difference in a column. If it ever
# disagrees with cap - committed - reserved, every budget check after that point
# is being made against a number that is not true.
row = dynamo.table().get_item(Key=DynamoLedger._mandate_key(mandate))["Item"]
derived = int(row["cap_paise"]) - int(row["committed_paise"]) - int(row["reserved_paise"])
check("the denormalised available figure still agrees with the ledger",
      int(row["available_paise"]) == derived)

TOTAL = 9
print(f"\n{ok}/{TOTAL} gates passed")
print(f"durable: this mandate is now a real row in {dynamo.table_name()}.\n")
raise SystemExit(0 if ok == TOTAL else 1)
