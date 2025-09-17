"""Test-wide isolation.

Every gateway built in a test gets its own root key. Without this the persisted
key is shared, two gateways verify each other's tokens, and a forgery test
quietly stops testing forgery - which is exactly what happened when key
persistence was added: the assertion moved from 401 forged to 404 unknown
mandate and would have gone unnoticed if the suite had not been re-run.

Tests must also never write a key to the repository.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def ephemeral_keys(monkeypatch):
    monkeypatch.setenv("POCKETCHANGE_EPHEMERAL_KEYS", "1")


@pytest.fixture(autouse=True, scope="session")
def never_read_dotenv():
    """Stop config.load() undoing the scrubbing below.

    It is called again inside every model invocation, so clearing the
    environment was not enough: a test that believed it was offline would have
    its credentials restored from .env and quietly reach the network.
    """
    os.environ["POCKETCHANGE_NO_DOTENV"] = "1"
    yield
    os.environ.pop("POCKETCHANGE_NO_DOTENV", None)


@pytest.fixture(autouse=True)
def no_cloud(monkeypatch):
    """No credentials in tests, so nothing reaches DynamoDB, Razorpay or Bedrock.

    Clearing the environment is NOT sufficient for AWS and this is the trap the
    port walked into. boto3 resolves credentials from a chain - `~/.aws/config`,
    an SSO cache, instance metadata - none of which is an environment variable,
    so on any developer machine with `aws configure` already run,
    `bedrock.available()` kept answering True with an empty environment and the
    suite quietly went back on the network.

    POCKETCHANGE_NO_BEDROCK is the switch that actually closes it, checked
    before boto3 is consulted at all. POCKETCHANGE_NO_SECRETS_MANAGER is the
    same switch for secrets.py, added when the root key gained a third tier -
    without it, a suite run on a machine with both AWS credentials and
    POCKETCHANGE_KEY_SECRET_NAME set in its shell profile would quietly start
    minting or fetching a real key on every test.
    """
    monkeypatch.setenv("POCKETCHANGE_NO_BEDROCK", "1")
    monkeypatch.setenv("POCKETCHANGE_NO_DYNAMODB", "1")
    monkeypatch.setenv("POCKETCHANGE_NO_SECRETS_MANAGER", "1")
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "RAZORPAY_KEY_ID",
        "RAZORPAY_KEY_SECRET",
        # The fallback providers. Omitting these was not hypothetical: adding the
        # fallback chain immediately turned two "the model is unreachable" tests
        # into live calls to Groq, which passed for the wrong reason and put the
        # suite back on the network.
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "CEREBRAS_API_KEY",
        "SAMBANOVA_API_KEY",
        "TAVILY_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def join_run_threads():
    """`POST /runs` answers before its funnel finishes - the work continues on
    a daemon thread named `run-<mandate id, short>` (gateway.py). A test that
    hits `/runs` and moves on without waiting leaves that thread running into
    whichever test collects next, where it goes on writing to
    `gateway.state` well after that test's `client` fixture has already
    replaced it with a fresh one.

    That is the actual cause of the flake TODO.md describes against
    test_t2t_a_fabricated_price_cannot_enter_a_cart - it never calls /runs
    itself; an earlier test's still-running funnel does, mutating state out
    from under whatever runs next. It is not that one test either: the same
    leak surfaces as whatever assertion the *next* test happens to make, which
    is why it reproduces as a different failure on a different run rather
    than the same one every fifth time.

    A timeout well under RUN_DEADLINE_SECONDS - every funnel exercised in this
    suite runs the deterministic decomposer against a small tree and finishes
    in well under a second; a thread that is still alive after 10s is not
    "the demo running slowly", it is a bug worth a loud failure rather than a
    join() that returns silently anyway.
    """
    yield
    import threading

    current = threading.current_thread()
    for thread in threading.enumerate():
        if thread is current or not thread.name.startswith("run-"):
            continue
        thread.join(timeout=10)
        assert not thread.is_alive(), (
            f"{thread.name} outlived its test - it would go on mutating "
            "gateway.state after the next test replaces it"
        )
