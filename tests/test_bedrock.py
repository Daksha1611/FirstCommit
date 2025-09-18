"""The Bedrock boundary: availability, region, and model spreading.

These replace the tests for the two-doors-to-one-vendor arrangement the project
used before the port. The interesting properties moved rather than disappeared:
what used to be "which door opened" is now "can this process reach a model at
all", and it has to be answerable without spending a call or touching the
network, because /status polls it.

Nothing here reaches AWS.
"""

import pytest

from pocketchange import bedrock


# --- availability, without asking anyone ------------------------------------


def test_the_kill_switch_wins_over_any_credential(monkeypatch):
    """The switch the whole suite depends on.

    Clearing AWS_* is not enough to take this process offline: boto3 also reads
    ~/.aws/config, an SSO cache and instance metadata, none of which a test can
    unset. Without an explicit switch, the suite silently went back on the
    network on any machine where `aws configure` had ever been run.
    """
    monkeypatch.setenv("POCKETCHANGE_NO_BEDROCK", "1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAnotreal")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "alsonotreal")
    assert bedrock.available() is False


def test_availability_reports_false_rather_than_raising(monkeypatch):
    """/status polls this. It must never raise and never block."""
    monkeypatch.delenv("POCKETCHANGE_NO_BEDROCK", raising=False)

    def broken(**kw):
        raise RuntimeError("profile is malformed")

    import boto3

    monkeypatch.setattr(boto3, "Session", broken)
    assert bedrock.available() is False


def test_an_unavailable_client_says_what_to_do(monkeypatch):
    monkeypatch.setenv("POCKETCHANGE_NO_BEDROCK", "1")
    with pytest.raises(bedrock.BedrockUnavailable) as caught:
        bedrock.client()
    assert "aws configure" in str(caught.value)


# --- region -----------------------------------------------------------------


def test_the_project_setting_outranks_the_ambient_aws_region(monkeypatch):
    """A developer whose shell is pointed at one region for unrelated work must
    not silently move this project's model calls."""
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("POCKETCHANGE_BEDROCK_REGION", "ap-south-1")
    assert bedrock.region() == "ap-south-1"


def test_the_aws_region_is_used_when_the_project_says_nothing(monkeypatch):
    monkeypatch.delenv("POCKETCHANGE_BEDROCK_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert bedrock.region() == "eu-west-1"


def test_there_is_always_a_region(monkeypatch):
    for name in ("POCKETCHANGE_BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)
    assert bedrock.region() == bedrock.DEFAULT_REGION


# --- model spreading --------------------------------------------------------


def test_parallel_agents_share_one_model():
    """The reversal described in PORTING.md D3.

    The previous provider metered quota per model name, so parallel agents had
    to be given different names or they queued behind one bucket. Bedrock meters
    per account and region, so distinct ids would only mean agents of unequal
    ability for no gain.
    """
    assert bedrock.spread_models(3) == (bedrock.SHOPPER_MODEL,) * 3


def test_spreading_none_is_empty_rather_than_an_error():
    assert bedrock.spread_models(0) == ()
    assert bedrock.spread_models(-1) == ()


# --- the tiers --------------------------------------------------------------


def test_the_judge_is_not_the_work_model():
    """The monitor is deliberately the smaller model - it is the trusted layer
    of an AI-control arrangement and answers one narrow question."""
    assert bedrock.JUDGE_MODEL != bedrock.WORK_MODEL
