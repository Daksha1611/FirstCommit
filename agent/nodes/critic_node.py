"""A model's opinion on a decomposition, before authority is minted from it.

`pocketchange/funnel.py` takes a callable and never learns whether a model
produced the answer - the same arrangement as the decomposer and the searcher.
That is what keeps `eval/funnel.py` able to build a 121-node tree with no model
configured at all, and it is what makes this layer swappable for a scripted one
in tests.

Advisory by construction: everything here that can go wrong resolves to
"proceed". The funnel treats an exception as approval, so a provider outage
leaves a run identical to one with no critic. Judgement is a second layer here,
never a prerequisite for spending.
"""

from __future__ import annotations

from typing import Callable, List

from pydantic import BaseModel, Field

from agent.models import StructuredChain
from agent.prompts import CRITIC_PROMPT
from pocketchange import funnel
from pocketchange.policy import RUPEE


class Critique(BaseModel):
    approve: bool = Field(description="False only when something is genuinely wrong.")
    reason: str = Field(default="", description="One short sentence.", max_length=180)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# Judgement tier: this decides whether authority gets minted at all.
critic_chain = StructuredChain(system=CRITIC_PROMPT, schema=Critique, tier="judge")


def _render(subtasks: List[funnel.SubTask]) -> str:
    lines = []
    for i, s in enumerate(subtasks, start=1):
        where = f" [{s.sourcing}]" if s.sourcing else ""
        supplier = f" via {s.supplier}" if s.supplier else ""
        lines.append(f"  {i}. {s.description} - {s.budget_paise // RUPEE:,} rupees{where}{supplier}")
    return "\n".join(lines)


def make_critic(intent: str, chain=None) -> Callable[..., funnel.CriticVerdict]:
    """Build the callable `Funnel(critic=...)` expects.

    `intent` is the mandate's stated purpose - what the human actually
    authorised. Without it the critic can only judge a split against its parent,
    which misses the drift that matters: a tree that divides beautifully and
    walks steadily away from what was asked for.
    """
    link = chain or critic_chain

    def critic(node: funnel.TaskNode, subtasks: List[funnel.SubTask]) -> funnel.CriticVerdict:
        result = link.invoke({
            "intent": intent,
            "description": node.description,
            "budget_rupees": f"{node.budget_paise // RUPEE:,}",
            "depth": node.depth,
            "subtasks": _render(subtasks),
        })
        return funnel.CriticVerdict(
            approve=result.approve,
            reason=result.reason or ("approved" if result.approve else "refused"),
            confidence=result.confidence,
        )

    return critic
