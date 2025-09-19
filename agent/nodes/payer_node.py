"""The payer. Holds the pay capability, never sees a product description.

The separation is structural rather than a matter of instruction: the payer's
token is a sibling of the shoppers', not a descendant, so it could not read a
listing and act on it even if it wanted to.
"""

from __future__ import annotations

from agent.prompts import PAYER_INSTRUCTION
from agent.runtime import build_agent
from agent.tools import ToolSurface


def payer_node(tools: ToolSurface, model_id: str):
    return build_agent(
        name="payer",
        description="Settles the adopted cart.",
        instruction=PAYER_INSTRUCTION,
        model_id=model_id,
        functions=tools.payer_functions(),
    )
