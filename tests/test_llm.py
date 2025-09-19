"""StructuredChain: the model boundary, previously untested.

It gained two guards after a live failure, and neither was covered:

  max_output_tokens  the model emitted 21 KB in which a free-text field ran away
                     and the structured list came back empty. The funnel read
                     that as "this task is atomic" and paid the whole budget in
                     one transaction using the root mandate.
  timeout            a hang is not an exception. A decompose call that never
                     returns leaves a run silently not growing.

Both survived the move to Bedrock and both are still asserted here, now against
the arguments actually sent to `messages.parse` rather than to a vendor config
object.

No model is called; the client is stubbed one layer lower than before - at
`bedrock.client`, so the code under test still builds the real request.
"""

import pytest
from pydantic import BaseModel, Field

from agent.models.llm import StructuredChain
from pocketchange import bedrock


class Answer(BaseModel):
    items: list[str] = Field(default_factory=list)
    note: str = ""


@pytest.fixture
def model(monkeypatch):
    """Capture the request StructuredChain builds, and script the reply."""
    captured = {}

    class Messages:
        def parse(self, **kw):
            captured.update(kw)
            if captured.get("raises"):
                raise captured["raises"]

            class Parsed:
                parsed_output = captured.get("parsed")
                stop_reason = "end_turn"

            return Parsed()

    class Client:
        def __init__(self, timeout=None):
            captured["timeout"] = timeout
            self.messages = Messages()

    monkeypatch.setattr(bedrock, "available", lambda: True)
    monkeypatch.setattr(bedrock, "client", lambda timeout=45.0: Client(timeout))
    return captured


def chain(**kw):
    return StructuredChain(system="Do {thing}.", schema=Answer, **kw)


# --- the two guards --------------------------------------------------------


def test_a_reply_ceiling_is_always_sent(model):
    """Without one, a runaway reply arrives as a well-formed empty answer."""
    model["parsed"] = Answer(items=["a"])
    chain().invoke({"thing": "something"})
    assert model["max_tokens"] > 0


def test_a_timeout_is_always_sent(model):
    model["parsed"] = Answer()
    chain().invoke({"thing": "something"})
    assert model["timeout"] > 0


def test_both_ceilings_are_configurable(model):
    model["parsed"] = Answer()
    chain(max_output_tokens=99, timeout_ms=1234).invoke({"thing": "x"})
    assert model["max_tokens"] == 99
    assert model["timeout"] == pytest.approx(1.234)


# --- the schema is enforced, not described ---------------------------------


def test_the_schema_is_sent_to_the_api(model):
    """The material difference from the previous provider path.

    That one described the JSON Schema in the prompt and validated whatever came
    back. Here the shape is a constraint on generation, so a reply that does not
    fit is not something the API can return.
    """
    model["parsed"] = Answer()
    chain().invoke({"thing": "x"})
    assert model["output_format"] is Answer


def test_a_parsed_object_is_returned_directly(model):
    model["parsed"] = Answer(items=["one", "two"])
    assert chain().invoke({"thing": "x"}).items == ["one", "two"]


def test_nothing_parsed_raises_instead_of_returning_an_empty_answer(model):
    """This is the important one. Returning Answer() here is exactly how a
    truncated runaway became a confident 'nothing to divide'."""
    model["parsed"] = None
    with pytest.raises(Exception):
        chain().invoke({"thing": "x"})


# --- tiers ------------------------------------------------------------------


def test_the_judge_tier_routes_to_the_small_model(model):
    model["parsed"] = Answer()
    chain(tier="judge").invoke({"thing": "x"})
    assert model["model"] == bedrock.JUDGE_MODEL


def test_naming_a_model_beats_the_tier(model):
    """Someone who names a model gets that model, not a silent substitution."""
    model["parsed"] = Answer()
    chain(model="anthropic.claude-haiku-4-5", tier="work").invoke({"thing": "x"})
    assert model["model"] == "anthropic.claude-haiku-4-5"


# --- prompt assembly -------------------------------------------------------


def test_the_human_turn_is_appended_and_both_are_formatted(model):
    model["parsed"] = Answer()
    StructuredChain(system="System: {thing}.", human="Now do {thing}.",
                    schema=Answer).invoke({"thing": "the split"})
    assert model["messages"][0]["content"] == "System: the split.\n\nNow do the split."


# --- somewhere else to ask --------------------------------------------------


def test_a_dead_primary_falls_back_and_still_validates(model, monkeypatch):
    from pocketchange import providers

    model["raises"] = RuntimeError("ThrottlingException")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setattr(providers, "json_chat",
                        lambda prompt, **kw: ("groq", '{"items": ["from groq"], "note": "ok"}'))

    assert chain().invoke({"thing": "x"}).items == ["from groq"]


def test_a_fallback_returning_the_wrong_shape_fails_like_no_answer(model, monkeypatch):
    """These providers take no schema, so the answer is validated here. A wrong
    shape must not slip through as a half-built object."""
    from pocketchange import providers

    model["raises"] = RuntimeError("down")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setattr(providers, "json_chat",
                        lambda prompt, **kw: ("groq", '{"items": "not a list"}'))

    with pytest.raises(Exception):
        chain().invoke({"thing": "x"})


def test_with_no_fallback_configured_the_original_failure_surfaces(model):
    """A caller must be told what actually went wrong, not that some unrelated
    fallback chain was empty."""
    model["raises"] = RuntimeError("ValidationException: model id not found")
    with pytest.raises(RuntimeError, match="model id not found"):
        chain().invoke({"thing": "x"})
