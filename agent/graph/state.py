"""The shape of an errand's state, written down.

RabbitHole declares CourtroomState as a TypedDict and every node returns a slice
of it. ADK keeps its state in the session and in the tool surface rather than
passing a dict between nodes, so the equivalent discipline is to declare the
shape here and have ToolSurface.snapshot() produce it.

The value is the same either way: one place that says what an errand knows, so a
node's contract is legible without reading the node.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

ErrandAction = Literal["done", "continue", "give_up", "waiting"]

# What kind of machine the request needs. Everything downstream branches on this,
# so intake is conservative: when a request reads as both errand and target it
# chooses errand, because the narrower reading spends less of someone else's money.
TaskKind = Literal["errand", "target", "standing"]


class RequestedItem(TypedDict):
    name: str
    quantity: NotRequired[str | None]


class TaskCommands(TypedDict):
    """Only what the person actually stated. Unstated fields stay None."""

    budget_paise: NotRequired[int | None]
    deadline: NotRequired[str | None]
    shops: NotRequired[list[str] | None]


class ProposalState(TypedDict):
    """One shopper's cart. Three of these exist per round, merged by strategy."""

    strategy: str
    items: dict[str, int]
    total_paise: int
    rationale: str


class RefusalState(TypedDict):
    """Why the gateway said no, in enough detail to re-plan against."""

    reason: str
    available: NotRequired[int]
    cap: NotRequired[int]
    committed: NotRequired[int]


class ErrandState(TypedDict):
    """Everything one errand knows, across every round."""

    request: str

    # Set once by intake_node and never revised - a cycle back through the graph
    # must not re-classify.
    task_kind: NotRequired[TaskKind]
    goal: NotRequired[str]
    items: NotRequired[list[RequestedItem]]
    commands: NotRequired[TaskCommands]
    # The sourcing axis, orthogonal to task_kind: where the goods come from,
    # and therefore whether the open web is involved.
    sourcing: NotRequired[str]
    supplier: NotRequired[str | None]
    intake_reasoning: NotRequired[str]

    iteration: int
    round_open: bool

    proposals: dict[str, ProposalState]
    chosen_strategy: NotRequired[str]
    choice_reason: NotRequired[str]
    cart: dict[str, int]

    last_refusal: NotRequired[RefusalState | None]
    remaining_paise: NotRequired[int | None]

    history: list[str]
    outcome: NotRequired[str]
    finished: bool
