"""Planner: a goal becomes a requisition.

The second intake branch. `errand` names its lines; `target` names an outcome and
something has to work out what that means.

The split of responsibility is the point:

  the model   decides what the goal implies, chooses SKUs, and ranks each line
              essential / important / optional
  the code    drops lines from the bottom until the total fits the budget

A model asked to stay under a number will say it did. Ranking is a judgement it
is good at; arithmetic under a constraint is one it should not be trusted with,
and does not need to be.

Trust position: the planner reads the catalogue, which is supplier-written text.
So it is untrusted and holds no payment capability — the same position as the
shoppers, for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from agent.models import StructuredChain
from agent.utils.catalogue import catalogue_prompt_block
from agent.prompts import PLANNER_PROMPT
from merchant import catalog

COMPANY = "Meridian Industries"

# The model sometimes returns reasoning and assumptions with no lines at all, on
# inputs it handles fine on the next call. Three attempts, then report failure.
PLAN_ATTEMPTS = 3

Priority = Literal["essential", "important", "optional"]
RANK: dict[str, int] = {"essential": 0, "important": 1, "optional": 2}


class PlannedLine(BaseModel):
    sku: str = Field(description="A sku from the catalogue. Never invented.")
    quantity: int = Field(ge=1, description="How many, after reasoning about the goal.")
    priority: Priority = Field(description="essential, important or optional")
    rationale: str = Field(description="One short clause on why this line exists.")


class Plan(BaseModel):
    lines: list[PlannedLine] = Field(default_factory=list)
    reasoning: str = Field(description="One or two sentences on how the goal was read.")
    assumptions: list[str] = Field(
        default_factory=list,
        description="Anything inferred that was not stated. Empty only if nothing was.",
    )


planner_chain = StructuredChain(system=PLANNER_PROMPT, schema=Plan)


@dataclass(frozen=True)
class Requisition:
    """A plan, cut to fit, with what was dropped recorded."""

    lines: tuple[PlannedLine, ...]
    dropped: tuple[PlannedLine, ...]
    total_paise: int
    budget_paise: int
    reasoning: str
    assumptions: tuple[str, ...]
    affordable: bool
    problem: str = ""

    def as_dict(self) -> dict:
        return {
            "lines": [line.model_dump() for line in self.lines],
            "dropped": [line.model_dump() for line in self.dropped],
            "total_paise": self.total_paise,
            "budget_paise": self.budget_paise,
            "affordable": self.affordable,
            "problem": self.problem,
            "reasoning": self.reasoning,
            "assumptions": list(self.assumptions),
        }

    def to_cart(self) -> dict[str, int]:
        rolled: dict[str, int] = {}
        for line in self.lines:
            rolled[line.sku] = rolled.get(line.sku, 0) + line.quantity
        return rolled


def _line_cost(line: PlannedLine) -> int:
    return catalog.get(line.sku).price_paise * line.quantity


def fit_to_budget(plan: Plan, budget_paise: int) -> Requisition:
    """Drop lines from the bottom until the plan fits. Deterministic.

    Essentials are kept even when they alone exceed the budget - the result is
    then marked unaffordable rather than quietly trimmed, because silently
    dropping something the planner called essential would answer a different
    question than the one that was asked.

    Within a priority band the most expensive line goes first, so one large
    optional item is dropped before three small ones.
    """
    valid, unknown = [], []
    for line in plan.lines:
        (valid if line.sku in catalog.BY_SKU else unknown).append(line)

    ordered = sorted(valid, key=lambda l: (RANK[l.priority], -_line_cost(l)))
    essentials = [l for l in ordered if l.priority == "essential"]
    rest = [l for l in ordered if l.priority != "essential"]

    kept = list(essentials)
    total = sum(_line_cost(l) for l in kept)

    for line in rest:
        cost = _line_cost(line)
        if total + cost <= budget_paise:
            kept.append(line)
            total += cost

    dropped = [l for l in ordered if l not in kept] + unknown

    # An empty plan is a planning failure, not a free lunch. Without this it
    # reports zero cost and "affordable", which reads as success - seen once in
    # a live run where the model returned reasoning and assumptions but no lines.
    problem = ""
    if not kept:
        problem = (
            "no usable lines: the planner returned nothing that priced against "
            "the catalogue"
            if not valid else
            "nothing fits: every line was dropped"
        )

    return Requisition(
        lines=tuple(kept),
        dropped=tuple(dropped),
        total_paise=total,
        budget_paise=budget_paise,
        reasoning=plan.reasoning,
        assumptions=tuple(plan.assumptions),
        affordable=bool(kept) and total <= budget_paise,
        problem=problem,
    )


def plan_requisition(
    *,
    budget_paise: int,
    request: str = "",
    goal: str = "",
    department: str = "unassigned",
) -> Requisition:
    """Turn a goal into a requisition that fits.

    `request` is the requester's original words and is what the planner plans
    against; `goal` is intake's paraphrase and is context only.

    `goal` is intake's paraphrase and is not sent to the model - the planner
    works from the requester's own words.

    **Retries on an empty plan.** The model intermittently returns fluent
    reasoning and assumptions with an empty `lines` list, on inputs it handles
    correctly moments earlier. Chasing that produced two wrong diagnoses - first
    that intake's paraphrase poisoned the prompt, then that the phrase "budget 5
    lakh" did - and both were coincidence across small samples. It is flakiness
    in structured output, and the honest fix is to notice and ask again rather
    than to explain it.

    Retrying is only possible because an empty plan is detected at all. Before
    that it reported a zero-rupee affordable requisition, which reads as success.
    """
    variables = {
        "company": COMPANY,
        "request": request or goal,
        "department": department,
        "budget_paise": budget_paise,
        "catalogue": catalogue_prompt_block(),
    }

    attempts: list[Plan] = []
    for _ in range(PLAN_ATTEMPTS):
        plan = planner_chain.invoke(variables)
        attempts.append(plan)
        if any(line.sku in catalog.BY_SKU for line in plan.lines):
            return fit_to_budget(plan, budget_paise)

    # Every attempt came back empty. Report the last one rather than inventing
    # a requisition - a planner that cannot plan must say so.
    return fit_to_budget(attempts[-1] if attempts else Plan(reasoning="", lines=[]),
                         budget_paise)


def planner_node(state: dict) -> dict:
    """Graph node. Only runs for a target; an errand already names its lines."""
    if state.get("task_kind") != "target" or state.get("requisition"):
        return {}

    commands = state.get("commands") or {}
    budget = commands.get("budget_paise") or state.get("budget_paise")
    if not budget:
        # No stated cap and none inherited. Planning against an unknown budget
        # would be inventing permission to spend.
        return {"requisition": None, "plan_error": "no budget to plan against"}

    requisition = plan_requisition(
        request=state.get("request", ""),
        goal=state.get("goal") or state.get("request", ""),
        budget_paise=budget,
        department=state.get("department", "unassigned"),
    )
    return {"requisition": requisition.as_dict(), "cart": requisition.to_cart()}
