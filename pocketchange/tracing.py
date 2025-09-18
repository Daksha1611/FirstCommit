"""Langfuse/OTel setup and pocketchange.* span attributes.

Two different things get called "observing" in this project, and conflating them
causes confusion later:

  monitor.py   a control mechanism *inside the product*. It gates real actions
               and can refuse them.
  this module  developer observability. It watches, records, and changes nothing.

Langfuse rather than LangSmith for three reasons, only one of which is technical:
Langfuse publishes a maintained ADK integration, it is open source so running the
adversarial harness thousands of times is not metered, and it is the employer
this project is partly aimed at. On tooling merit alone the two are close.

The value here is not that LLM calls get traced - ADK does that on its own. It is
that each span also carries the *security* story: which token, how deep in the
delegation chain, what budget remained, which check refused. A trace that shows
only prompts and completions cannot explain why a payment did not happen.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

_ENABLED: bool | None = None
_CLIENT: Any = None

PREFIX = "pocketchange"


def _client() -> Any:
    """The Langfuse client, or None if it is not configured.

    Tracing must never be load-bearing. A missing key, an unreachable host or a
    version mismatch degrades to no tracing - it does not stop a payment being
    correctly refused.
    """
    global _ENABLED, _CLIENT
    if _ENABLED is not None:
        return _CLIENT

    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        _ENABLED, _CLIENT = False, None
        return None
    try:
        from langfuse import get_client

        client = get_client()
        _ENABLED, _CLIENT = True, client
    except Exception:  # noqa: BLE001 - any import or config failure means "off"
        _ENABLED, _CLIENT = False, None
    return _CLIENT


def is_enabled() -> bool:
    return _client() is not None


def instrument_agents() -> bool:
    """Turn on the agent SDK's own telemetry if it is installed.

    Strands emits OpenTelemetry GenAI semantic conventions itself, so every tool
    call and model completion becomes a span without bespoke code - the same
    bargain the previous framework's instrumentor offered, with one less package
    in between.
    """
    if not is_enabled():
        return False
    try:
        from strands.telemetry import StrandsTelemetry

        StrandsTelemetry().setup_otlp_exporter()
        return True
    except Exception:  # noqa: BLE001
        return False


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A span carrying pocketchange.* attributes.

    Falls through silently when tracing is off, so call sites need no guards.
    """
    client = _client()
    if client is None:
        yield None
        return

    tagged = {f"{PREFIX}.{k}": v for k, v in attributes.items() if v is not None}
    try:
        with client.start_as_current_observation(
            as_type="span", name=name, metadata=tagged
        ) as observation:
            yield observation
    except Exception:  # noqa: BLE001
        yield None


def annotate(**attributes: Any) -> None:
    """Add attributes to whatever span is currently open.

    Used by the gateway to record the outcome of a check once it is known -
    which check refused, how much budget was left, what the monitor said.
    """
    client = _client()
    if client is None:
        return
    tagged = {f"{PREFIX}.{k}": v for k, v in attributes.items() if v is not None}
    try:
        client.update_current_span(metadata=tagged)
    except Exception:  # noqa: BLE001
        pass


def flush() -> None:
    """Send anything buffered. Scripts should call this before exiting."""
    client = _client()
    if client is not None:
        try:
            client.flush()
        except Exception:  # noqa: BLE001
            pass


def trace_url() -> str | None:
    client = _client()
    if client is None:
        return None
    try:
        return client.get_trace_url()
    except Exception:  # noqa: BLE001
        return None
