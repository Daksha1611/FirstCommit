"""The model call at a branch of the funnel — one per branch, not one per node.

    funnel.run(decompose=make_decomposer(), execute=...)

`pocketchange/funnel.py` does not import this. That is deliberate: the trusted
layer takes a callable and never learns whether a model, a script or a test
produced the answer. Every bound is checked on the result regardless of where it
came from, which is what lets the 121-node demo run offline and the same code
run against Gemini unchanged.

The model's answer is treated as a proposal, not a decision:

  * budget shares are RESCALED to fit, never trusted to sum correctly
  * a share of zero or less is dropped
  * "specific" without a named supplier falls back to "catalogue"
  * everything else - fan-out, cycles, conservation, depth, node budget - is
    caught by the funnel's own bounds, after this returns

Structured output on this model family has been flaky in this project before: a
plan node once returned an empty list twice in a row and two different causes
were confidently published before the real one (sampling variance) was found. So
this retries, and says so rather than returning an empty decomposition that
would look like "the task is atomic".
"""

from __future__ import annotations

from typing import Callable, List, Literal, Optional

from pydantic import BaseModel, Field

from agent.models import StructuredChain
from agent.prompts import DECOMPOSE_HUMAN, DECOMPOSE_PROMPT
from pocketchange import funnel
from pocketchange.policy import RUPEE

ATTEMPTS = 3


class ProposedSubTask(BaseModel):
    description: str = Field(
        description="What this sub-task is, in one plain phrase. Never a restatement of the parent."
    )
    budget_rupees: int = Field(
        description="Share of the parent's budget, in whole rupees.", ge=0)
    sourcing: Optional[Literal["catalogue", "specific", "best"]] = Field(
        default=None,
        description=(
            "Leave unset to inherit the parent task's sourcing. Set one only to "
            "NARROW it - the funnel clamps anything wider back to the parent's."
        ))
    supplier: Optional[str] = Field(
        default=None, description="Required when sourcing is 'specific'. Never invented.")


class Decomposition(BaseModel):
    subtasks: List[ProposedSubTask] = Field(
        default_factory=list,
        description="Empty if the task is already atomic - a legitimate answer.")
    # Capped hard. Unbounded free text beside a structured list is where this
    # model runs away: it filled 21 KB of rationale and returned no sub-tasks at
    # all, which the funnel then read as "atomic" and paid in one transaction.
    reasoning: str = Field(
        default="", max_length=180,
        description="At most one short sentence. Never more.")


decompose_chain = StructuredChain(
    system=DECOMPOSE_PROMPT,
    human=DECOMPOSE_HUMAN,
    schema=Decomposition,
)


def _settle(proposed: List[ProposedSubTask], budget_paise: int) -> list[funnel.SubTask]:
    """Turn a proposal into sub-tasks that fit, or refuse to guess.

    Rescaling rather than refusing on over-allocation is a deliberate asymmetry.
    A model that allocates 110% has made an arithmetic slip about a division we
    can do ourselves; a model that names a supplier it was not given has invented
    a fact, and there is nothing to rescale.
    """
    kept = [p for p in proposed if p.budget_rupees > 0]
    if not kept:
        return []

    total_paise = sum(p.budget_rupees * RUPEE for p in kept)
    scale = min(1.0, budget_paise / total_paise) if total_paise else 0.0

    out: list[funnel.SubTask] = []
    for p in kept:
        share = int(p.budget_rupees * RUPEE * scale)
        if share <= 0:
            continue
        sourcing = p.sourcing
        supplier = (p.supplier or "").strip() or None
        if sourcing == "specific" and not supplier:
            # A missing name is not a narrower instruction, it is an absent one.
            # None hands the decision back to the parent rather than inventing a
            # catalogue that may not be what was asked for.
            sourcing = None
        out.append(funnel.SubTask(
            description=p.description.strip(),
            budget_paise=share,
            sourcing=sourcing,
            supplier=supplier if sourcing == "specific" else None,
        ))
    return out


def make_decomposer(bounds: funnel.Bounds | None = None,
                    chain=None) -> Callable[[funnel.TaskNode], list[funnel.SubTask]]:
    """Build the callable `Funnel.run` expects."""
    limits = bounds or funnel.Bounds()
    link = chain or decompose_chain

    def decompose(node: funnel.TaskNode) -> list[funnel.SubTask]:
        variables = {
            "description": node.description,
            "budget_rupees": node.budget_paise // RUPEE,
            "depth": node.depth,
            "max_depth": limits.max_depth,
            "max_fanout": limits.max_fanout,
            "sourcing": node.sourcing,
        }
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            try:
                result = link.invoke(variables)
            except Exception as exc:  # noqa: BLE001 - the model is not ours
                last = exc
                continue
            settled = _settle(result.subtasks, node.budget_paise)
            if settled:
                return settled

            if not result.subtasks:
                # "Atomic" is a real answer, but one flaky sample of it collapses
                # the whole tree - the node pays its entire budget in a single
                # transaction. Observed live: a Rs 6,00,000 studio fit-out came
                # back empty, and the same prompt called again returned four
                # sensible streams. So a node with room to divide has to say
                # atomic TWICE before we believe it.
                roomy = node.budget_paise >= 2 * limits.decompose_floor_paise
                if not roomy or attempt >= ATTEMPTS - 1:
                    return []
                last = ValueError("empty decomposition for a node well above the floor")
                continue

            last = ValueError("every proposed sub-task had a zero budget")
        if last is not None:
            raise last
        return []

    return decompose
