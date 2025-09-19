"""The reviewer: does this errand continue, and if so how?

RabbitHole routes after its HITL node by reading state. Here the routing rule
lives in graph/route.py as ordinary code, and the reviewer is handed its verdict
rather than asked to infer one - so a scope refusal can never be retried because
a model felt optimistic.
"""

from __future__ import annotations

from agent.prompts import REVIEWER_INSTRUCTION
from agent.runtime import build_agent
from agent.tools import ToolSurface


def reviewer_node(tools: ToolSurface, model_id: str):
    return build_agent(
        name="reviewer",
        description="Decides whether the errand is finished or should re-plan.",
        instruction=REVIEWER_INSTRUCTION,
        model_id=model_id,
        functions=tools.reviewer_functions(),
    )
