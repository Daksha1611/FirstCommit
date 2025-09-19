"""Model construction and the structured-chain idiom, on Amazon Bedrock.

Two things live here: how an agent node gets a model, and how a non-agent call
gets a validated object back instead of a string that still needs parsing.

What this file used to be about was quota. The previous provider metered a free
tier per model *name*, so six agents sharing one name queued behind one bucket,
and the fix was to hand each parallel agent a different model name. That whole
concern is gone - Bedrock meters per account and region - and what replaces it
is ordinary retry with backoff, which the SDK does itself.

What survives the move are the two guards, both added after live failures and
both still exactly as necessary:

  max_output_tokens  the model once emitted 21 KB in which a free-text field ran
                     away and the structured list came back empty. The funnel
                     read that as "this task is atomic" and paid the whole budget
                     in one transaction against the root mandate.
  timeout            a hang is not an exception. A decompose call that never
                     returns leaves a run silently not growing: no error, no
                     terminal event, and a console that cannot tell "thinking"
                     from "dead".

See PORTING.md §D1-D3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel

from pocketchange import bedrock


def resilient(model_id: str):
    """A Strands model for an agent node, pointed at Bedrock.

    Retry lives inside the SDK rather than in a hand-built policy object here,
    which is the one piece of the old model factory that does not need
    replacing - it needs deleting.
    """
    from strands.models import BedrockModel

    return BedrockModel(model_id=model_id, region_name=bedrock.region())


def fleet(count: int) -> tuple[str, ...]:
    """One model id per parallel agent. See `bedrock.spread_models`."""
    return bedrock.spread_models(count)


# --- the chain idiom ------------------------------------------------------
#
# A node's model call is declared as one thing - the prompt, the model and the
# output schema together - and invoked where it is used:
#
#     CHAIN = StructuredChain(system=PROMPT, schema=Split)
#     result = CHAIN.invoke({"errand": ...})
#
# The shape is worth keeping. What changed underneath is that the schema is now
# enforced by the API rather than described in the prompt and hoped for.


@dataclass
class StructuredChain:
    """A system prompt bound to a model and a response schema.

    `invoke(variables)` formats the prompt, calls the model, and returns a
    validated pydantic object - never a string that still needs parsing, and
    never a half-built object standing in for an answer that did not arrive.
    """

    system: str
    schema: type[BaseModel]
    # Empty means "whatever the tier picks". An explicit id overrides the tier,
    # because someone who names a model should get that model rather than a
    # silent substitution.
    model: str = ""
    human: str = ""
    max_output_tokens: int = 2048
    # "judge" routes to the small trusted model; "work" to the reasoning one.
    tier: str = "work"
    timeout_ms: int = 45_000

    def model_id(self) -> str:
        if self.model:
            return self.model
        return bedrock.JUDGE_MODEL if self.tier == "judge" else bedrock.WORK_MODEL

    def invoke(self, variables: dict) -> BaseModel:
        prompt = self.system.format(**variables)
        if self.human:
            prompt = f"{prompt}\n\n{self.human.format(**variables)}"

        try:
            return bedrock.structured(
                prompt,
                self.schema,
                model=self.model_id(),
                max_tokens=self.max_output_tokens,
                timeout=self.timeout_ms / 1000,
            )
        except Exception as primary:  # noqa: BLE001
            from pocketchange import providers

            if not providers.configured():
                raise
            try:
                return self._fallback(prompt)
            except providers.NoProviderAnswered:
                raise primary

    def _fallback(self, prompt: str) -> BaseModel:
        """Ask somewhere else.

        Bedrock is the primary and the only one that enforces the schema, so the
        answer from here is validated locally instead. A provider that returns
        the wrong shape fails exactly like one that returned nothing, which is
        the point: a caller that cannot tell "no answer" from "the answer was
        nothing" is how a degenerate response became a confident decision once
        already in this project.
        """
        from pocketchange import providers

        schema_hint = json.dumps(self.schema.model_json_schema())
        asked = (f"{prompt}\n\nReply with a single JSON object matching this "
                 f"JSON Schema. No prose, no code fence.\n{schema_hint}")
        _name, raw = providers.json_chat(asked, max_tokens=self.max_output_tokens)
        body = raw.strip()
        if body.startswith("```"):
            body = body.split("```")[1].removeprefix("json").strip()
        return self.schema.model_validate_json(body)
