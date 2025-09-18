"""Where the root signing key lives once a laptop's disk stops being the disk.

`keys.py` fixed one failure mode: a gateway that restarts keeps its key, so a
bounce is not a silent revocation of every outstanding mandate. It fixes
nothing about a *container*. App Runner's filesystem is not attached storage -
a redeploy, a crash restart, or a second instance scaled out for load each get
their own empty disk, and each one calls `keys.load_or_create()` and mints a
different key. The result is not an outage. It is worse: some fraction of
otherwise-valid mandates fail verification depending on which instance happens
to serve the request, and the failure looks exactly like forgery.

This is the fix TODO.md called the biggest gap in the project: the root key in
AWS Secrets Manager, fetched at startup, identical across every instance
because there is only one secret.

Same shape as `dynamo.py` on purpose - an env-var switch, a get-or-create that
runs once, and a hard `NO_*` override tests reach for before boto3 is consulted
at all. There is no local-endpoint equivalent of DynamoDB Local here, because
there is no such thing as Secrets Manager Local; a laptop wants `keys.py`, and
the ephemeral-keys test switch already runs before this module is ever
imported - see `gateway._root_keypair()`.
"""

from __future__ import annotations

import base64
import os

SECRET_ENV = "POCKETCHANGE_KEY_SECRET_NAME"
DEFAULT_SECRET_NAME = "pocketchange/root-key"


def secret_name() -> str:
    return os.environ.get(SECRET_ENV, "").strip() or DEFAULT_SECRET_NAME


def configured() -> bool:
    """Whether this process should fetch the root key from Secrets Manager.

    Mirrors `dynamo.configured()`: a `NO_*` override wins outright, and
    otherwise this only turns on when someone has actually named a secret -
    unlike DynamoDB, there is no ambient "credentials happen to be on this
    machine" default, because minting a *second* root key by accident is a
    much worse failure than the one this module exists to close.
    """
    if os.environ.get("POCKETCHANGE_NO_SECRETS_MANAGER"):
        return False
    if not os.environ.get(SECRET_ENV, "").strip():
        return False
    try:
        import boto3
    except ImportError:
        return False
    try:
        return boto3.Session().get_credentials() is not None
    except Exception:  # noqa: BLE001
        return False


def load_or_create():
    """The root key: one secret, fetched or minted, identical everywhere.

    `GetSecretValue` first, because after the first instance the secret always
    exists and a create attempt would just be a slower, noisier way to find
    that out. Only `ResourceNotFoundException` falls through to minting one -
    every other failure (denied, throttled, wrong region) surfaces, the same
    choice `dynamo.create_table_if_absent()` makes about its own ClientErrors.

    Two instances can still both see "not found" on a cold start at the same
    moment. Whichever one's `CreateSecret` lands second gets
    `ResourceExistsException`, not the secret - and it must not fall back to
    the key it just generated locally, or the two instances would sign with
    two different keys, which is exactly the bug this module exists to close.
    It fetches what actually won instead.
    """
    import boto3
    from biscuit_auth import Algorithm, KeyPair, PrivateKey
    from botocore.exceptions import ClientError

    client = boto3.client(
        "secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1")
    )
    name = secret_name()

    try:
        response = client.get_secret_value(SecretId=name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        keypair = KeyPair()
        try:
            client.create_secret(
                Name=name, SecretBinary=bytes(keypair.private_key.to_bytes())
            )
            return keypair
        except ClientError as create_exc:
            if create_exc.response["Error"]["Code"] != "ResourceExistsException":
                raise
            response = client.get_secret_value(SecretId=name)

    raw = response.get("SecretBinary")
    if raw is None:
        # boto3's own SecretBinary comes through already decoded; a secret
        # created by hand through the console instead stores base64 text in
        # SecretString, and that path deserves to keep working too.
        raw = base64.b64decode(response["SecretString"])
    return KeyPair.from_private_key(PrivateKey.from_bytes(bytes(raw), Algorithm.Ed25519))
