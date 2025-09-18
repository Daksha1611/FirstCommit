"""Somewhere else to ask.

Gemini's 15-per-minute free tier has been the binding constraint on this project
throughout - not correctness, quota. Measured while writing this, with real keys,
two of the four fallbacks were unavailable at that exact moment (Cerebras wanted
payment, SambaNova was rate limited). That is the argument for a chain rather
than a spare.

Nothing here touches the network.
"""

import json

import pytest

from pocketchange import providers


@pytest.fixture
def keys(monkeypatch):
    def configure(**pairs):
        for p in providers.FALLBACKS:
            monkeypatch.delenv(p.env, raising=False)
            monkeypatch.delenv(f"{p.env}_MODEL", raising=False)
        for name, value in pairs.items():
            monkeypatch.setenv(name, value)
    return configure


class FakeHTTP:
    """Scripted responses, in the order providers are tried."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw["json"]["model"]))
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        class R:
            status_code = outcome[0]
            text = outcome[1]

            def json(self):
                return {"choices": [{"message": {"content": outcome[1]}}]}

        return R()


@pytest.fixture
def http(monkeypatch):
    import httpx

    def install(script):
        fake = FakeHTTP(script)
        monkeypatch.setattr(httpx, "post", fake.post)
        return fake
    return install


OK = (200, '{"answer": "yes"}')


# --- who is configured ------------------------------------------------------


def test_nothing_configured_means_no_providers(keys):
    keys()
    assert providers.configured() == []


def test_only_providers_with_a_key_are_tried(keys):
    keys(GROQ_API_KEY="g", SAMBANOVA_API_KEY="s")
    assert [p.name for p in providers.configured()] == ["groq", "sambanova"]


def test_a_model_can_be_overridden_without_touching_code(keys):
    """Those lists churn - Cerebras served a dozen models one month and two the
    next - so a pinned default has to be replaceable from the environment."""
    keys(GROQ_API_KEY="g", GROQ_API_KEY_MODEL="something/else")
    groq = providers.configured()[0]
    assert groq.model_name() == "something/else"


# --- the chain --------------------------------------------------------------


def test_the_first_provider_that_answers_wins(keys, http):
    keys(GROQ_API_KEY="g", OPENROUTER_API_KEY="o")
    fake = http([OK])
    name, raw = providers.json_chat("hello")
    assert name == "groq"
    assert json.loads(raw) == {"answer": "yes"}
    assert len(fake.calls) == 1


def test_it_walks_past_a_provider_that_wants_payment(keys, http):
    """Exactly what Cerebras did: HTTP 402."""
    keys(GROQ_API_KEY="g", OPENROUTER_API_KEY="o")
    fake = http([(402, "payment required"), OK])
    name, _ = providers.json_chat("hello")
    assert name == "openrouter"
    assert len(fake.calls) == 2


def test_it_walks_past_a_rate_limit(keys, http):
    """Exactly what SambaNova did: HTTP 429."""
    keys(GROQ_API_KEY="g", OPENROUTER_API_KEY="o")
    http([(429, "rate limit exceeded"), OK])
    assert providers.json_chat("hello")[0] == "openrouter"


def test_it_walks_past_a_provider_that_never_responds(keys, http):
    keys(GROQ_API_KEY="g", OPENROUTER_API_KEY="o")
    http([TimeoutError("timed out"), OK])
    assert providers.json_chat("hello")[0] == "openrouter"


def test_an_empty_answer_is_not_an_answer(keys, http):
    """A provider returning "" must not read as a successful empty result - that
    conflation once made a degenerate reply into a confident decision."""
    keys(GROQ_API_KEY="g", OPENROUTER_API_KEY="o")
    http([(200, "   "), OK])
    assert providers.json_chat("hello")[0] == "openrouter"


def test_when_everyone_refuses_it_says_so_rather_than_returning_nothing(keys, http):
    keys(GROQ_API_KEY="g", OPENROUTER_API_KEY="o")
    http([(402, "no"), (429, "no")])
    with pytest.raises(providers.NoProviderAnswered) as exc:
        providers.json_chat("hello")
    # The message has to name what happened, or a quota outage looks like a bug.
    assert "groq: HTTP 402" in str(exc.value)
    assert "openrouter: HTTP 429" in str(exc.value)


def test_with_nothing_configured_it_refuses_immediately(keys, http):
    keys()
    fake = http([])
    with pytest.raises(providers.NoProviderAnswered):
        providers.json_chat("hello")
    assert fake.calls == []


def test_a_fenced_reply_is_still_parsed(keys, http):
    keys(GROQ_API_KEY="g")
    http([(200, '```json\n{"answer": "yes"}\n```')])
    name, obj = providers.json_object("hello")
    assert obj == {"answer": "yes"}

