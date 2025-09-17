"""Recursive delegation: a task becomes a tree of narrower authorities.

The shape you asked for.

    task (any size)
      |
      +-- sub-task  budget/n, its own token, may decompose again
      |     +-- sub-sub-task   narrower still
      |     +-- ...
      +-- sub-task
      +-- ...

Every edge is a Biscuit block appended to the parent's token, so a child holds a
strict subset of its parent's authority and no holder anywhere in the tree can
widen anything. That part is cryptography and needs no trust.

What needs care is *termination*. Unbounded recursion attached to a payment rail
is a machine for spending money, so six bounds sit on it, deliberately
overlapping so that no single bug disables stopping:

  1  depth        cryptographic - the token itself refuses past max_depth
  2  budget floor the real terminator; budget strictly decreases, so depth is
                  bounded by log(root / floor) whatever the depth cap says
  3  fan-out      at most N children per node
  4  node budget  at most N nodes per run, and exhaustion REFUSES rather than
                  quietly returning a smaller tree
  5  cycles       a sub-task that restates an ancestor is a loop
  6  conservation children may not be allocated more than their parent holds

1 and 2 are ordinary termination: stop decomposing, do the work. 3-6 are faults
- a decomposer misbehaving or being manipulated - and they refuse loudly. The
difference matters, because a system that treats "finished" and "gave up" the
same way reports success for work it never did.

Note what is NOT here: no model call. Splitting, allocating, attenuating and
bounding are arithmetic. The model is asked one question per branch node - what
are the sub-tasks? - and everything else in this file is free. That inversion is
what makes a hundred-node tree affordable.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from biscuit_auth import Biscuit

from . import events, token as tokens
from .policy import Grant

# --- sourcing: how a task finds what it is buying ---------------------------
#
# Orthogonal to scope (errand/target/standing). A task is a pair.

CATALOGUE = "catalogue"   # our own SKUs
SPECIFIC = "specific"     # a named supplier; no search
BEST = "best"             # search the open web, compare, then buy
SOURCINGS = frozenset({CATALOGUE, SPECIFIC, BEST})

# How much seller-controlled text each sourcing exposes the tree to, ordered.
# Our own catalogue is no external text at all; one named supplier is some; the
# open web is arbitrary text written by anyone who wants to be found.
EXPOSURE = {CATALOGUE: 0, SPECIFIC: 1, BEST: 2}


def narrowed(parent: str, child: str | None) -> str:
    """The child's sourcing, which may narrow the parent's but never widen it.

    The same rule the tokens follow, applied to the other axis of authority.
    A token cannot grant more than it holds; a sub-task cannot reach further
    for its goods than the task it came from.

    Both directions were broken, in opposite ways:

      * A decomposing model that answers "catalogue" for every child - which is
        what a schema defaulting to catalogue invites - silently dropped a
        person's "find the best price" one layer below the root. The instruction
        was recorded at the top of the tree and obeyed nowhere, so the
        zero-budget looker never ran on a model-driven run at all.

      * In the other direction, a model could move a branch from catalogue to
        best and put the tree in front of open-web text nobody authorised. That
        is a widening of exposure decided mid-run by the untrusted component.

    So None means inherit, a narrower value is honoured, and a wider one is
    clamped back to the parent's rather than obeyed.
    """
    if child is None:
        return parent
    if EXPOSURE.get(child, 0) > EXPOSURE.get(parent, 0):
        return parent
    return child

PENDING, DECOMPOSED, EXECUTED, REFUSED, ESCALATED = (
    "pending", "decomposed", "executed", "refused", "escalated")


@dataclass(frozen=True)
class Bounds:
    """The limits. Defaults are the ones we settled on, not magic numbers.

    max_depth 8 - the token grows ~324 base64 chars per delegation, so a depth-8
    chain is ~3KB and still fits an HTTP header with room to spare.

    decompose_floor ₹5,000 - below this a task is done being split. This is the
    bound that actually ends the recursion: three agents deliberating over five
    thousand rupees costs more in model calls than the decision is worth.
    """

    max_depth: int = 8
    max_fanout: int = 6
    max_nodes: int = 128
    decompose_floor_paise: int = 500_000  # ₹5,000
    # The payment rail refuses orders above its own limit. Razorpay rejected a
    # Rs 6,00,000 leaf outright: "Amount exceeds maximum amount allowed."
    #
    # This bound is the mirror image of the floor, and the pair is the whole
    # sizing story: the FLOOR stops splitting when a piece is small enough to
    # buy, and the CAP forces splitting while a piece is too big to pay for.
    # Without it a tree decomposes happily, pays some leaves, and fails partway
    # through - a half-executed run that only announces itself after money has
    # already moved.
    max_leaf_paise: int = 50_000_000  # ₹5,00,000

    def __post_init__(self) -> None:
        if not 1 <= self.max_depth <= 8:
            raise ValueError("max_depth must be 1..8; the gateway will not mint outside it")
        if self.max_fanout < 1 or self.max_nodes < 1:
            raise ValueError("fan-out and node budget must be positive")
        if self.max_leaf_paise <= self.decompose_floor_paise:
            raise ValueError(
                f"the rail cap ({self.max_leaf_paise}p) must exceed the decomposition "
                f"floor ({self.decompose_floor_paise}p), or no amount is payable"
            )


class FunnelError(Exception):
    """Base for the faults below."""


class BoundExceeded(FunnelError):
    """A decomposition broke one of bounds 3-6. The node is refused, not trimmed."""

    def __init__(self, bound: str, detail: dict[str, Any]) -> None:
        super().__init__(f"{bound}: {detail}")
        self.bound = bound
        self.detail = detail


class Escalated(FunnelError):
    """The payment is suspended awaiting a human, not refused.

    Worth its own type. The gateway answers an escalation with 202 and HOLDS the
    reservation, so the budget is neither spent nor returned - and a run that
    reports a held payment as a refusal tells the operator the money is back when
    it is not. Same distinction the bounds make between ordinary termination and
    a fault: collapsing the two is how a report becomes misleading while every
    individual number in it stays true.
    """


@dataclass(frozen=True)
class CriticVerdict:
    """What a critic thought of one proposed decomposition.

    Advisory, always. `approve=False` refuses the node the way any fault bound
    does; a critic that raises, times out, or is absent leaves the run exactly as
    it would have been. Judgement is a second layer here, never a prerequisite -
    if it became one, a model outage would stop all spending, which is the
    dependency this architecture exists to avoid.
    """

    approve: bool
    reason: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class SubTask:
    """What a decomposer proposes. Plain data - it has not been granted anything.

    `sourcing=None` means inherit. A sub-task of "buy from the best available
    supplier" is itself sourced that way unless it says otherwise, and a
    sub-task of "buy from Meridian" is bought from Meridian. Defaulting to
    `catalogue` instead would silently drop the parent's instruction one layer
    down, which is how a tree ends up sourcing from somewhere nobody chose.
    """

    description: str
    budget_paise: int
    sourcing: str | None = None
    supplier: str | None = None

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("a sub-task with no description cannot be audited")
        if self.budget_paise < 0:
            raise ValueError(f"negative budget: {self.budget_paise}")
        if self.sourcing is not None and self.sourcing not in SOURCINGS:
            raise ValueError(f"unknown sourcing {self.sourcing!r}")
        if self.sourcing == SPECIFIC and not self.supplier:
            raise ValueError("sourcing 'specific' must name a supplier")


@dataclass
class TaskNode:
    """One task, and the authority granted to carry it out."""

    id: str
    parent_id: str | None
    depth: int
    description: str
    budget_paise: int
    token: Biscuit
    sourcing: str = CATALOGUE
    supplier: str | None = None
    children: list["TaskNode"] = field(default_factory=list)
    # A zero-budget looker attached to a node that sources from the open web.
    # Kept apart from `children` deliberately: a helper is not a sub-task, and
    # folding it in would make a paying leaf stop counting as a leaf.
    helpers: list["TaskNode"] = field(default_factory=list)
    state: str = PENDING
    # True for a zero-budget looker. It is a node - it spawns, it is granted, it
    # appears in the stream - but it is not a piece of the work, so it is not
    # counted as a leaf.
    is_helper: bool = False
    refusal: str | None = None
    result: Any = None

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def walk(self) -> Iterator["TaskNode"]:
        yield self
        for helper in self.helpers:
            yield from helper.walk()
        for child in self.children:
            yield from child.walk()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "parent_id": self.parent_id, "depth": self.depth,
            "description": self.description, "budget_paise": self.budget_paise,
            "sourcing": self.sourcing, "supplier": self.supplier,
            "state": self.state, "refusal": self.refusal,
            "helpers": [h.as_dict() for h in self.helpers],
            "children": [c.as_dict() for c in self.children],
        }


def _token_facts(tok: Biscuit) -> dict[str, Any]:
    """What a token IS, without handing anyone the token.

    The Datalog source of each block is a statement of constraints - it says what
    this holder may do, and showing it is the clearest possible evidence that the
    narrowing is real rather than bookkeeping. The signed base64 is a different
    thing entirely: a bearer credential. A `pay` token in a browser is spendable
    by anyone who opens devtools or watches a screen recording, so it never
    leaves this process.

    Size is included because it is the honest cost of the design: roughly 324
    base64 characters per delegation.
    """
    return {
        "blocks": [
            (tok.block_source(i) or "").strip()
            for i in range(tok.block_count())
        ],
        "token_bytes": len(tokens.serialize(tok)),
    }


def _normalise(text: str) -> str:
    """For cycle detection. Case, punctuation and spacing are not the point."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class Funnel:
    """Grows the tree, holds the bounds, narrates as it goes."""

    def __init__(
        self,
        root_token: Biscuit,
        *,
        description: str,
        budget_paise: int,
        bounds: Bounds | None = None,
        bus: events.EventBus | None = None,
        sourcing: str = CATALOGUE,
        supplier: str | None = None,
        ttl_seconds: int = 1800,
        run_id: str | None = None,
        search: Callable[[TaskNode], Any] | None = None,
        critic: Callable[[TaskNode, list[SubTask]], CriticVerdict] | None = None,
    ) -> None:
        self.bounds = bounds or Bounds()
        # Optional: a node sourcing from the open web gets a zero-budget looker
        # first. Left unset, `best` behaves like `catalogue` rather than
        # pretending to have searched.
        self.search = search
        # Optional judgement on a decomposition, before any authority is minted
        # from it. A callable, like `decompose` and `search`, so this layer never
        # learns whether a model produced the answer - which is what keeps
        # eval/funnel.py able to drive 121 nodes with no model at all.
        self.critic = critic
        self.critiques: list[tuple[str, str]] = []
        self.bus = bus if bus is not None else events.bus
        # Stamped on every event this run emits. Node ids are tree-local, so
        # without it two concurrent runs on one gateway draw as one tree.
        self.run_id = run_id
        self.ttl_seconds = ttl_seconds
        self.refusals: list[tuple[str, str]] = []
        self.escalations: list[tuple[str, str]] = []
        self._nodes = 1
        if sourcing == SPECIFIC and not (supplier or "").strip():
            raise ValueError("sourcing 'specific' must name a supplier")
        self.root = TaskNode(
            id="root", parent_id=None, depth=0, description=description,
            budget_paise=budget_paise, token=root_token, sourcing=sourcing,
            supplier=(supplier or "").strip() or None,
        )
        self._emit(self.root, events.SPAWNED, budget_paise=budget_paise,
                   description=description, sourcing=sourcing,
                   supplier=self.root.supplier)

    # --- narration ---------------------------------------------------------

    def _emit(self, node: TaskNode, kind: str, **detail: Any) -> None:
        self.bus.emit(node.id, kind, parent_id=node.parent_id,
                      depth=node.depth, run_id=self.run_id, **detail)

    # --- bounds ------------------------------------------------------------

    @property
    def node_count(self) -> int:
        return self._nodes

    def can_split(self, node: TaskNode) -> tuple[bool, str]:
        """Should this node decompose, or is it already as small as it should get?

        Both answers are success. This is bounds 1 and 2 - the ordinary end of
        the recursion, not a fault.
        """
        if node.depth >= self.bounds.max_depth:
            return False, "at max_depth; this node acts, it does not delegate"
        if node.budget_paise < self.bounds.decompose_floor_paise:
            return False, "below the decomposition floor; too small to be worth splitting"
        return True, ""

    def _parent_of(self, node: TaskNode) -> TaskNode:
        """The branch a node hangs from - the root is its own branch."""
        if not node.parent_id:
            return node
        for candidate in self.root.walk():
            if candidate.id == node.parent_id:
                return candidate
        return self.root

    def _ancestors(self, node: TaskNode) -> list[str]:
        out, current = [], node
        by_id = {n.id: n for n in self.root.walk()}
        while current is not None:
            out.append(_normalise(current.description))
            current = by_id.get(current.parent_id) if current.parent_id else None
        return out

    def _check(self, node: TaskNode, subtasks: list[SubTask]) -> None:
        b = self.bounds
        if len(subtasks) > b.max_fanout:
            raise BoundExceeded("fan-out", {"asked": len(subtasks), "limit": b.max_fanout})
        if self._nodes + len(subtasks) > b.max_nodes:
            raise BoundExceeded("node budget", {
                "used": self._nodes, "asked": len(subtasks), "limit": b.max_nodes})
        total = sum(s.budget_paise for s in subtasks)
        if total > node.budget_paise:
            # Cryptographically this cannot overspend - the chain caps each child
            # and the shared ledger caps the tree. But it means the decomposer
            # believes it has more than it does, and acting on that belief is how
            # a plan half-executes and stops.
            raise BoundExceeded("conservation", {
                "allocated": total, "available": node.budget_paise})
        seen = set(self._ancestors(node))
        for s in subtasks:
            key = _normalise(s.description)
            if key in seen:
                raise BoundExceeded("cycle", {"description": s.description})
            seen.add(key)

    # --- growing the tree --------------------------------------------------

    def split(self, node: TaskNode, subtasks: list[SubTask]) -> list[TaskNode]:
        """Attenuate the node's token once per sub-task and hang the children off it."""
        if not subtasks:
            raise BoundExceeded("empty decomposition", {"node": node.id})
        self._check(node, subtasks)
        self._criticise(node, subtasks)

        child_ttl = max(60, self.ttl_seconds // (2 ** (node.depth + 1)))
        expires = datetime.now(timezone.utc) + timedelta(seconds=child_ttl)
        children: list[TaskNode] = []

        for i, sub in enumerate(subtasks, start=1):
            child_id = f"{node.id}.{i}"
            child_depth = node.depth + 1
            # A child that cannot split does not get `delegate`. Leaves are
            # cryptographically unable to grow the tree, which means a
            # compromised leaf cannot manufacture accomplices - it can only
            # misspend the small budget it was handed.
            sourcing = narrowed(node.sourcing, sub.sourcing)
            # The supplier travels with `specific` and with nothing else: a
            # child clamped back to the parent's sourcing must not keep a name
            # the parent never had, and one narrowed to catalogue has no
            # supplier to speak of.
            if sourcing == SPECIFIC:
                supplier = sub.supplier or node.supplier
            else:
                supplier = None
            probe = TaskNode(child_id, node.id, child_depth, sub.description,
                             sub.budget_paise, node.token, sourcing, supplier)
            splittable, _ = self.can_split(probe)
            # A BRANCH holds what its subtree may need to be granted; a LEAF is
            # narrowed to exactly one capability.
            #
            # This is forced by monotonic attenuation, and getting it wrong is
            # subtle. The looker used to hang off the paying node, so the payer
            # had to hold `search` for its own child to inherit it - which put
            # "reads supplier-written pages" and "can spend" in one token and
            # quietly undid the separation the design is named for. A branch may
            # confer both because it exercises neither: that is the same
            # grant-without-use compromise documented in token.is_broker, and it
            # is gateway policy rather than cryptography.
            tools: tuple[str, ...] = ("delegate", "pay", "search") if splittable else ("pay",)

            # A branch is named as a broker so the gateway refuses its payments.
            #
            # It must HOLD everything it confers - a parent without `pay` cannot
            # grant `pay`, which is the monotonicity this project proved the hard
            # way - so a branch necessarily carries pay and search together. That
            # combination is exactly what must never execute, and the existing
            # broker rule already refuses it. The consequence is deliberate: a
            # node that was sized to split but produced no sub-tasks is REFUSED
            # rather than quietly paying while holding `search`, which surfaces a
            # badly-set floor instead of hiding it.
            authority = "broker/" if splittable else ""
            child_token = tokens.attenuate(
                node.token,
                to=f"agent://pocketchange/{authority}{child_id}",
                grant=Grant(tools=tools, budget_paise=sub.budget_paise, expires=expires),
                context=f"sub-task of {node.id}: {sub.description}",
            )
            child = TaskNode(
                id=child_id, parent_id=node.id, depth=child_depth,
                description=sub.description, budget_paise=sub.budget_paise,
                token=child_token, sourcing=sourcing, supplier=supplier,
            )
            children.append(child)
            self._nodes += 1
            self._emit(child, events.SPAWNED, description=sub.description,
                       sourcing=sourcing, supplier=supplier)
            self._emit(child, events.GRANTED, budget_paise=sub.budget_paise,
                       tools=list(tools), expires=expires.isoformat(),
                       token_depth=tokens.depth_of(child_token),
                       **_token_facts(child_token),
                       # A branch holds `pay` and `search` in order to CONFER
                       # them, and the gateway refuses it either. Without this
                       # flag a reader sees the same chips on a broker and on an
                       # agent that genuinely does both.
                       broker=splittable)

        node.children = children
        node.state = DECOMPOSED
        self._emit(node, events.DECOMPOSED, children=len(children),
                   allocated=sum(s.budget_paise for s in subtasks))
        return children

    def search_child(self, node: TaskNode) -> TaskNode:
        """A looker with no money, SIBLING to the node it sources for.

        Granted ("search",) and a budget of zero, so the agent that reads the
        most hostile text in the system is, by construction, the one that cannot
        spend. Datamarking the results is defence in depth; this is the defence.

        Sibling, not child, and that word is the whole design. Attenuation is
        monotonic: a child holds a subset of its parent. Hanging the looker off
        the paying node therefore requires the PAYER to hold `search` before its
        child can inherit it - which is the shopper/payer chain this project
        already proved unsafe once, rebuilt by accident. Attenuating from the
        parent branch instead keeps read and pay in two tokens that never meet.
        """
        branch = self._parent_of(node)
        expires = datetime.now(timezone.utc) + timedelta(
            seconds=max(60, self.ttl_seconds // (2 ** (node.depth + 1))))
        sibling_id = f"{node.id}~search"
        child_token = tokens.attenuate(
            branch.token,
            to=f"agent://pocketchange/{sibling_id}",
            grant=Grant(tools=("search",), budget_paise=0, expires=expires),
            context=f"sourcing for {node.id}: may look, may not spend",
        )
        child = TaskNode(
            id=sibling_id, parent_id=branch.id, depth=node.depth,
            description=f"find the best source for: {node.description}",
            budget_paise=0, token=child_token, sourcing=node.sourcing,
            is_helper=True,
        )
        self._nodes += 1
        # Flagged explicitly rather than left to be inferred from the id. A
        # consumer matching on "ends with .search" broke silently the moment the
        # looker became a sibling and its id gained a ~ instead of a dot.
        self._emit(child, events.SPAWNED, description=child.description,
                   sourcing=node.sourcing, helper=True, sources_for=node.id)
        self._emit(child, events.GRANTED, budget_paise=0, tools=["search"],
                   expires=expires.isoformat(),
                   token_depth=tokens.depth_of(child_token),
                   note="may look, may not spend",
                   **_token_facts(child_token))
        return child

    def sole_payer(self, node: TaskNode) -> TaskNode:
        """One narrowed child to carry out a task that did not divide.

        The mandate must never be the thing that pays. It is the human's own
        authority: unattenuated, holding every capability, capped only by the
        ceiling. Letting it settle a payment directly is precisely "hand the
        agent your wallet", and it happened the first time a live model answered
        that a task was atomic - the root paid the entire budget in one
        transaction with nothing narrowed and no leaf bound in sight.

        So a node that will act and holds no narrowing gets exactly one child,
        granted ("pay",) and its own budget. Deliberately a leaf whatever the
        floor says: we have already decided this work is not being split, so the
        thing that carries it out is a leaf by definition.
        """
        child_id = f"{node.id}.1"
        expires = datetime.now(timezone.utc) + timedelta(
            seconds=max(60, self.ttl_seconds // 2))
        child_token = tokens.attenuate(
            node.token,
            to=f"agent://pocketchange/{child_id}",
            grant=Grant(tools=("pay",), budget_paise=node.budget_paise, expires=expires),
            context=f"sole payer for {node.id}: the task did not divide",
        )
        child = TaskNode(
            id=child_id, parent_id=node.id, depth=node.depth + 1,
            description=node.description, budget_paise=node.budget_paise,
            token=child_token, sourcing=node.sourcing, supplier=node.supplier,
        )
        node.children = [child]
        self._nodes += 1
        self._emit(child, events.SPAWNED, description=child.description,
                   sourcing=child.sourcing, supplier=child.supplier)
        self._emit(child, events.GRANTED, budget_paise=child.budget_paise,
                   tools=["pay"], expires=expires.isoformat(),
                   token_depth=tokens.depth_of(child_token), broker=False,
                   note="the task did not divide; narrowed once so the mandate never pays",
                   **_token_facts(child_token))
        return child

    def _criticise(self, node: TaskNode, subtasks: list[SubTask]) -> None:
        """Judge a decomposition before minting authority from it.

        Placed after `_check` on purpose. The bounds are arithmetic, cheap and
        certain; a decomposition that breaks conservation is refused without
        spending a model call on it. Only a plan that is structurally sound gets
        the expensive question asked of it.

        And placed before `tokens.attenuate`, because that is where authority
        comes into existence. Judging at payment time - which is all this system
        did until now - arrives after the tree has already been built and every
        token handed out.

        What the bounds cannot see: whether the sub-tasks have anything to do
        with what the human authorised. A decomposer that drifts, or is steered,
        into plausible-but-wrong work passes fan-out, node budget, conservation
        and cycles without touching any of them.
        """
        if self.critic is None:
            return
        try:
            verdict = self.critic(node, subtasks)
        except Exception as exc:  # noqa: BLE001
            # The trust boundary. A critic that is unreachable, slow or broken
            # must leave the run identical to one with no critic configured.
            self._emit(node, events.BOUND_HIT, fault=False,
                       reason=f"critic unavailable, proceeding: {type(exc).__name__}")
            return

        if verdict.approve:
            return

        self.critiques.append((node.id, verdict.reason))
        raise BoundExceeded("critic", {
            # Not "reason": _refuse(node, reason, **detail) already takes that
            # name, and the collision is a TypeError at the worst moment.
            "critique": verdict.reason,
            "confidence": verdict.confidence,
            "subtasks": [s.description for s in subtasks],
        })

    def _refuse(self, node: TaskNode, reason: str, **detail: Any) -> None:
        node.state = REFUSED
        node.refusal = reason
        self.refusals.append((node.id, reason))
        self._emit(node, events.BOUND_HIT, reason=reason, fault=True, **detail)

    # --- running -----------------------------------------------------------

    def run(
        self,
        decompose: Callable[[TaskNode], list[SubTask]],
        execute: Callable[[TaskNode], Any],
    ) -> "FunnelResult":
        """Breadth-first, so the tree fills out layer by layer.

        Breadth-first on purpose: it makes the live view legible (a layer appears
        at a time rather than one deep spike), and it means the node budget is
        spent on breadth near the root rather than exhausted inside the first
        branch it wanders into.
        """
        queued: deque[TaskNode] = deque([self.root])
        while queued:
            node = queued.popleft()
            splittable, why = self.can_split(node)

            if not splittable:
                self._emit(node, events.BOUND_HIT, reason=why, fault=False)
                self._execute(node, execute)
                continue

            try:
                subtasks = decompose(node)
            except Exception as exc:  # noqa: BLE001 - a decomposer is untrusted
                self._refuse(node, f"decomposition failed: {exc}")
                continue

            if not subtasks:
                # Nothing to split into is a legitimate answer: the task is
                # already atomic. Do it.
                self._emit(node, events.BOUND_HIT,
                           reason="decomposer returned no sub-tasks; task is atomic",
                           fault=False)
                self._execute(node, execute)
                continue

            try:
                queued.extend(self.split(node, subtasks))
            except BoundExceeded as exc:
                self._refuse(node, exc.bound, **exc.detail)

        return FunnelResult(root=self.root, refusals=list(self.refusals),
                            escalations=list(self.escalations),
                            critiques=list(self.critiques),
                            nodes=self._nodes, bounds=self.bounds)

    def _execute(self, node: TaskNode, execute: Callable[[TaskNode], Any]) -> None:
        # A leaf the rail will not accept.
        #
        # The floor and the depth cap both stop the recursion, and neither knows
        # what a payment processor will take: Razorpay refused a Rs 6,00,000 leaf
        # outright. Since the floor always sits below this cap, an oversized node
        # keeps splitting on its own - so the only way to arrive here is to run
        # out of depth first, and at that point there is nothing left to try.
        #
        # Refuse rather than attempt it. The rail would reject it anyway, but a
        # step later, after siblings have already paid: a half-executed tree that
        # announces the problem only once money has moved.
        if node.budget_paise > self.bounds.max_leaf_paise:
            self._refuse(
                node, "above the rail's per-order limit",
                amount_paise=node.budget_paise, limit_paise=self.bounds.max_leaf_paise,
                hint="raise max_depth or widen fan-out so the leaves come out smaller",
            )
            return

        # The root holds the mandate. It delegates; it does not spend.
        if node.parent_id is None and not node.children:
            node.state = DECOMPOSED
            self._emit(node, events.DECOMPOSED, children=1,
                       allocated=node.budget_paise,
                       reason="task did not divide; narrowed once before paying")
            self._execute(self.sole_payer(node), execute)
            return

        if node.sourcing == BEST and self.search is not None:
            looker = self.search_child(node)
            self._emit(looker, events.SEARCHING, query=node.description)
            try:
                looker.result = self.search(looker)
            except Exception as exc:  # noqa: BLE001 - the web is not ours
                looker.state = REFUSED
                looker.refusal = str(exc)
                self._emit(looker, events.DENIED, reason=str(exc))
            else:
                looker.state = EXECUTED
                self._emit(looker, events.SETTLED,
                           result=f"{len(looker.result or [])} results")
            self._parent_of(node).helpers.append(looker)

        self._emit(node, events.PAYING, amount_paise=node.budget_paise,
                   sourcing=node.sourcing, supplier=node.supplier)
        try:
            node.result = execute(node)
        except Escalated as exc:
            node.state = ESCALATED
            node.refusal = str(exc)
            self.escalations.append((node.id, str(exc)))
            self._emit(node, events.ESCALATED, reason=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - the rail and the gateway both refuse
            node.state = REFUSED
            node.refusal = str(exc)
            self.refusals.append((node.id, str(exc)))
            self._emit(node, events.DENIED, reason=str(exc))
            return
        node.state = EXECUTED
        self._emit(node, events.SETTLED, result=node.result)


@dataclass(frozen=True)
class FunnelResult:
    """What the run produced, including what it refused to do.

    `refusals` is not an afterthought. A tree that hit its node budget looks
    exactly like a smaller tree unless the run says so, and "covered everything"
    is the most expensive wrong belief this system could produce.
    """

    root: TaskNode
    refusals: list[tuple[str, str]]
    nodes: int
    bounds: Bounds
    escalations: list[tuple[str, str]] = field(default_factory=list)
    # Plans a critic refused, kept apart from `refusals` because the reason is a
    # judgement rather than a bound being exceeded.
    critiques: list[tuple[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Neither refused nor still waiting on a person.

        An escalation is not a refusal, but it is not done either - the money is
        held, not returned - so a run with one outstanding is incomplete.
        """
        return not self.refusals and not self.escalations

    @property
    def leaves(self) -> list[TaskNode]:
        """Task nodes that did the work. Lookers are not part of the work."""
        return [n for n in self.root.walk() if n.is_leaf and not n.is_helper]

    @property
    def executed(self) -> list[TaskNode]:
        return [n for n in self.root.walk()
                if n.state == EXECUTED and not n.is_helper]

    def committed_paise(self) -> int:
        return sum(n.budget_paise for n in self.executed)

    def summary(self) -> dict[str, Any]:
        depths = [n.depth for n in self.root.walk()]
        return {
            "nodes": self.nodes,
            "max_depth_reached": max(depths) if depths else 0,
            "leaves": len(self.leaves),
            "executed": len(self.executed),
            "refused": len(self.refusals),
            "escalated": len(self.escalations),
            "complete": self.complete,
            "committed_paise": self.committed_paise(),
        }
