"""The root key's third tier: Secrets Manager, on a mocked AWS.

No AWS account and no network: moto intercepts botocore, the same way
test_ledger_backends.py and test_store_backends.py exercise DynamoDB. What this
file is actually protecting against is the failure `secrets.py`'s docstring
describes - two "instances" minting two different keys - which a test against
a single client object cannot reproduce for real. It reproduces the shape of
it instead: call `load_or_create()` twice and require the same key back, and
force the exact race window (`ResourceExistsException` on `CreateSecret`) to
prove the loser fetches the winner's key rather than keeping its own.
"""

from __future__ import annotations

import pytest

from pocketchange import secrets


@pytest.fixture
def moto_secrets(monkeypatch):
    from moto import mock_aws

    # moto intercepts botocore, but boto3 still insists on finding credentials
    # before it will build a client - conftest has deliberately removed them.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("POCKETCHANGE_NO_SECRETS_MANAGER", raising=False)
    monkeypatch.setenv(secrets.SECRET_ENV, "test/pocketchange-root-key")

    with mock_aws():
        yield


def test_configured_is_false_without_a_secret_name(monkeypatch):
    monkeypatch.delenv("POCKETCHANGE_NO_SECRETS_MANAGER", raising=False)
    monkeypatch.delenv(secrets.SECRET_ENV, raising=False)
    assert secrets.configured() is False


def test_configured_is_false_when_the_override_is_set(monkeypatch):
    monkeypatch.setenv(secrets.SECRET_ENV, "test/pocketchange-root-key")
    monkeypatch.setenv("POCKETCHANGE_NO_SECRETS_MANAGER", "1")
    assert secrets.configured() is False


def test_first_call_mints_and_stores_a_key(moto_secrets):
    import boto3

    keypair = secrets.load_or_create()
    assert keypair.public_key is not None

    client = boto3.client("secretsmanager", region_name="us-east-1")
    stored = client.get_secret_value(SecretId=secrets.secret_name())
    assert stored["SecretBinary"] == bytes(keypair.private_key.to_bytes())


def test_second_call_returns_the_same_key_not_a_fresh_one(moto_secrets):
    first = secrets.load_or_create()
    second = secrets.load_or_create()
    assert bytes(first.private_key.to_bytes()) == bytes(second.private_key.to_bytes())


def test_signatures_from_the_fetched_key_still_verify(moto_secrets):
    """Not just equal bytes - a working keypair, round-tripped through the wire
    format Secrets Manager actually stores and boto3 actually returns.
    """
    from biscuit_auth import Biscuit, BiscuitBuilder, BiscuitValidationError, KeyPair

    minted = secrets.load_or_create()
    fetched = secrets.load_or_create()

    token = BiscuitBuilder("right(true);").build(minted.private_key)
    raw = token.to_base64()

    # Verifying with the *fetched* key's public half is the actual claim this
    # module makes: two "instances" that each called load_or_create() agree.
    Biscuit.from_base64(raw, fetched.public_key)

    with pytest.raises(BiscuitValidationError):
        Biscuit.from_base64(raw, KeyPair().public_key)


def test_a_racing_create_fetches_the_winner_instead_of_keeping_its_own(
    moto_secrets, monkeypatch
):
    """The exact window the docstring calls out: this process sees "not found",
    generates a key, and loses the race to create the secret. It must return
    whatever the winner actually stored, not the key it just generated.
    """
    import boto3
    from botocore.exceptions import ClientError

    winner_client = boto3.client("secretsmanager", region_name="us-east-1")
    winning_keypair_bytes = b"\x01" * 32  # a stand-in "someone else's" key
    winner_client.create_secret(
        Name=secrets.secret_name(), SecretBinary=winning_keypair_bytes
    )

    real_client = boto3.client("secretsmanager", region_name="us-east-1")
    real_get_secret_value = real_client.get_secret_value
    calls = {"get": 0}

    def flaky_get_secret_value(*args, **kwargs):
        # The first GetSecretValue this test sees must report "not found" so
        # load_or_create() takes the mint-and-create branch, even though the
        # secret above already exists - simulating this process having
        # checked a beat before the winner created it.
        calls["get"] += 1
        if calls["get"] == 1:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "no"}},
                "GetSecretValue",
            )
        return real_get_secret_value(*args, **kwargs)

    def losing_create_secret(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "ResourceExistsException", "Message": "already there"}},
            "CreateSecret",
        )

    monkeypatch.setattr("boto3.client", lambda *a, **k: real_client)
    monkeypatch.setattr(real_client, "get_secret_value", flaky_get_secret_value)
    monkeypatch.setattr(real_client, "create_secret", losing_create_secret)

    result = secrets.load_or_create()
    # The point of the test: this is the WINNER's key, not the one this
    # process minted locally and then lost the race to store.
    assert bytes(result.private_key.to_bytes()) == winning_keypair_bytes
    assert calls["get"] == 2


def test_dynamo_resolves_a_region_without_boto3s_help(monkeypatch):
    """The trap that put a live deployment on the in-memory ledger.

    Only AWS_REGION set - which botocore does not read for session region -
    must still produce a usable region here, because dynamo.py resolves it
    itself rather than leaving it to boto3.
    """
    from pocketchange import dynamo

    monkeypatch.delenv("POCKETCHANGE_DDB_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    assert dynamo.region() == "ap-south-1"

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    assert dynamo.region() == "eu-west-1"

    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert dynamo.region() == dynamo.DEFAULT_REGION


def test_a_broken_durable_ledger_is_reported_not_swallowed(monkeypatch):
    """Configured-and-broken must not look like never-configured.

    Both give you a MemoryLedger; only one of them means the spend record
    dies with the process, and that one now says so.
    """
    from pocketchange import dynamo, ledger

    monkeypatch.setattr(dynamo, "configured", lambda: True)

    def explode():
        raise RuntimeError("NoRegionError: You must specify a region.")

    monkeypatch.setattr(dynamo, "table", explode)

    got = ledger.from_env()
    assert isinstance(got, ledger.MemoryLedger)
    assert "NoRegionError" in (ledger.fallback_reason() or "")

    # ...and not-configured stays silent, because that is a choice.
    monkeypatch.setattr(dynamo, "configured", lambda: False)
    ledger.from_env()
    assert ledger.fallback_reason() is None
