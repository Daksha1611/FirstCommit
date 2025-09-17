"""Amazon Bedrock: the model behind every judgement this system makes.

Bedrock replaced a direct-to-vendor API here, and the reason was not branding.
The old path authenticated with a bare API key belonging to whoever pasted it
into `.env`, carried its own free-tier quota, and left no trace in any account
anyone administers. Bedrock authenticates with the ordinary AWS credential
chain, so the model is reached the same way the ledger's table is: an IAM
principal, a region, and a policy someone can read.

That matters more than it sounds for a project about spending authority. The
monitor is a trusted component - it is asked whether a payment should proceed -
and a trusted component whose credential is a shared secret in an environment
file is only nominally trusted. On Bedrock the call is signed by the task's
role, and `bedrock:InvokeModel` can be scoped to exactly the model ids below.

Three tiers, because the work is genuinely three different jobs:

  * WORK_MODEL     the buyer's reasoning - planning, decomposition, choosing
  * SHOPPER_MODEL  the parallel fan-out, where several agents run at once
  * JUDGE_MODEL    the monitor, which is deliberately small and fast

The monitor being the smallest model is a design decision inherited from the
AI-control pattern this project follows, not a cost saving: the trusted layer
should be simple enough to reason about, and it answers one narrow question.

See PORTING.md §D1-D4 for what was tried before this shape.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel

# Bedrock model ids carry a vendor prefix. Several regions serve the current
# models only through a cross-region inference profile, whose id is the same
# string behind a geography prefix - `us.`, `eu.` or `apac.`. Which one is
# needed is a property of the account and region, not of the code, so every id
# here is overridable and a wrong one is allowed to surface as an error rather
# than be guessed at.
WORK_MODEL = os.environ.get("POCKETCHANGE_BEDROCK_MODEL", "anthropic.claude-opus-5")
SHOPPER_MODEL = os.environ.get("POCKETCHANGE_BEDROCK_SHOPPER_MODEL", "anthropic.claude-sonnet-5")
JUDGE_MODEL = os.environ.get("POCKETCHANGE_BEDROCK_JUDGE_MODEL", "anthropic.claude-haiku-4-5")

# us-east-1 rather than ap-south-1, despite every price in this project being in
# paise. Model availability by region is not uniform, and a demo that fails
# because a model is not served in Mumbai fails for a reason that has nothing to
# do with what is being demonstrated.
DEFAULT_REGION = "us-east-1"

# The SDK retries connection errors, 429 and 5xx on its own, which is what the
# hand-rolled backoff this replaced was for. Two is its default; four suits a
# fan-out where several agents meet the same throttle at once.
MAX_RETRIES = 4
TIMEOUT_SECONDS = 45.0


class BedrockUnavailable(Exception):
    """No credentials, no region, or the SDK is missing.

    Raised rather than returned so a caller cannot mistake "could not ask" for
    "the answer was no" - the distinction this project cares about most.
    """


def region() -> str:
    for name in ("POCKETCHANGE_BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return DEFAULT_REGION


def available() -> bool:
    """Whether a Bedrock call could be made, without making one.

    Deliberately does not reach the network. This is read by `/status` and by
    the gateway's capability report, both of which are polled, and neither
    should be able to spend a model call or hang on a timeout.
    """
    if os.environ.get("POCKETCHANGE_NO_BEDROCK"):
        return False
    try:
        import boto3
    except ImportError:
        return False
    try:
        return boto3.Session(region_name=region()).get_credentials() is not None
    except Exception:  # noqa: BLE001 - a broken profile is "unavailable", not a crash
        return False


def client(timeout: float = TIMEOUT_SECONDS):
    """A Bedrock client on the ordinary AWS credential chain.

    No key is read from the environment here. Whatever boto3 resolves - a task
    role, an SSO profile, instance metadata - is what signs the call.

    The timeout is a parameter rather than a constant because a hang is not an
    exception: a decompose call that never returns leaves a run silently not
    growing, with no error and no terminal event. Observed at 75 seconds and
    still climbing, which is why every caller passes one.
    """
    if not available():
        # Say which of the two it is. The first draft of this message suggested
        # setting POCKETCHANGE_NO_BEDROCK to "work offline", which is exactly
        # backwards - that switch is what forces this failure - and it was read
        # by someone who had already set it.
        if os.environ.get("POCKETCHANGE_NO_BEDROCK"):
            raise BedrockUnavailable(
                "POCKETCHANGE_NO_BEDROCK is set, so no model is reachable in "
                "this process. Callers that can run without judgement check "
                "`bedrock.available()` first; this one needs a model. Unset it "
                "and run `aws configure`."
            )
        raise BedrockUnavailable(
            f"no AWS credentials resolved for region {region()}. "
            "Run `aws configure`, or give this process a role that can call "
            "bedrock:InvokeModel."
        )
    from anthropic import AnthropicBedrockMantle

    return AnthropicBedrockMantle(
        aws_region=region(), max_retries=MAX_RETRIES, timeout=timeout
    )


def structured(
    prompt: str,
    schema: type[BaseModel],
    *,
    model: str | None = None,
    max_tokens: int = 2048,
    system: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> BaseModel:
    """Ask Bedrock for one object of `schema`, validated before it is returned.

    The schema is enforced by the API rather than described in the prompt, so a
    reply that does not fit the shape is not something this function can return.
    A caller never has to decide whether a half-parsed object is good enough,
    because it never receives one - which is what the old path's "describe the
    JSON Schema in the prompt and hope" fallback could not promise.
    """
    response = client(timeout).messages.parse(
        model=model or WORK_MODEL,
        max_tokens=max_tokens,
        system=system or "Reply only with the requested structure.",
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )
    parsed = response.parsed_output
    if parsed is None:
        # Structured output is constrained, so this is rare - but a refusal
        # stops generation with nothing parsed, and that must never read as an
        # empty-but-valid answer.
        raise BedrockUnavailable(
            f"Bedrock returned no parsable {schema.__name__} "
            f"(stop_reason={response.stop_reason})"
        )
    return parsed


def text(prompt: str, *, model: str | None = None, max_tokens: int = 1024) -> str:
    """One plain-text answer, for callers that parse the reply themselves."""
    response = client().messages.create(
        model=model or JUDGE_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def json_object(prompt: str, *, model: str | None = None,
                max_tokens: int = 1024) -> dict[str, Any]:
    """A JSON reply, tolerating a fenced block. For callers with no schema."""
    body = text(prompt, model=model, max_tokens=max_tokens).strip()
    if body.startswith("```"):
        body = body.split("```")[1].removeprefix("json").strip()
    return json.loads(body)


def spread_models(count: int) -> tuple[str, ...]:
    """One model id per parallel agent.

    The path this replaced returned a *different* model name per agent, because
    that provider's free tier metered quota per model name and six agents
    sharing one name queued behind one bucket. That was a real finding.

    It buys nothing on Bedrock, which meters per account and region. Returning
    distinct ids now would spread load across models of different capability for
    no benefit, which is strictly worse, so every agent gets the shopper model
    and throttling is handled by retry where it belongs.

    The signature stays because the graph builder asks for one id per agent, and
    a genuine tier split - a stronger model for one strategy - would live here.
    """
    return tuple(SHOPPER_MODEL for _ in range(max(count, 0)))
