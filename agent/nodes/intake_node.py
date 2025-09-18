"""Intake: classify the request, extract what the person actually specified.

RabbitHole's query_refine_node reads raw user input once, rewrites it into the
form the graph works in, and pulls out any explicit commands the user gave -
number of perspectives, judiciary type - leaving them null when unstated. Same
job here: decide which machine handles this, and record only what was actually
asked for.

Runs once per errand. The guard at the top is the same idempotency guard the
moderator uses, because a cycle back through the graph must not re-classify.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from agent.models import StructuredChain
from agent.prompts import INTAKE_CLASSIFIER_PROMPT, INTAKE_HUMAN

TaskKind = Literal["errand", "target", "standing"]

# A second, orthogonal axis. Scope says how much work the request is; sourcing
# says where the goods come from, and that decides whether the open web - and
# with it a far more hostile class of text - is involved at all.
#
#   catalogue  our own SKUs; no search
#   specific   a supplier the person named; go there, do not shop around
#   best       find the best source, which means searching and comparing
#
# Kept apart from task_kind on purpose. "Buy four laptops from Meridian" and
# "buy four laptops from whoever is cheapest" are the same scope and completely
# different amounts of exposure.
Sourcing = Literal["catalogue", "specific", "best"]


class RequestedItem(BaseModel):
    name: str = Field(description="The item as the person named it, not a SKU.")
    quantity: Optional[str] = Field(
        default=None,
        description="Quantity exactly as stated, e.g. '4 kg'. None if unstated.",
    )


class IntakeCommands(BaseModel):
    budget_paise: Optional[int] = Field(
        default=None,
        description="Cap in paise, only if the person stated one. Never invent it.",
    )
    deadline: Optional[str] = Field(
        default=None,
        description="Any time limit the person gave, e.g. 'within the hour'. None if unstated.",
    )
    shops: Optional[List[str]] = Field(
        default=None,
        description="Specific shops named by the person. None if unstated.",
    )


class IntakeOutput(BaseModel):
    task_kind: TaskKind = Field(
        description="errand for a specific purchase, target for a goal, standing for a recurring instruction."
    )
    goal: str = Field(
        description="One plain sentence describing what success looks like for the buyer."
    )
    items: List[RequestedItem] = Field(
        default_factory=list,
        description="Items explicitly named. Empty for a target the agent must work out itself.",
    )
    commands: IntakeCommands = Field(
        description="Constraints the person stated. Fields stay null when unstated."
    )
    sourcing: Sourcing = Field(
        default="catalogue",
        description=(
            "catalogue if no source is implied, specific if the person named a "
            "supplier, best if they asked for the best/cheapest source and it "
            "must be searched for. Default to catalogue when unsure - searching "
            "the open web is the option that adds risk."
        ),
    )
    supplier: Optional[str] = Field(
        default=None,
        description="The supplier named, only when sourcing is 'specific'. Never invented.",
    )
    reasoning: str = Field(
        description="One short sentence on why this task_kind and not the others."
    )


intake_chain = StructuredChain(
    system=INTAKE_CLASSIFIER_PROMPT,
    human=INTAKE_HUMAN,
    schema=IntakeOutput,
)


def _settle_sourcing(result: IntakeOutput) -> str:
    """The model may choose the sourcing; it may not choose it incoherently.

    Same shape as settle_tier in agent/tiers.py: rules constrain the model's
    answer rather than trusting it. "specific" without a named supplier is not a
    narrower instruction, it is a missing one, and treating it as specific would
    send the buyer to a supplier that was never named.
    """
    if result.sourcing == "specific" and not (result.supplier or "").strip():
        return "catalogue"
    return result.sourcing


def build_task(result: IntakeOutput) -> dict:
    """The state slice an intake produces."""
    return {
        "task_kind": result.task_kind,
        "goal": result.goal,
        "items": [item.model_dump() for item in result.items],
        "commands": result.commands.model_dump(),
        "sourcing": _settle_sourcing(result),
        "supplier": result.supplier,
        "intake_reasoning": result.reasoning,
    }


def intake_node(state: dict) -> dict:
    """Classify the request once. Returns only the keys it sets."""
    # Intake runs once. A cycle back through the graph must not re-classify -
    # the same guard the moderator uses to avoid recreating its perspectives.
    if state.get("task_kind"):
        return {}

    request = state.get("request", "")
    if not request.strip():
        return {
            "task_kind": "errand",
            "goal": "",
            "items": [],
            "commands": {},
            "sourcing": "catalogue",
            "supplier": None,
            "intake_reasoning": "empty request",
        }

    result = intake_chain.invoke({"request": request})
    return build_task(result)
