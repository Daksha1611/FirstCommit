"""Graph assembly. RabbitHole's builder.py, expressed in Strands.

    for round in range(MAX_ROUNDS):        <- the cycle
      stage "market"                       <- fan-out, run concurrently
        shopper_thrifty
        shopper_complete
        shopper_balanced
      stage "chooser"                      <- fan-in, and the actual decision
      stage "payer"  (or broker + sub_payer)
      stage "reviewer"                     <- routing: done | continue | give_up

The previous framework expressed this topology through composition - a LoopAgent
repeating its sub-agents and a ParallelAgent running its own concurrently - so
the graph *was* the nesting. Strands has no equivalent wrapper, and writing the
loop out is the better trade: the cycle, the fan-out and the stopping condition
are now three lines of Python anyone can read, instead of behaviour implied by
which class wraps which.

It also makes the topology inspectable without constructing a model. `Buyer` is
a plain description of the stages, which is what the assembly tests assert on.

What is deliberately *not* in the agent's hands: whether a refusal is retryable.
That is graph/route.py, ordinary testable code, because a model that decides to
retry a scope refusal wastes rounds it cannot win.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from agent.models import fleet
from agent.nodes import (
    all_shoppers,
    broker_node,
    chooser_node,
    payer_node,
    reviewer_node,
    sub_payer_node,
)
from agent.prompts import STRATEGY_BRIEFS
from agent.runtime import said
from agent.tools import ToolSurface
from agent.utils import Progress

from .route import classify, route_after_refusal
from .state import ErrandState

MAX_ROUNDS = 3


@dataclass(frozen=True)
class Stage:
    """One step of a round. More than one agent means run them concurrently."""

    name: str
    agents: tuple[Any, ...]

    @property
    def parallel(self) -> bool:
        return len(self.agents) > 1


@dataclass(frozen=True)
class Buyer:
    """The topology, as data.

    Deliberately not an agent. The thing that runs the graph is `run_errand`
    below, and keeping the description separate from the execution is what lets
    the shape be tested without credentials, a model, or a network.
    """

    stages: tuple[Stage, ...]
    max_iterations: int = MAX_ROUNDS


@dataclass
class ErrandResult:
    """What one errand did, across every round."""

    transcript: list[str]
    tools: ToolSurface
    rounds: int
    progress: Progress

    @property
    def paid(self) -> bool:
        return any(a.allowed for a in self.tools.attempted("checkout"))

    @property
    def refusals(self) -> int:
        return sum(1 for a in self.tools.attempted("checkout") if not a.allowed)

    @property
    def replanned(self) -> bool:
        """Recovered from a refusal, rather than stopping at one."""
        return self.refusals > 0 and self.paid

    @property
    def proposals_seen(self) -> int:
        return len(self.tools.attempted("propose_cart"))

    @property
    def payout_attempts(self) -> int:
        return len(self.tools.attempted("payout"))

    @property
    def payouts_succeeded(self) -> int:
        return sum(a.allowed for a in self.tools.attempted("payout"))

    @property
    def refusal_kind(self) -> str:
        return classify(self.tools.last_refusal)

    @property
    def recommended_action(self) -> str:
        """What routing says should happen next, independent of the model."""
        return route_after_refusal(
            self.tools.last_refusal,
            remaining_paise=self.tools.remaining_paise,
            iteration=self.rounds,
            max_rounds=MAX_ROUNDS,
        )

    def state(self) -> ErrandState:
        return self.tools.snapshot()

    def summary(self) -> dict[str, Any]:
        return {
            "paid": self.paid,
            "refusals": self.refusals,
            "replanned": self.replanned,
            "rounds": self.rounds,
            "proposals": self.proposals_seen,
            "chosen": self.tools.chosen_strategy,
            "why": self.tools.choice_reason,
            "refusal_kind": self.refusal_kind,
            "outcome": self.tools.outcome,
            "cart": dict(self.tools.cart),
            "payout_attempts": self.payout_attempts,
            "payouts_succeeded": self.payouts_succeeded,
        }


def build_buyer(tools: ToolSurface, model: str | None = None) -> Buyer:
    """Assemble the graph.

    Passing `model` puts every agent on that one model. On Bedrock that is also
    what happens by default - see `bedrock.spread_models` for why the per-agent
    spread that used to live here was removed rather than ported.
    """
    count = len(STRATEGY_BRIEFS) + 3
    models = fleet(count) if model is None else tuple([model] * count)

    # With a broker token the purchase is split per seller and each share is paid
    # under its own capped mandate. Without one, a single payer settles the whole
    # cart - the older single-seller path, kept so a surface without delegation
    # authority still works.
    settle = (
        (Stage("broker", (broker_node(tools, models[-3]),)),
         Stage("sub_payer", (sub_payer_node(tools, models[-2]),)))
        if tools.broker_token
        else (Stage("payer", (payer_node(tools, models[-2]),)),)
    )

    return Buyer(
        stages=(
            Stage("market", tuple(all_shoppers(tools, models[: len(STRATEGY_BRIEFS)]))),
            Stage("chooser", (chooser_node(tools, models[-3]),)),
            *settle,
            Stage("reviewer", (reviewer_node(tools, models[-1]),)),
        ),
        max_iterations=MAX_ROUNDS,
    )


async def run_errand(
    tools: ToolSurface,
    request: str,
    model: str | None = None,
    on_event=None,
) -> ErrandResult:
    """Run to completion, to an unrecoverable refusal, or to MAX_ROUNDS."""
    progress = Progress(sink=on_event)
    buyer = build_buyer(tools, model)
    transcript: list[str] = []

    async def speak(agent) -> None:
        # Every agent is handed the same errand text. State is not threaded
        # through the conversation - it is read back through the tool surface
        # (`current_constraint`, `review_proposals`, `review_outcome`), which is
        # why a shopper in round three sees the refusal from round two without
        # anyone passing it along.
        result = await agent.invoke_async(request)
        text = said(result)
        if not text:
            return
        line = f"[{agent.name}] {text}"
        transcript.append(line)
        progress(line)

    for _ in range(buyer.max_iterations):
        for stage in buyer.stages:
            if stage.parallel:
                await asyncio.gather(*(speak(agent) for agent in stage.agents))
            else:
                await speak(stage.agents[0])
            # `finish()` sets this. The previous framework needed a special
            # escalation flag on the tool context to break out of its loop; an
            # ordinary loop just asks.
            if tools.finished:
                break
        if tools.finished:
            break

    return ErrandResult(
        transcript=transcript, tools=tools, rounds=tools.iteration, progress=progress
    )
