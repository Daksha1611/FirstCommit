"""Somewhere else to ask when Bedrock will not answer.

This began as an answer to a free tier of 15 requests a minute, which was the
binding constraint on the project before the move to Bedrock - not correctness,
not design, quota. A demo that dies mid-judging because a decomposition hit a
429 is a demo that failed for the least interesting possible reason.

Bedrock removes the quota cliff, so the honest question is why this file
survives the port at all. Two reasons. A throttle is still a throttle, and the
monitor fails OPEN when it cannot reach a model - so the window in which the
second layer silently stops existing is worth narrowing with something that
does not share Bedrock's failure modes. And keeping a non-AWS path reachable is
what lets the project keep claiming the enforcement layer is provider-neutral:
the claim is cheap to make and expensive to verify once the alternative is
deleted.

So this is a chain, not a spare. Measured while writing it, with real keys:

    groq        OK  0.7s
    openrouter  OK  3.0s
    cerebras    HTTP 402  payment required
    sambanova   HTTP 429  rate limit exceeded

Two of four were unavailable at that moment. A single fallback would have been a
coin toss; the chain simply walks past both.

All four speak the OpenAI chat-completions shape, so one adapter covers them.
Model names are pinned to what each provider actually served when this was
written - `GET /models` on each, not a blog post - and every one is overridable,
because those lists churn. A wrong name returns 400 and the chain moves on,
which is the failure mode you want.

This lives in the trusted layer because the monitor uses it too, and the monitor
must never import from `agent/`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str
    env: str
    base: str
    model: str
    # Some gateways want a referer; OpenRouter is happier with one.
    extra_headers: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        return os.environ.get(self.env, "").strip()

    def model_name(self) -> str:
        return os.environ.get(f"{self.env}_MODEL", "").strip() or self.model


# Order is deliberate: fastest first, measured rather than assumed.
FALLBACKS: tuple[Provider, ...] = (
    Provider("groq", "GROQ_API_KEY",
             "https://api.groq.com/openai/v1", "openai/gpt-oss-20b"),
    Provider("openrouter", "OPENROUTER_API_KEY",
             "https://openrouter.ai/api/v1", "openai/gpt-oss-20b",
             (("HTTP-Referer", "https://github.com/Somay-kousis/Pocket-Change"),
              ("X-Title", "Pocket Change"))),
    Provider("cerebras", "CEREBRAS_API_KEY",
             "https://api.cerebras.ai/v1", "gpt-oss-120b"),
    Provider("sambanova", "SAMBANOVA_API_KEY",
             "https://api.sambanova.ai/v1", "Meta-Llama-3.3-70B-Instruct"),
)


class NoProviderAnswered(Exception):
    """Every configured provider refused, timed out, or is not configured."""


def configured() -> list[Provider]:
    return [p for p in FALLBACKS if p.key]


def json_chat(prompt: str, *, max_tokens: int = 2048,
              timeout: float = 30.0) -> tuple[str, str]:
    """Ask each configured provider in turn for one JSON object.

    Returns (provider name, raw text). Raises NoProviderAnswered if none did -
    deliberately, rather than returning something empty: a caller that cannot
    tell "no answer" from "the answer was nothing" is how a degenerate response
    became a confident decision once already in this project.
    """
    import httpx

    attempts: list[str] = []
    for provider in configured():
        headers = {
            "Authorization": f"Bearer {provider.key}",
            "Content-Type": "application/json",
            **dict(provider.extra_headers),
        }
        body = {
            "model": provider.model_name(),
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        try:
            response = httpx.post(f"{provider.base}/chat/completions",
                                  headers=headers, json=body, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - a dead provider is not an error here
            attempts.append(f"{provider.name}: {type(exc).__name__}")
            continue

        if response.status_code != 200:
            attempts.append(f"{provider.name}: HTTP {response.status_code}")
            continue

        try:
            text = response.json()["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            attempts.append(f"{provider.name}: unreadable response")
            continue

        if not (text or "").strip():
            attempts.append(f"{provider.name}: empty")
            continue
        return provider.name, text

    raise NoProviderAnswered(
        "no fallback provider answered" + (f" ({'; '.join(attempts)})" if attempts else "")
    )


def json_object(prompt: str, **kw) -> tuple[str, dict]:
    """json_chat, parsed. Tolerates a fenced block, which several of these wrap in."""
    name, text = json_chat(prompt, **kw)
    body = text.strip()
    if body.startswith("```"):
        body = body.split("```")[1].removeprefix("json").strip()
    return name, json.loads(body)
