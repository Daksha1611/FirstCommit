"""The broker, and the payers it creates.

The one agent here that alters the authority graph rather than acting inside it.
It holds `delegate` and mints a `pay` mandate per seller, each capped at that
seller's subtotal.

A caveat worth carrying in the code rather than only in a doc: the broker's token
necessarily also permits `pay`, because attenuation is monotonic and a token
cannot confer a capability it lacks. The broker is stopped from spending by the
gateway, on identity - see `pocketchange.token.is_broker`, and the Cedar policy
that now states the rule in one readable place. The per-seller caps are
cryptographic; the broker/payer split is policy.
"""

from __future__ import annotations

from agent.prompts import BROKER_INSTRUCTION, SUBPAYER_INSTRUCTION
from agent.runtime import build_agent
from agent.tools import ToolSurface


def broker_node(tools: ToolSurface, model_id: str):
    """Splits the chosen cart and mints one mandate per seller."""
    return build_agent(
        name="broker",
        description="Splits a cart by seller and mints a capped mandate for each.",
        instruction=BROKER_INSTRUCTION,
        model_id=model_id,
        functions=tools.broker_functions(),
    )


def sub_payer_node(tools: ToolSurface, model_id: str):
    """Pays each seller with that seller's own narrow mandate."""
    return build_agent(
        name="sub_payer",
        description="Settles each seller's share using its own capped mandate.",
        instruction=SUBPAYER_INSTRUCTION,
        model_id=model_id,
        functions=tools.sub_payer_functions(),
    )
