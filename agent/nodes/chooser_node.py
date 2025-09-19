"""The chooser: RabbitHole's judiciary, deciding between carts.

Three shoppers propose; one agent adopts. This is where the trade-off is actually
resolved, and it is the decision that makes the system an agent rather than a
pipeline - the outcome depends on judgement, not on ordering.

Never reads a listing. It sees totals, item counts and rationales.
"""

from __future__ import annotations

from agent.prompts import CHOOSER_INSTRUCTION
from agent.runtime import build_agent
from agent.tools import ToolSurface


def chooser_node(tools: ToolSurface, model_id: str):
    return build_agent(
        name="chooser",
        description="Compares competing carts against the budget and adopts one.",
        instruction=CHOOSER_INSTRUCTION,
        model_id=model_id,
        functions=tools.chooser_functions(),
    )
