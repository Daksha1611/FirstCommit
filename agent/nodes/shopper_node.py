"""The shoppers. Three strategies, one request, run concurrently.

RabbitHole's perspective_node.py parameterises one function over ten ids and
exports p1_node..p10_node. Same shape here over three strategies - the node is one
function, the strategies are data, and the graph wires whichever it wants.

Trust position: these are the only agents that read seller-controlled product
descriptions, and they hold no payment capability. Widening the fan-out widens the
untrusted layer without widening its authority, which is exactly what sibling
delegation was chosen to make possible.
"""

from __future__ import annotations

from agent.prompts import STRATEGY_BRIEFS, shopper_instruction
from agent.runtime import build_agent
from agent.tools import ToolSurface


def shopper_node(tools: ToolSurface, strategy: str, model_id: str):
    """One shopper, bound to one strategy."""
    if strategy not in STRATEGY_BRIEFS:
        raise KeyError(f"unknown strategy: {strategy}. Known: {sorted(STRATEGY_BRIEFS)}")
    return build_agent(
        name=f"shopper_{strategy}",
        description=f"Assembles a cart, optimising for: {STRATEGY_BRIEFS[strategy][:60]}",
        instruction=shopper_instruction(strategy),
        model_id=model_id,
        functions=tools.shopper_functions(),
    )


def all_shoppers(tools: ToolSurface, models: tuple[str, ...]) -> list:
    """One shopper per strategy.

    They share a model id now. On the previous provider each got a different one,
    because quota was metered per model name and three agents sharing a name
    queued behind one bucket - a real constraint that Bedrock does not have.
    Spreading across models of different capability for no benefit would be
    strictly worse, so the fan-out is now three agents of equal ability
    disagreeing about strategy, which is what it was always meant to be.
    """
    return [
        shopper_node(tools, strategy, models[i % len(models)])
        for i, strategy in enumerate(STRATEGY_BRIEFS)
    ]
