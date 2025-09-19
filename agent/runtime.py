"""Agent construction on the Strands Agents SDK.

One place that knows how a node becomes an agent, so the node modules stay what
they always were: a name, a brief, a tool list, and a sentence about why that
particular surface and no wider.

Strands was already in this repository before the port, in `strands_buyer.py`,
for a deliberately awkward reason - it was a second agent framework kept around
to test the claim that enforcement is separable from the reasoning layer. If
swapping the framework had required touching the gateway, the claim was false.
It did not, so the experiment is now the only framework and the argument it was
built to make is the reason this port was cheap.

Two things the previous framework supplied that are written out by hand here:

  * the loop and the fan-out. ADK expressed the topology through composition -
    `LoopAgent` repeating its sub-agents, `ParallelAgent` running its own
    concurrently - so the graph *was* the nesting. Strands has no equivalent
    wrapper, and the honest replacement is an explicit loop in `graph/builder`,
    which reads better than the nesting did.
  * tool escalation. ADK exited a LoopAgent when a sub-agent set
    `tool_context.actions.escalate`. The loop is now ordinary Python and can
    simply read `tools.finished`, so `finish()` lost its framework argument.
"""

from __future__ import annotations

from typing import Any, Callable

from pocketchange import bedrock


def as_tools(functions: list[Callable]) -> list:
    """Wrap bound ToolSurface methods as Strands tools.

    The wrapping happens here rather than in `agent/tools.py` on purpose: the
    tool surface is the trust boundary and it must stay readable without knowing
    which agent framework is in use this month. It describes *what may be done*;
    this file decides how that is handed to a model.
    """
    from strands import tool

    return [tool(function) for function in functions]


def build_agent(
    *,
    name: str,
    description: str,
    instruction: str,
    model_id: str,
    functions: list[Callable],
):
    """One Strands agent, on Bedrock, holding exactly the tools it is given."""
    from strands import Agent

    return Agent(
        name=name,
        description=description,
        system_prompt=instruction,
        model=bedrock_model(model_id),
        tools=as_tools(functions),
    )


def bedrock_model(model_id: str):
    from strands.models import BedrockModel

    return BedrockModel(model_id=model_id, region_name=bedrock.region())


def said(result: Any) -> str:
    """The text an agent produced, however the SDK chose to wrap it.

    Defensive because the transcript is what the console renders and what the
    audit trail is read against - a result shape this does not recognise should
    degrade to its string form, not to an empty line that makes a round look
    like it never happened.
    """
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        parts = [
            block.get("text", "")
            for block in message.get("content", [])
            if isinstance(block, dict)
        ]
        joined = "".join(parts).strip()
        if joined:
            return joined
    return str(result).strip()
