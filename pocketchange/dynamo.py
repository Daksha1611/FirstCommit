"""DynamoDB: one table, and the conditional write that makes a ledger safe.

The storage this replaced was chosen because a previous event required a
particular cloud's service, and `ledger.py` said so in a comment that also
admitted the fit was poor: the mandate row is a hot document, one sustained
write per second, "a relational database with SELECT ... FOR UPDATE would
express this invariant more naturally".

DynamoDB expresses it more naturally still, and this is the one place in the
port where the AWS service is a genuine improvement rather than a swap.

The invariant the two-phase reservation exists to protect is:

    reserve only if cap - committed - reserved >= amount

The old backend needed a transaction that read the mandate, did the arithmetic
in Python, and wrote the result - three steps, with the correctness resting on
the transaction's isolation. DynamoDB does it in one request:

    UpdateItem
      SET reserved_paise = reserved_paise + :amount
      CONDITION cap_paise - committed_paise - reserved_paise >= :amount

The check and the decrement are the same operation. There is no window between
them, because there is no "between".

Layout - a single table, composite key:

    PK                      SK              what
    MANDATE#<mandate_id>    STATE           cap, committed, reserved
    MANDATE#<mandate_id>    RES#<res_id>    amount, idem key, settled
    MANDATE#<mandate_id>    IDEM#<key>      -> reservation id
    RES#<res_id>            INDEX           -> mandate id

The last one is a reverse index. The gateway holds only a reservation id when it
settles, and a reservation lives under its mandate, so one small write buys an
O(1) lookup instead of a scan. Same reasoning as the backend it replaces.

Runs against DynamoDB Local or LocalStack with no AWS account - set
POCKETCHANGE_DDB_ENDPOINT. See deploy/README.md.
"""

from __future__ import annotations

import os
from functools import lru_cache

TABLE_ENV = "POCKETCHANGE_DDB_TABLE"
ENDPOINT_ENV = "POCKETCHANGE_DDB_ENDPOINT"
DEFAULT_TABLE = "pocketchange"
DEFAULT_REGION = "us-east-1"


def table_name() -> str:
    return os.environ.get(TABLE_ENV, "").strip() or DEFAULT_TABLE


def endpoint() -> str | None:
    """A local DynamoDB, when one is configured. None means the real thing."""
    return os.environ.get(ENDPOINT_ENV, "").strip() or None


def region() -> str:
    """The region, resolved here rather than left to boto3.

    This is not belt-and-braces. botocore resolves a session's region from
    AWS_DEFAULT_REGION and *not* from AWS_REGION - 1.43.98 reads the second
    one not at all - so a container started with only AWS_REGION set has a
    correct-looking environment, `os.environ["AWS_REGION"]` returning the
    right string, and `boto3.Session().region_name` of None. The resource
    constructor then raises NoRegionError, `ledger.from_env()` catches it,
    and the deployment quietly runs on the in-memory ledger.

    That is not hypothetical; it is what the first EC2 deployment did, and
    nothing in the logs said so. bedrock.py already resolved its own region
    for the same class of reason. This is that fix, applied to the module
    that holds the money.
    """
    for name in ("POCKETCHANGE_DDB_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return DEFAULT_REGION


def configured() -> bool:
    """Whether this process should use DynamoDB at all.

    A local endpoint counts on its own - that is the whole point of running
    against DynamoDB Local, where there are no real credentials to find. A
    deployment needs credentials to resolve, and needs not to be switched off.
    """
    if os.environ.get("POCKETCHANGE_NO_DYNAMODB"):
        return False
    if endpoint():
        return True
    try:
        import boto3
    except ImportError:
        return False
    try:
        return boto3.Session().get_credentials() is not None
    except Exception:  # noqa: BLE001
        return False


@lru_cache(maxsize=1)
def table():
    """The boto3 Table handle, built once.

    Cached because botocore builds a client from a JSON service model and that
    is slow enough to notice on a path this project times.
    """
    import boto3

    # region_name unconditionally: see region() for why leaving this to
    # boto3's own resolution puts the ledger in memory without saying so.
    kwargs = {"region_name": region()}
    if endpoint():
        # DynamoDB Local accepts any credentials but boto3 still insists on
        # finding some, so a machine with no AWS config at all can run the demo.
        kwargs |= {
            "endpoint_url": endpoint(),
            "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", "local"),
            "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", "local"),
        }
    return boto3.resource("dynamodb", **kwargs).Table(table_name())


def reset_cache() -> None:
    """Forget the cached handle. Tests move the endpoint between cases."""
    table.cache_clear()


def create_table_if_absent() -> bool:
    """Create the table. Returns True if it made one.

    Here rather than only in the SAM template because the local path has to work
    without SAM: `python -m pocketchange.cli init-table` against DynamoDB Local
    is the whole setup for a laptop.

    On-demand billing, so nothing has to be capacity-planned for a demo.
    """
    import boto3
    from botocore.exceptions import ClientError

    kwargs = {"region_name": region()}
    if endpoint():
        kwargs |= {
            "endpoint_url": endpoint(),
            "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", "local"),
            "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", "local"),
        }
    client = boto3.client("dynamodb", **kwargs)
    try:
        client.create_table(
            TableName=table_name(),
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceInUseException":
            return False
        raise
    client.get_waiter("table_exists").wait(TableName=table_name())
    return True


def cancellation_reasons(exc) -> list[str]:
    """Which leg of a TransactWriteItems failed, in order.

    A cancelled transaction reports one reason per item, and "None" for the legs
    that were fine. Without reading them every failure looks the same, and this
    module has to tell "already reserved" from "over budget" from "no such
    mandate" - three outcomes the caller handles completely differently.
    """
    response = getattr(exc, "response", None) or {}
    return [
        reason.get("Code", "None")
        for reason in response.get("CancellationReasons", []) or []
    ]
