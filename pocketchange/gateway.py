"""The policy enforcement point. Holds the only real payment credential.

Everything the agent wants to do passes through here, and the order the checks
run in *is* the specification. Cheap cryptography first, expensive state last, so
a forged token never reaches the database and a denied payment never reaches
Razorpay.

  1  extract   token from one of three protocol bindings
  2  authenticate  signature chain against the root key      -> Forged
  3  context    non-empty, because the monitor will need it
  4  authorise  expiry, depth, scope, per-token budget       -> Denied
  5  reserve    cumulative spend + idempotency, under a lock -> InsufficientBudget
  6  charge     Razorpay, the only place a credential is used
  7  settle     commit on success, release on failure
  8  record     audit entry either way

Every path is fail-closed: an exception anywhere denies, and every denial is
written to the audit trail with its reason. A log that only records successes
cannot show that an attack was stopped.
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from time import perf_counter
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import Request as _Request
from pydantic import BaseModel, ConfigDict, Field

from . import bedrock, cedar, config, counterparties, events, funnel as funnels
from . import identity, intake, providers, token
from . import memory as memory_bank
from .approvals import AlreadyResolved, ApprovalStore, UnknownApproval
from .audit import AuditLog, AuditTampered, Decision
from .idempotency import ReplayStore, derive_key
from .ledger import InsufficientBudget, LedgerError, UnknownMandate
from .ledger import fallback_reason as ledger_fallback_reason
from .ledger import from_env as ledger_from_env
from .monitor import Judgement, Situation, Verdict
from .monitor import from_env as monitor_from_env
from .monitor import judges as monitor_judges
from .policy import Grant, Operation
from .registry import AgentCard, AgentRegistry, ExceedsCard, UnknownAgent
from .razorpay_client import FakeRail, PaymentError, from_env

app = FastAPI(
    title="Pocket Change",
    description="Bounded, revocable spending authority for AI agents.",
    version="0.1.0",
)

# The dashboard is served by Vite on another port, so every call to this gateway
# is cross-origin. Restricted to loopback development origins by name: a
# wildcard here would let any page the operator happens to have open read their
# audit trail and post to /events.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://127.0.0.1:5173",   # vite dev
        "http://localhost:4173", "http://127.0.0.1:4173",   # vite preview
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-AIP-Token", "Authorization"],
)


class State:
    """Everything the gateway owns.

    The root key lives here and nowhere else. In a real deployment it would sit
    in a KMS and the principal would sign with a hardware-backed key; for the
    demo, generating it at startup is honest and keeps the trust boundary in the
    right place - the agent never holds it either way.
    """

    def __init__(self) -> None:
        self.principal = identity.web(
            "pocketchange.dev", "principal", keypair=_root_keypair()
        )
        self.root_public_key = token.root_key_from(self.principal.private_key)
        # DynamoDB when one is configured, in-memory otherwise.
        # Tests and local work must never need cloud credentials.
        self.ledger = ledger_from_env()
        self.replays = ReplayStore()
        self.audit = AuditLog()
        self.rail = from_env()
        self.monitor = monitor_from_env()
        self.intents: dict[str, str] = {}   # mandate id -> what the human authorised
        self.approvals = ApprovalStore()
        self.registry = _standard_fleet()
        # Who we have actually paid. DynamoDB when configured, so the record
        # outlives a restart - a reputation that resets every deploy is not one.
        self.counterparties = counterparties.from_env()
        self.memory = memory_bank.from_env()


def _standard_fleet() -> AgentRegistry:
    """The agents a procurement department deploys, published and approved.

    Seeded rather than empty so the registry is a live control from the first
    request. Ephemeral sub-agents are not here on purpose - they are minted per
    purchase and bounded by attenuation, and pre-registering something that lives
    for ten minutes would be paperwork rather than governance.
    """
    registry = AgentRegistry()
    for card in (
        AgentCard(
            name="procurement-shopper", version="1.0.0", department="engineering",
            owner="platform-team", identity="aip:web:pocketchange.dev/shopper",
            capabilities=("search", "cart"), max_budget_paise=600_000 * 100,
            description="Reads supplier catalogues and proposes requisitions. Cannot spend.",
        ),
        AgentCard(
            name="procurement-broker", version="1.0.0", department="finance",
            owner="treasury", identity="aip:web:pocketchange.dev/broker",
            capabilities=("delegate", "pay"), max_budget_paise=600_000 * 100,
            description="Splits an approved requisition and mints one capped mandate per supplier.",
        ),
    ):
        registry.publish(card)
    return registry


def _root_keypair():
    """Three tiers, checked in order: ephemeral, Secrets Manager, disk.

    Tests and throwaway runs get a fresh key. A real gateway keeps one - but
    "keeps one" means something different depending on what is under it.
    `secrets.py` wins when it is configured because a container's disk is not
    shared across instances or across a redeploy, and a key that is not
    identical everywhere is indistinguishable from forgery to whichever
    instance did not mint it. `keys.py` is the laptop fallback: right for one
    process, one disk, no scaling.
    """
    from biscuit_auth import KeyPair

    if os.getenv("POCKETCHANGE_EPHEMERAL_KEYS") == "1":
        return KeyPair()

    from . import secrets

    if secrets.configured():
        return secrets.load_or_create()

    from . import keys

    try:
        return keys.load_or_create()
    except OSError:
        # An unwritable disk must not stop the gateway, but only a disk problem
        # earns this fallback. Anything else is a bug and should surface.
        return KeyPair()


# Read .env BEFORE the state is built. Everything that decides whether this
# gateway is live - the Razorpay rail, the durable ledger, the monitor -
# reads os.environ inside State(), and until now nothing loaded the file. The
# effect was quiet and total: a fully configured project still reported
# {"rail": "fake"} because the keys were on disk and never in the process.
CONFIGURED = config.load()

# Whether the semantic monitor runs for THIS call. A context variable rather
# than a request field on purpose: a field would let any external caller turn the
# monitor off, which is the one control an untrusted agent must never hold. Only
# _pay_internal sets it, only for the thread driving that run, and an HTTP
# request that arrives from outside never touches it - so the default is always
# "monitor runs".
# The verdict recorded when judgement was deliberately skipped. Not an "allow"
# the monitor issued - the audit entry says monitor_ran: false beside it.
_ALLOW_UNJUDGED = Judgement(Verdict.ALLOW, "monitor not run for this payment", 0.0)

# And the one recorded when there was no monitor to run. Separate wording,
# because "you turned it off" and "there was never anything there" are different
# facts about a payment, and a reader of the trail is entitled to both.
_ALLOW_UNCONFIGURED = Judgement(
    Verdict.ALLOW, "no monitor configured - nothing judged this payment", 0.0)


# How long a run may go without finishing before the console is told it has
# stalled. Generous: a wide tree with a live model and a live rail legitimately
# takes a while. This is a liveness signal, not a timeout on the work.
RUN_DEADLINE_SECONDS = 180.0

MONITOR_ON = contextvars.ContextVar("pocketchange_monitor_on", default=True)

# audit seq -> (token, the exact request, what it produced). Server-side only:
# these hold live tokens, which is precisely why they never travel to a browser.
_REPLAYABLE: dict[int, tuple[str, "PayRequest", dict[str, Any]]] = {}

state = State()


# --- entry points ----------------------------------------------------------


def bearer_token(request: Request) -> str:
    """Pull the token from whichever binding the caller used.

    AIP defines three, and they all land here so the checks below never learn
    which transport a request arrived on:

      MCP    X-AIP-Token: <token>
      HTTP   Authorization: AIP <token>
      A2A    aip_token in task metadata (request body)

    Headers are read off the request rather than declared as FastAPI Header
    parameters, because this is called directly from the handler rather than
    through Depends - declared parameters would silently arrive as None.
    """
    mcp = request.headers.get("x-aip-token")
    if mcp and mcp.strip():
        return mcp.strip()

    authorization = request.headers.get("authorization")
    if authorization and authorization.startswith("AIP "):
        candidate = authorization[4:].strip()
        if candidate:
            return candidate

    a2a = getattr(request.state, "a2a_token", None)
    if a2a:
        return a2a

    raise HTTPException(401, "no AIP token: use X-AIP-Token, Authorization: AIP, or aip_token")


# --- the demo gate -----------------------------------------------------------
#
# NOT authentication, and the difference matters enough to name. This is one
# shared secret that ships inside a public web page, so anyone determined has it.
# What it stops is the realistic failure for a hackathon demo: a crawler or a
# shared link finding POST /runs and draining a 15-request-per-minute free tier
# in the middle of judging.
#
# Reads stay open on purpose - a judge should be able to see the tree, the
# ledger, the audit trail and the counterparty record without being asked for
# anything. Only the routes that spend quota or mint authority are gated.
#
# Unset, nothing is gated: local development and the test suite behave exactly as
# before, which is why every existing test still passes untouched.

DEMO_TOKEN = os.environ.get("POCKETCHANGE_DEMO_TOKEN", "").strip()

# A sliding window, per process. Cloud Run may run several instances, so this is
# a brake rather than a guarantee - and a brake is what the free tier needs.
RUN_WINDOW_SECONDS = 600
RUN_LIMIT = 8
_recent_runs: list[float] = []
_runs_lock = threading.Lock()


@app.middleware("http")
async def gate_writes(request: Request, call_next):
    """Every write needs the token; every read is open.

    Middleware rather than a per-route dependency, and that is the whole point:
    gating routes one at a time left POST /approvals open on the first pass -
    the endpoint where a person releases a held payment - along with /events,
    /replay and /agents. A rule that has to be remembered at each new route is a
    rule that will be forgotten at one of them.

    So the shape is inverted: writes are refused by default, and there is no
    per-route opt-in to leave off.
    """
    if DEMO_TOKEN and request.method not in ("GET", "HEAD", "OPTIONS"):
        offered = (request.headers.get("x-demo-token") or "").strip()
        if not compare_digest(offered, DEMO_TOKEN):
            return JSONResponse(status_code=401, content={"detail": {
                "denied": "this deployment gates writes behind a demo token",
                "hint": "send X-Demo-Token. Reads are open - try GET /audit "
                        "or GET /counterparties.",
            }})
    return await call_next(request)


def rate_limit_runs() -> None:
    """Cap how often a run may be started, so quota survives being found.

    Tied to the demo gate rather than always on: the limit exists to protect one
    shared free tier on a public URL. Locally, and in the test suite, there is
    nothing to protect and a cap is only noise.
    """
    if not DEMO_TOKEN:
        return
    now = perf_counter()
    with _runs_lock:
        cutoff = now - RUN_WINDOW_SECONDS
        _recent_runs[:] = [t for t in _recent_runs if t > cutoff]
        if len(_recent_runs) >= RUN_LIMIT:
            wait = int(RUN_WINDOW_SECONDS - (now - _recent_runs[0]))
            raise HTTPException(429, {
                "denied": f"{RUN_LIMIT} runs per {RUN_WINDOW_SECONDS // 60} minutes",
                "retry_after_seconds": max(1, wait),
                "why": "a shared free tier, and a demo that has to survive judging",
            })
        _recent_runs.append(now)


# --- request and response shapes -------------------------------------------


class MandateRequest(BaseModel):
    # Unknown fields are rejected, not ignored. A request carrying a field
    # this gateway does not understand is a protocol mismatch, and silently
    # dropping it is how a caller ends up believing a constraint applied.
    model_config = ConfigDict(extra="forbid")

    budget_paise: int = Field(gt=0, description="Ceiling for everything under this mandate")
    # 8, not 5: a recursive funnel needs room to decompose. Biscuit was probed
    # to depth 10 - the token grows ~324 base64 chars per layer, ~3KB at depth 8,
    # and verified at every level. That is well inside an 8KB header limit.
    max_depth: int = Field(default=3, ge=1, le=8)
    ttl_seconds: int = Field(default=3600, gt=0, le=86_400)
    purpose: str = Field(min_length=1, description="What the human is authorising")


class MandateResponse(BaseModel):
    mandate_id: str
    token: str
    budget_paise: int
    expires: datetime
    purpose: str


class DelegateRequest(BaseModel):
    # Unknown fields are rejected, not ignored. A request carrying a field
    # this gateway does not understand is a protocol mismatch, and silently
    # dropping it is how a caller ends up believing a constraint applied.
    model_config = ConfigDict(extra="forbid")

    token: str
    tools: list[str] = Field(min_length=1)
    budget_paise: int = Field(ge=0)
    context: str = Field(min_length=1, description="Why this delegation is happening")
    to: str | None = None
    depth: int | None = Field(
        default=None, ge=0,
        description="Ignored; depth is derived from the token chain, not claimed.",
    )
    ttl_seconds: int | None = Field(
        default=None, gt=0, le=3600,
        description="Optional short life for the child, so an unused mandate expires.",
    )


class PayRequest(BaseModel):
    # Unknown fields are rejected, not ignored. A request carrying a field
    # this gateway does not understand is a protocol mismatch, and silently
    # dropping it is how a caller ends up believing a constraint applied.
    model_config = ConfigDict(extra="forbid")

    amount_paise: int = Field(gt=0)
    cart: dict[str, Any] = Field(description="What is being bought; fingerprinted for replay")
    context: str = Field(min_length=1, description="Why the agent believes it should pay")
    depth: int | None = Field(
        default=None, ge=0,
        description="Ignored; depth is derived from the token chain, not claimed.",
    )
    aip_token: str | None = Field(default=None, description="A2A binding")
    # Reported by the buyer, so untrusted - but a signal worth keeping.
    # 0 means every parallel shopper produced an identical cart, which is the
    # monoculture failure D4 describes appearing inside our own system: same
    # model family, same catalogue, same answer. Unanimity is evidence, not
    # comfort. Recorded here so an independent observer can act on it later.
    divergence: int | None = Field(
        default=None, ge=0,
        description="How many distinct carts the parallel shoppers produced, minus one.",
    )
    # Who is being paid. Agent-supplied, so the NAME is untrusted - but what we
    # record against that name is ours, and a seller cannot inflate it.
    counterparty: str | None = Field(
        default=None, max_length=200,
        description="Identifier of the party being paid, e.g. a supplier id.",
    )
    # Agent-supplied, and safe to be, because of what it cannot do. Setting it
    # never settles anything: it routes a request that would otherwise be
    # replayed to a person, instead of handing back the original receipt. An
    # agent that sets it on every call earns itself a human reading every
    # payment, which is the opposite of an escape hatch.
    #
    # This is the distinction that matters. `derive_key` refuses a caller-chosen
    # idempotency KEY, because an agent could defeat replay protection by
    # picking a fresh one. A caller-chosen FLAG is fine so long as its only
    # reachable effect is to demand more authorisation than the default path,
    # never less.
    repurchase: bool = Field(
        default=False,
        description="Ask a person to authorise buying this same cart again.",
    )


class PayResponse(BaseModel):
    order_id: str
    amount_paise: int
    mandate_id: str
    remaining_paise: int
    replayed: bool
    audit_seq: int


def _depth(bearer, claimed: int | None, *, mid: str, tool: str, context: str) -> int:
    """The depth we will actually enforce, plus a record if the caller lied.

    `claimed` is whatever the request body said. We do not use it. It is compared
    only so that a disagreement becomes evidence: an agent that under-reports its
    own depth is trying to buy itself another layer of delegation, which is a
    concrete instance of the agent-integrity failure SoK calls D1. Silently
    ignoring the field would throw that signal away.
    """
    derived = token.depth_of(bearer)
    if claimed is not None and claimed != derived:
        state.audit.append(
            mandate_id=mid,
            actor=token.terminal_delegate(bearer) or "unknown",
            tool=tool,
            decision=Decision.DENIED,
            reason="claimed depth does not match the token chain",
            context=context,
            detail={"claimed_depth": claimed, "derived_depth": derived},
        )
    return derived


def _role(bearer) -> str:
    """The principal's role, as the authorisation policy names it.

    Derived from the token chain, never from the request body. A role the caller
    could assert is not a role - it is a request, and this one decides whether
    the money moves.
    """
    return "broker" if token.is_broker(bearer) else "payer"


def _mount_console() -> None:
    """Serve the built console from this same service, if it has been built.

    One origin, which removes the CORS surface rather than configuring it: the
    console fetches same-origin paths and the browser never issues a preflight.
    Mounted last so it cannot shadow an API route, and skipped entirely when
    frontend/dist is absent - the gateway must still run for anyone who only
    wants the API.
    """
    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if not (dist / "index.html").exists():
        return
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="console")


# --- endpoints -------------------------------------------------------------


class RunRequest(BaseModel):
    """Start a funnel. The one thing the console could not do before this.

    Until now a run had to be driven from a Python process, so the dashboard's
    centrepiece was necessarily a simulation - and a simulated funnel is exactly
    the artefact this project keeps having to apologise for. This makes the tree
    on screen the tree that actually spent the money.
    """

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=400)
    budget_paise: int = Field(gt=0, le=100_000_000)
    max_depth: int = Field(default=8, ge=1, le=8)
    fan_out: int = Field(default=3, ge=2, le=6)
    floor_paise: int = Field(default=5_000 * 100, gt=0)
    decomposer: Literal["auto", "model", "departmental"] = Field(
        default="auto",
        description="auto uses the model when a key is configured, else departmental.",
    )
    critic: bool = Field(
        default=True,
        description=(
            "Have a second model judge each decomposition before authority is "
            "minted from it. Advisory: if it is unreachable the run proceeds."
        ),
    )
    # Sourcing has to travel as DATA, not only inside the task sentence.
    #
    # It used to be folded into the prose alone - "find the best source;
    # searching is allowed" - while the root node was constructed at the default
    # `catalogue`. The decomposing model read the sentence and the funnel
    # ignored it, so the zero-budget looker never spawned on a real run: the
    # mechanism the whole separation exists for was unreachable from the API.
    sourcing: Literal["catalogue", "specific", "best"] = Field(
        default="catalogue",
        description=(
            "Where goods may be sourced from. The widest exposure any node in "
            "this run may have - sub-tasks inherit it and may narrow it, never "
            "widen it."
        ),
    )
    supplier: str | None = Field(
        default=None, max_length=120,
        description="Required when sourcing is 'specific'. Never invented.",
    )
    monitor: bool = Field(
        default=True,
        description=(
            "Run the semantic monitor on every payment. Switching it off removes "
            "no cryptographic guarantee - the two layers are separable - and "
            "costs one model call per leaf less."
        ),
    )


def _departmental(fan_out: int):
    """A decomposer that needs no model, and does not pretend to be one.

    Splits a task by department, then by category, then stops. Deterministic on
    purpose: the console must work on a free tier with no key, and the bounds -
    which are what this system is actually about - are model-independent.
    """
    DEPARTMENTS = ["engineering", "design", "operations", "finance", "support", "facilities"]
    CATEGORIES = ["workstations", "peripherals", "furniture", "consumables",
                  "networking", "licences"]

    def decompose(node):
        share = node.budget_paise // fan_out
        if node.depth == 0:
            names = DEPARTMENTS[:fan_out]
            return [funnels.SubTask(f"kit out {d}", share) for d in names]
        if node.depth == 1:
            names = CATEGORIES[:fan_out]
            return [
                funnels.SubTask(
                    f"{node.description}: {c}", share,
                    # One category per branch is sourced from the open web, so a
                    # zero-budget looker appears in every run rather than only in
                    # the ones a reader goes looking for.
                    sourcing=funnels.BEST if c == "peripherals" else None,
                )
                for c in names
            ]
        return []

    return decompose


def _choose_decomposer(req: RunRequest, bounds: funnels.Bounds):
    """Which decomposer, and an honest name for it.

    `_departmental` splits by department and category whatever you typed, so it
    cannot answer a real query - "buy 3 standing desks" and "restock toner"
    produce identical trees. The model path is what makes an arbitrary task mean
    something. It is chosen only when a key is actually configured, and the name
    is returned so the console can never imply a model was involved when it was
    not.
    """
    wanted = req.decomposer
    have_key = have_model()

    if wanted == "departmental" or (wanted == "auto" and not have_key):
        return _departmental(req.fan_out), "departmental (deterministic, no model)"

    if wanted == "model" and not have_key:
        raise HTTPException(400, "decomposer=model needs AWS credentials for Bedrock")

    from agent.nodes.decompose_node import make_decomposer

    return make_decomposer(bounds), "model (bedrock, one call per branch)"


def _expected_model_calls(req: RunRequest, using_model: bool) -> int:
    """Branch nodes cost one decompose call; leaves cost one monitor call.

    Worth returning rather than discovering: the free tier is 15 requests a
    minute, and a run that is one node too wide fails halfway through with the
    monitor failing open, which looks like approval.

    A FLOOR, not a forecast, and exact only for the departmental decomposer.
    This walks an evenly-divided tree, which is what `_departmental` builds by
    construction. A model divides unevenly and keeps going where the work keeps
    going, so real branches run deeper than the arithmetic here allows: a run
    predicted at 5 calls made closer to 17. Reported alongside
    `model_calls_exact` so nothing downstream shows a lower bound as a total.
    """
    layers, nodes, branches, leaves = 0, 1, 0, 0
    budget = req.budget_paise
    width = 1
    while layers < req.max_depth and budget // req.fan_out >= req.floor_paise:
        branches += width
        width *= req.fan_out
        budget //= req.fan_out
        layers += 1
    leaves = width
    return ((branches if using_model else 0)
            + (branches if req.critic else 0)     # one judgement per plan
            + (leaves if req.monitor else 0))


class IntakeRequest(BaseModel):
    """One sentence, plus whatever has been answered so far.

    The console posts this repeatedly with a growing `answers` map. Nothing here
    mints authority - the reply is a proposal, and a proposal becomes a mandate
    only when someone posts it to /runs.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000)
    answers: dict[str, Any] = Field(default_factory=dict)
    fan_out: int = Field(default=3, ge=2, le=6)
    max_depth: int = Field(default=8, ge=1, le=8)
    monitor: bool = True
    critic: bool = True


def _extractor():
    """The model that reads a request, or None if there is no model to ask.

    None is a supported answer, not a failure: `intake.read` then asks every
    question instead of only the missing ones, which is a worse front door and a
    working one.
    """
    if not have_model():
        return None
    try:
        from agent.nodes.intake_node import intake_chain
    except Exception:  # noqa: BLE001
        return None
    return lambda text: intake_chain.invoke({"request": text})


@app.post("/intake")
def read_request(req: IntakeRequest) -> dict[str, Any]:
    """Read a plain-language request and say what is still missing.

    Costs at most one model call, and answers without one. Deliberately not
    rate-limited the way /runs is: asking a clarifying question must stay cheaper
    than starting a run, or people will skip the questions.
    """
    reading = intake.read(
        req.text,
        req.answers,
        extract=_extractor(),
        defaults={"fan_out": req.fan_out, "max_depth": req.max_depth,
                  "monitor": req.monitor, "critic": req.critic},
    )
    payload = reading.to_dict()
    if reading.proposal:
        # Say what the run will cost before it is started, not after. The same
        # figure /runs returns, computed from the same function.
        proposal = RunRequest(**reading.proposal)
        using_model = proposal.decomposer != "departmental" and have_model()
        payload["expected_model_calls"] = _expected_model_calls(
            proposal, using_model=using_model)
        payload["model_calls_exact"] = not using_model
    return payload


@app.post("/runs")
def start_run(req: RunRequest) -> dict[str, Any]:
    rate_limit_runs()
    """Mint a mandate and run a funnel under it, in the background.

    Returns as soon as the mandate exists so the caller can attach to /stream and
    watch the tree grow. The run itself is a thread, not a task queue - this is a
    single-process gateway and pretending otherwise would be architecture theatre.
    """
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    root = token.mint(state.principal, budget_paise=req.budget_paise,
                      max_depth=req.max_depth, expires=expires)
    mid = token.mandate_id(root)
    state.ledger.open(mid, req.budget_paise)
    state.intents[mid] = req.task
    state.audit.append(
        mandate_id=mid, actor=state.principal.aip_id, tool="mandate",
        decision=Decision.ALLOWED, reason="mandate opened", context=req.task,
        amount_paise=req.budget_paise,
        detail={"max_depth": req.max_depth, "expires": expires.isoformat()},
    )

    # The rail's own ceiling, overridable because it is an OBSERVED limit rather
    # than a documented contract: Razorpay refused a Rs 6,00,000 order and we
    # inferred the rest. A deployment that knows better should be able to say so.
    try:
        max_leaf = int(os.environ.get("POCKETCHANGE_MAX_LEAF_PAISE", "")
                       or funnels.Bounds.max_leaf_paise)
    except ValueError:
        raise HTTPException(400, "POCKETCHANGE_MAX_LEAF_PAISE must be an integer") from None

    try:
        bounds = funnels.Bounds(max_depth=req.max_depth, max_fanout=req.fan_out,
                                max_nodes=128, decompose_floor_paise=req.floor_paise,
                                max_leaf_paise=max_leaf)
    except ValueError as exc:
        # Caught here rather than three payments in: a floor at or above the rail
        # cap describes a tree with no payable leaf anywhere in it.
        raise HTTPException(400, {"denied": str(exc)}) from exc
    decompose, decomposer_name = _choose_decomposer(req, bounds)

    critic = _make_critic(req) if req.critic else None

    if req.sourcing == "specific" and not (req.supplier or "").strip():
        # A missing name is not a narrower instruction. Refusing beats sending
        # the tree to a supplier nobody named.
        raise HTTPException(400, {"denied": "sourcing 'specific' must name a supplier"})

    run = funnels.Funnel(
        root, description=req.task, budget_paise=req.budget_paise,
        bounds=bounds, bus=events.bus,
        search=lambda node: _search_for(node),
        critic=critic,
        sourcing=req.sourcing,
        supplier=(req.supplier or "").strip() or None,
        run_id=mid,
    )

    def execute(node: funnels.TaskNode) -> str:
        return _pay_internal(node, mid, monitor=req.monitor)

    finished = threading.Event()

    def stalled() -> None:
        if finished.is_set():
            return
        # The work cannot be cancelled from here - a blocked HTTP call in a
        # daemon thread is not interruptible - but the console can at least stop
        # showing a live spinner over a run that is not moving.
        events.bus.emit(
            "root", events.BOUND_HIT, depth=0, fault=True, run_id=mid,
            reason=(f"no progress for {RUN_DEADLINE_SECONDS:.0f}s - the run is stalled, "
                    "most likely on a model call that never returned"),
        )

    watchdog = threading.Timer(RUN_DEADLINE_SECONDS, stalled)
    watchdog.daemon = True

    def drive() -> None:
        try:
            run.run(decompose, execute)
        except Exception as exc:  # noqa: BLE001 - a thread that dies silently is worse
            events.bus.emit("root", events.DENIED, depth=0, run_id=mid,
                            reason=f"run failed: {exc}")
        finally:
            finished.set()
            watchdog.cancel()

    watchdog.start()
    threading.Thread(target=drive, daemon=True, name=f"run-{mid[:8]}").start()

    return {
        "run_id": uuid.uuid4().hex[:12],
        "mandate_id": mid,
        "budget_paise": req.budget_paise,
        "task": req.task,
        "decomposer": decomposer_name,
        # Three states, not two. "off" is a choice someone made; "unconfigured"
        # is a layer that does not exist in this deployment. Reporting either as
        # a plain false let the console print "critic on" over a run no critic
        # ever read, and a ticked monitor box over payments nothing judged.
        "critic": ("bedrock" if critic
                   else "off" if not req.critic else "unconfigured"),
        "monitor": ("bedrock" if (req.monitor and monitor_judges(state.monitor))
                    else "off" if not req.monitor else "unconfigured"),
        "capabilities": capabilities(),
        "expected_model_calls": _expected_model_calls(
            req, using_model=decomposer_name.startswith("model")),
        # Exact only when the tree's shape is known in advance, which is to say
        # only when no model is choosing it.
        "model_calls_exact": not decomposer_name.startswith("model"),
    }


def _make_critic(req: "RunRequest"):
    """A model-backed critic, or none at all.

    Returns None rather than raising when no model is reachable: the critic is a
    second opinion, and a run must never fail to start because one is
    unavailable.
    """
    if not have_model():
        return None
    try:
        from agent.nodes.critic_node import make_critic

        return make_critic(intent=req.task)
    except Exception:  # noqa: BLE001
        return None


def _search_for(node) -> list[dict[str, Any]]:
    """Sourcing for a `best` node. Marked at the boundary, as everywhere else."""
    try:
        from agent import search as websearch

        hits = websearch.search(node.description)
        # Remember which suppliers address our agent instead of describing goods.
        # Recorded, never used to block: a phrase list is trivially evaded, and
        # the value is that the next payment to them carries this history.
        for supplier, url in websearch.suspicious(hits):
            try:
                state.counterparties.flag(supplier or url, counterparties.INJECTION)
            except Exception:  # noqa: BLE001
                pass
        return [h.as_prompt_dict() for h in hits]
    except Exception:  # noqa: BLE001 - the console must survive a missing agent package
        return []


def _pay_internal(node, mandate_id: str, *, monitor: bool = True) -> str:
    """A leaf pays, through the same nine steps as any external caller.

    Deliberately NOT a shortcut past the gateway's own checks - it calls the same
    verify, reserve, monitor and settle path. A console that spent money by a
    quieter route would be testing something other than the system.
    """
    # The A2A binding carries the token in the body, which is a supported path
    # rather than a back door - so this leaf authenticates exactly like a remote
    # agent would. The scope is minimal because bearer_token() only ever reads
    # headers and request.state.
    shim = _Request({
        "type": "http", "method": "POST", "path": "/pay",
        "headers": [], "query_string": b"", "state": {},
    })
    raw_token = token.serialize(node.token)
    body = PayRequest(
        amount_paise=node.budget_paise,
        cart={"node": node.id, "for": node.description},
        context=f"leaf of the funnel: {node.description}",
        aip_token=raw_token,
        # Named only when the task actually names one. A `catalogue` leaf has no
        # counterparty to speak of, and inventing one would put fiction into the
        # one record in this system that is supposed to be ours.
        counterparty=node.supplier or None,
    )
    reset = MONITOR_ON.set(monitor)
    try:
        response = pay(shim, body)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if exc.status_code == 202:
            raise funnels.Escalated(detail.get("reason", "held for a human")) from exc
        raise RuntimeError(detail.get("denied", str(exc.detail))) from exc
    finally:
        MONITOR_ON.reset(reset)

    _REPLAYABLE[response.audit_seq] = (raw_token, body, {
        "order_id": response.order_id,
        "amount_paise": response.amount_paise,
        "mandate_id": response.mandate_id,
        "audit_seq": response.audit_seq,
        "node": node.id,
    })
    return response.order_id


@app.get("/stream")
def stream(replay: bool = True) -> StreamingResponse:
    """Server-sent events: the funnel, as it happens.

    Deliberately separate from /audit. The audit trail is the durable record and
    is queried; this is a tail, it is lossy under backpressure, and nothing is
    reconciled against it. A dashboard that stops reading loses events and the
    payment run does not notice - which is the correct trade, because the
    alternative is a stalled browser tab holding up money.

    Replays the buffered history first, so a client connecting halfway through a
    run sees the tree so far instead of an empty screen.
    """

    def emit():
        yield ": connected\n\n"   # first byte, so buffering proxies let go
        for event in events.bus.stream(replay=replay):
            if event is None:
                yield ": keep-alive\n\n"
                continue
            yield f"event: {event.kind}\ndata: {json.dumps(event.as_dict())}\n\n"

    return StreamingResponse(emit(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.get("/events")
def recent_events(limit: int = 200) -> dict[str, Any]:
    """The same buffer as one response, for anything that cannot hold a
    connection open - a test, a curl, a dashboard poll."""
    history = events.bus.history()
    return {
        "events": [e.as_dict() for e in history[-limit:]],
        "total": len(history),
        "dropped": events.bus.dropped,
    }


class EventIn(BaseModel):
    """One event published by a funnel running outside this process.

    Unauthenticated on purpose, and only defensible because the stream is not a
    record: nothing is reconciled against it and the audit log does not read it.
    See events.RemoteBus for the same warning in the place that writes here.
    """

    model_config = ConfigDict(extra="ignore")

    node_id: str = Field(min_length=1, max_length=200)
    parent_id: str | None = None
    depth: int = Field(default=0, ge=0, le=32)
    kind: str
    detail: dict[str, Any] = Field(default_factory=dict)


@app.post("/events")
def publish_event(req: EventIn) -> dict[str, Any]:
    if req.kind not in events.KINDS:
        raise HTTPException(400, f"unknown event kind: {req.kind}")
    event = events.bus.publish(events.Event(
        node_id=req.node_id, parent_id=req.parent_id,
        depth=req.depth, kind=req.kind, detail=req.detail))
    return {"published": True, "at": event.at.isoformat()}


# Two paths, one handler. A managed edge may intercept /healthz and answer it
# with its own 404 before the request reaches the container - the giveaway is a
# response carrying no trace header, unlike every route that does arrive.
# /status is the one to use on a deployment; /healthz stays for local runs and
# anything expecting that name.
def have_model() -> bool:
    """Is there any model this process can actually reach?

    One definition, called from five places that each used to inline their own
    environment check. They drifted: two looked for a project id and two did
    not, so the same process could report a model on /status and refuse to use
    one on /runs.
    """
    return bedrock.available()


def capabilities() -> dict[str, Any]:
    """What is actually live in this process, named plainly.

    Every layer here degrades quietly by design - no key means a deterministic
    decomposer, a monitor that cannot judge, no critic, and a fake rail - and
    each of those is the right failure. What was NOT right is that the console
    could not tell the difference, so a run with none of them looked like a run
    with all of them.

    A deployment that cannot say which of its layers are real is not a system
    anyone should trust with money, so this is a first-class endpoint rather
    than a debug aid.
    """
    model = have_model()
    live = isinstance(state.rail, FakeRail) is False
    # Which ledger is actually holding the money, and - if a durable one was
    # asked for and not obtained - why not. This is reported for the same
    # reason as everything else here, but it is the one layer whose quiet
    # degradation survives inspection: a MemoryLedger answers every query
    # correctly right up until the process restarts and the record of who was
    # paid goes with it.
    durable_ledger = type(state.ledger).__name__ == "DynamoLedger"
    ledger_fault = ledger_fallback_reason()
    return {
        # The rail is the one that decides whether money is a simulation.
        "rail": "razorpay-test" if live else "fake",
        "payments_are_real": False,          # test mode, always. Never live keys.
        "model": model,
        "decomposer": "model" if model else "departmental",
        "monitor": "bedrock" if monitor_judges(state.monitor) else "unconfigured",
        "critic": "bedrock" if model else "unconfigured",
        "search": bool(os.environ.get("TAVILY_API_KEY")),
        "ledger": "dynamodb" if durable_ledger else "memory",
        # Present only when a durable ledger was configured and could not be
        # built. Not-configured is a choice; configured-and-broken is a fault.
        "ledger_fault": ledger_fault,
        # One sentence a console can print without having to reason about the
        # combination itself.
        "degraded": (None if model and live and not ledger_fault
                     else _degraded_because(model, live, ledger_fault)),
    }


def _degraded_because(model: bool, live_rail: bool,
                      ledger_fault: str | None = None) -> str:
    missing = []
    if not model:
        missing.append("no model is configured, so tasks split by a fixed "
                       "department list, nothing judges a payment, and no critic "
                       "reads a plan")
    if not live_rail:
        missing.append("no Razorpay test keys, so orders are simulated")
    if ledger_fault:
        # Deliberately blunter than the others. The rest of this list is
        # "a layer is absent"; this one is "a layer you asked for is broken
        # and the spend record dies with this process".
        missing.append("the durable ledger was configured but could not be "
                       f"built, so spend is held IN MEMORY and will not "
                       f"survive a restart ({ledger_fault})")
    return "; ".join(missing) + "."


@app.get("/status")
@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "rail": "fake" if isinstance(state.rail, FakeRail) else "razorpay-test",
        "audit_entries": len(state.audit),
        "audit_head": state.audit.head[:16],
        # Added because the console had no way to know a ticked "semantic
        # monitor" box was sitting over a monitor that could not judge.
        "capabilities": capabilities(),
    }


@app.post("/mandates", response_model=MandateResponse)
def create_mandate(req: MandateRequest) -> MandateResponse:
    """A human authorises spending. Returns the root token.

    This is the only place a ceiling can be set, because nothing appended to
    this token later can loosen a check written here.
    """
    expires = datetime.now(timezone.utc) + timedelta(seconds=req.ttl_seconds)
    root = token.mint(
        state.principal,
        budget_paise=req.budget_paise,
        max_depth=req.max_depth,
        expires=expires,
    )
    mid = token.mandate_id(root)
    state.ledger.open(mid, req.budget_paise)
    # Kept so the monitor can compare an action against the original authorisation.
    state.intents[mid] = req.purpose
    state.audit.append(
        mandate_id=mid,
        actor=state.principal.aip_id,
        tool="mandate",
        decision=Decision.ALLOWED,
        reason="mandate opened",
        context=req.purpose,
        amount_paise=req.budget_paise,
        detail={"max_depth": req.max_depth, "expires": expires.isoformat()},
    )
    return MandateResponse(
        mandate_id=mid,
        token=token.serialize(root),
        budget_paise=req.budget_paise,
        expires=expires,
        purpose=req.purpose,
    )


@app.post("/delegate")
def delegate(req: DelegateRequest) -> dict[str, Any]:
    """Narrow a token and hand it onward.

    Offered as a convenience; appending a block needs no key at all, so an agent
    can do this entirely offline. That is the property that makes the whole
    design work across trust boundaries - no issuer round-trip to get less
    authority.
    """
    parent = token.deserialize(req.token, state.root_public_key)

    # Delegation is itself a capability. Without this check any token holder can
    # mint children, so a shopper could mint itself a payer and the whole sibling
    # separation collapses - the agent reading seller-controlled text would be one
    # call away from being able to spend.
    #
    # The root authority block carries no tool check, so a human mandate can
    # always delegate. Anything attenuated below it must have been granted
    # `delegate` explicitly.
    try:
        # The depth checked is the CHILD's, not the parent's. Appending a block
        # is the act being authorised, so the question is "may a token exist at
        # depth N+1?" - answered now, at mint time. Checking the parent's own
        # depth instead would happily mint an over-deep token and only refuse it
        # later at /pay, which in a funnel means discovering the ceiling after
        # building the subtree beneath it.
        parent_depth = _depth(
            parent, req.depth, mid=token.mandate_id(parent),
            tool="delegate", context=req.context,
        )
        token.verify(parent, Operation("delegate", 0, depth=parent_depth + 1))
    except token.Denied as exc:
        entry = state.audit.append(
            mandate_id=token.mandate_id(parent),
            actor="unknown",
            tool="delegate",
            decision=Decision.DENIED,
            reason="this token is not permitted to delegate",
            context=req.context,
            detail={"requested_tools": req.tools},
        )
        raise HTTPException(
            403, {"denied": "token may not delegate", "audit_seq": entry.seq}
        ) from exc

    expires = None
    if req.ttl_seconds:
        expires = datetime.now(timezone.utc) + timedelta(seconds=req.ttl_seconds)

    # Second, independent bound. Attenuation stops a token being widened; the
    # registry stops one being minted wider than the agent was ever approved to
    # hold. They fail differently, so a bug in one does not disable the other.
    if req.to:
        try:
            state.registry.check_grant(req.to, tuple(req.tools), req.budget_paise)
        except ExceedsCard as exc:
            entry = state.audit.append(
                mandate_id=token.mandate_id(parent), actor=req.to, tool="delegate",
                decision=Decision.DENIED, reason=str(exc), context=req.context,
                amount_paise=req.budget_paise,
            )
            raise HTTPException(
                403, {"denied": str(exc), "audit_seq": entry.seq}
            ) from exc

    child = token.attenuate(
        parent,
        to=req.to or identity.ephemeral().aip_id,
        grant=Grant(
            tools=tuple(req.tools), budget_paise=req.budget_paise, expires=expires
        ),
        context=req.context,
    )
    state.audit.append(
        mandate_id=token.mandate_id(child),
        actor=req.to or "ephemeral",
        tool="delegate",
        decision=Decision.ALLOWED,
        reason=f"delegated {','.join(req.tools)}",
        context=req.context,
        amount_paise=req.budget_paise,
        detail={"expires": expires.isoformat() if expires else None},
    )
    return {
        "token": token.serialize(child),
        "mandate_id": token.mandate_id(child),
        "expires": expires.isoformat() if expires else None,
    }


@app.post("/pay", response_model=PayResponse)
def pay(
    request: Request,
    req: Annotated[PayRequest, Body()],
) -> PayResponse:
    """The money path. Eight steps, fail-closed at every one."""
    # Timed in two halves, deliberately.
    #
    # AIP reports 0.049 ms for verification and +2.35 ms end to end. We add a
    # model call, which is seconds. Quoting their number beside ours without
    # saying so would be dishonest - but the two layers are separable, and that
    # is the actual finding: enforcement is sub-millisecond and always runs;
    # judgement is slow and optional.
    started = perf_counter()

    # 1. extract - A2A carries the token in the body rather than a header
    if req.aip_token:
        request.state.a2a_token = req.aip_token
    raw = bearer_token(request)

    mid = "unknown"
    actor = "unknown"

    def deny(reason: str, status: int, detail: dict[str, Any] | None = None) -> HTTPException:
        entry = state.audit.append(
            mandate_id=mid,
            actor=actor,
            tool="pay",
            decision=Decision.DENIED,
            reason=reason,
            context=req.context,
            amount_paise=req.amount_paise,
            detail=detail or {},
        )
        _flag(req.counterparty, counterparties.REFUSED)
        return HTTPException(status, {"denied": reason, "audit_seq": entry.seq})

    # 2. authenticate
    try:
        bearer = token.deserialize(raw, state.root_public_key)
    except token.Forged as exc:
        raise deny(f"forged token: {exc}", 401) from exc

    mid = token.mandate_id(bearer)

    # 3. context - enforced here as well as at delegation, because the monitor
    #    will read it and an empty one makes both audit and monitor worthless
    if not req.context.strip():
        raise deny("empty context", 400)

    # 4. authorise - expiry, depth, scope, per-token budget
    depth = _depth(bearer, req.depth, mid=mid, tool="pay", context=req.context)
    op = Operation("pay", req.amount_paise, depth=depth, at=datetime.now(timezone.utc))
    try:
        token.verify(bearer, op)
    except token.Denied as exc:
        raise deny(str(exc), 403) from exc

    # 4b. the rules we chose, as opposed to the ones we proved.
    #
    # Everything above this line is a cryptographic guarantee: the signature, the
    # chain, the scope, the expiry, the depth. What follows is policy - decisions
    # someone made about who may do what - and it lives in policies/pay.cedar so
    # that it can be read without reading this function.
    #
    # The rule that used to be written here is the broker separation. A broker
    # must be able to confer `pay` on its sub-payers, and attenuation is
    # monotonic, so its own chain necessarily permits `pay` too. The token cannot
    # express "may grant but not spend", so the enforcement point does.
    refusal = cedar.decide(action="pay", role=_role(bearer), mandate_id=mid)
    if refusal is not None:
        raise deny(refusal.reason, refusal.status,
                   {"policy": refusal.policy_id,
                    "delegate": token.terminal_delegate(bearer)})

    # 5. reserve - cumulative spend and idempotency, in one lock
    #
    # The fingerprint covers the amount as well as the cart. With the cart alone
    # the same basket at a different price hashed identical, so the second call
    # was handed the first receipt and the price change vanished silently. The
    # replay branch below never compared amounts, so nothing else would have
    # caught it.
    idem = derive_key(mandate_id=mid, tool="pay",
                      payload={"cart": req.cart, "amount_paise": req.amount_paise})
    prior = state.replays.get(idem)
    if prior is not None:
        # Amount and cart both match a payment already made inside the TTL.
        # Ordinarily that is a retry and the honest answer is the original
        # receipt. But it is also what an intentional second purchase of the
        # same thing looks like, and the two are indistinguishable from the
        # request alone - so the caller has to say which it meant.
        #
        # It cannot say so in a way that spends money. `repurchase` buys a
        # question, not a charge: the occurrence number is assigned here, from
        # our own count of settled payments, and the settlement waits on a
        # person. An agent cannot reach the second charge by itself.
        if req.repurchase:
            return _open_repurchase(
                mid=mid, actor=actor, base_key=idem, req=req, deny=deny,
            )
        state.audit.append(
            mandate_id=mid, actor=actor, tool="pay", decision=Decision.ALLOWED,
            reason="replay: returning original result", context=req.context,
            amount_paise=req.amount_paise, detail={"idempotency_key": idem[:16]},
        )
        return PayResponse(**{**prior.result, "replayed": True})

    try:
        reservation = state.ledger.reserve(mid, req.amount_paise, idem)
    except InsufficientBudget as exc:
        raise deny(
            "cumulative budget exhausted",
            402,
            {"requested": exc.requested, "available": exc.available,
             "cap": exc.cap, "committed": exc.committed},
        ) from exc
    except UnknownMandate as exc:
        raise deny("no mandate open for this token", 404) from exc
    except LedgerError as exc:
        raise deny(f"ledger refused: {exc}", 409) from exc

    # 6. monitor - the semantic layer, after the rules have already passed
    #
    #    Everything above said yes. This asks a different question: does the
    #    action match what the human actually authorised, and does the agent's
    #    stated reason describe what it is doing? Rules cannot express "forty
    #    kilos of rice is not a household order".
    #
    #    Runs after the reservation so the budget is held while judgement takes
    #    place - otherwise a slow monitor reopens the race the reservation exists
    #    to close.
    enforcement_ms = (perf_counter() - started) * 1000
    monitor_started = perf_counter()
    # Two separate reasons there may be no judgement, and they must not be
    # collapsed. `wanted` is a choice someone made for this run; `able` is
    # whether a monitor capable of an opinion exists at all. An unconfigured
    # monitor answers ALLOW to everything, and recording that as an approval is
    # how a run with no second layer comes to look like one that passed it.
    monitor_wanted = MONITOR_ON.get()
    monitor_able = monitor_judges(state.monitor)
    monitor_ran = monitor_wanted and monitor_able

    ledger_now = state.ledger.state(mid)
    history = _counterparty_history(req.counterparty)
    if not monitor_ran:
        judgement = _ALLOW_UNJUDGED if not monitor_wanted else _ALLOW_UNCONFIGURED
    else:
        judgement = state.monitor.judge(Situation(
            intent=state.intents.get(mid, "unknown"),
            tool="pay",
            amount_paise=req.amount_paise,
            agent_claim=req.context,
            cart=req.cart if isinstance(req.cart, dict) else {},
            cap_paise=ledger_now.cap_paise,
            committed_paise=ledger_now.committed_paise,
            counterparty=req.counterparty or "",
            counterparty_history=history,
        ))
    monitor_ms = (perf_counter() - monitor_started) * 1000

    # DEFER: the monitor asked for a smaller version of this purchase.
    #
    # This verdict was offered to the model - the prompt lists all three - and
    # then ignored: the money path checked only ESCALATE, so a "spend less than
    # this" answer fell straight through and settled IN FULL, recording
    # `monitor: defer` in the audit as though it were an approval. The second
    # layer was asked for an opinion, gave one, and was overruled silently.
    #
    # Honouring it means moving the reservation down. There is no partial commit
    # in the ledger, so the held amount is released and re-reserved at the lower
    # figure. The idempotency key is derived from the cart, which has not
    # changed, so a replay still returns this reduced payment rather than
    # becoming a second one.
    settle_amount = req.amount_paise
    deferred_from: int | None = None

    if judgement.verdict is Verdict.DEFER and monitor_ran:
        suggested = judgement.suggested_amount_paise
        if suggested and 0 < suggested < req.amount_paise:
            try:
                state.ledger.release(reservation.id)
                reservation = state.ledger.reserve(mid, suggested, idem)
            except LedgerError as exc:
                raise deny(f"could not reduce to the suggested amount: {exc}", 409) from exc
            deferred_from, settle_amount = req.amount_paise, suggested
        else:
            # No usable figure. Inventing a safer amount on the model's behalf
            # would be a decision nobody authorised, so this becomes a question
            # for a person instead of a guess.
            judgement = Judgement(
                Verdict.ESCALATE,
                f"monitor deferred without a usable amount: {judgement.reason}",
                judgement.confidence,
            )

    if judgement.verdict is Verdict.ESCALATE:
        # Suspend, do not refuse. The reservation is HELD: releasing it would let
        # other spending consume the budget this payment is waiting on, so a
        # human saying yes ten minutes later could fail for reasons that had
        # nothing to do with their decision.
        approval = state.approvals.open(
            mandate_id=mid, reservation_id=reservation.id, idempotency_key=idem,
            amount_paise=settle_amount, cart=req.cart, context=req.context,
            reason=judgement.reason, counterparty=req.counterparty or "",
        )
        _flag(req.counterparty, counterparties.ESCALATED)
        entry = state.audit.append(
            mandate_id=mid, actor=actor, tool="pay",
            decision=Decision.ESCALATED,
            reason=f"monitor escalated: {judgement.reason}",
            context=req.context, amount_paise=req.amount_paise,
            detail={"approval_id": approval.id, "expires_at": approval.expires_at.isoformat()},
        )
        raise HTTPException(202, {
            "status": "pending_approval",
            "approval_id": approval.id,
            "reason": judgement.reason,
            "expires_at": approval.expires_at.isoformat(),
            "audit_seq": entry.seq,
        })

    # 7-9. charge, settle, record
    return PayResponse(**_settle(
        mandate_id=mid, reservation_id=reservation.id, idempotency_key=idem,
        amount_paise=settle_amount, cart=req.cart, context=req.context,
        counterparty=req.counterparty,
        detail={
            "monitor": (judgement.verdict.value if monitor_ran
                        else "skipped" if not monitor_wanted else "unconfigured"),
            "monitor_reason": judgement.reason,
            # Both figures, always. A payment reduced by judgement must never
            # read as one that was simply approved.
            "requested_paise": req.amount_paise,
            "deferred_from_paise": deferred_from,
            # Recorded so a run with judgement switched off can never be read as
            # a run that was judged and approved.
            "monitor_ran": monitor_ran,
            "divergence_reported": req.divergence,
            "unanimous": req.divergence == 0 if req.divergence is not None else None,
            "enforcement_ms": round(enforcement_ms, 3),
            "monitor_ms": round(monitor_ms, 3),
        },
    ))


def _open_repurchase(*, mid: str, actor: str, base_key: str, req, deny) -> None:
    """Ask a person to authorise buying the same cart a second time.

    Always raises - either 202 with an approval to answer, or a denial. There is
    no path through this function that charges anybody, which is the property
    that makes it safe for the agent to be the one asking.

    The occurrence number comes from our own settled-payment count, never from
    the request, so the key this eventually settles under is one the caller
    could not have named. Budget is reserved and HELD while the question is
    open, for the same reason an escalation holds it: a person saying yes ten
    minutes later should not fail because something else spent the money in the
    meantime.
    """
    occurrence = state.replays.next_occurrence(base_key)
    key = f"{base_key}#{occurrence}"

    # The repeat is a payment in its own right and is charged against the cap
    # like any other. A mandate with nothing left cannot buy the same thing
    # twice just because it once could.
    try:
        reservation = state.ledger.reserve(mid, req.amount_paise, key)
    except InsufficientBudget as exc:
        raise deny(
            "cumulative budget exhausted",
            402,
            {"requested": exc.requested, "available": exc.available,
             "cap": exc.cap, "committed": exc.committed},
        ) from exc
    except LedgerError as exc:
        raise deny(f"ledger refused: {exc}", 409) from exc

    reason = (f"repeat purchase: this cart was already paid for in this window; "
              f"occurrence {occurrence}")
    approval = state.approvals.open(
        mandate_id=mid, kind="repurchase", reservation_id=reservation.id,
        idempotency_key=key, amount_paise=req.amount_paise, cart=req.cart,
        context=req.context, reason=reason, counterparty=req.counterparty or "",
    )
    entry = state.audit.append(
        mandate_id=mid, actor=actor, tool="pay", decision=Decision.ESCALATED,
        reason=reason, context=req.context, amount_paise=req.amount_paise,
        detail={"approval_id": approval.id, "occurrence": occurrence,
                "idempotency_key": key[:16],
                "expires_at": approval.expires_at.isoformat()},
    )
    raise HTTPException(202, {
        "status": "pending_approval",
        "approval_id": approval.id,
        "reason": reason,
        "occurrence": occurrence,
        "expires_at": approval.expires_at.isoformat(),
        "audit_seq": entry.seq,
    })


def _settle(
    *, mandate_id: str, reservation_id: str, idempotency_key: str, amount_paise: int,
    cart: dict[str, Any], context: str, detail: dict[str, Any],
    counterparty: str | None = None,
) -> dict[str, Any]:
    """Charge, settle and record. Shared by /pay and an approved escalation.

    Extracted so a payment resumed by a human takes exactly the same path as one
    that was never paused - a resume that used a different code path would be a
    second implementation of the most sensitive step in the system.
    """
    try:
        order = state.rail.create_order(
            amount_paise=amount_paise,
            receipt=idempotency_key,
            notes={"mandate": mandate_id[:16], "context": context[:200]},
        )
    except PaymentError as exc:
        # The charge never happened, so the budget goes back and the idempotency
        # key is released, keeping the action retryable.
        state.ledger.release(reservation_id)
        state.replays.forget(idempotency_key)
        entry = state.audit.append(
            mandate_id=mandate_id, actor="gateway", tool="pay",
            decision=Decision.DENIED, reason=f"payment rail failed: {exc}",
            context=context, amount_paise=amount_paise,
        )
        raise HTTPException(
            502, {"denied": f"payment rail failed: {exc}", "audit_seq": entry.seq}
        ) from exc

    ledger_state = state.ledger.commit(reservation_id)

    # Recorded only for money that actually cleared. A reservation that was
    # released, or a payment the rail refused, is not a dealing with anybody.
    if counterparty:
        try:
            state.counterparties.record(
                counterparty, amount_paise=amount_paise, mandate_id=mandate_id)
        except Exception:  # noqa: BLE001 - the payment already succeeded
            pass
    entry = state.audit.append(
        mandate_id=mandate_id, actor="gateway", tool="pay",
        decision=Decision.ALLOWED, reason="paid", context=context,
        amount_paise=amount_paise,
        detail={"order_id": order.id, "remaining": ledger_state.available_paise, **detail},
    )
    result = {
        "order_id": order.id,
        "amount_paise": amount_paise,
        "mandate_id": mandate_id,
        "remaining_paise": ledger_state.available_paise,
        "replayed": False,
        "audit_seq": entry.seq,
    }
    state.replays.record(idempotency_key, result)
    return result


class PayoutRequest(BaseModel):
    # Unknown fields are rejected, not ignored. A request carrying a field
    # this gateway does not understand is a protocol mismatch, and silently
    # dropping it is how a caller ends up believing a constraint applied.
    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1)
    amount_paise: int = Field(gt=0)
    context: str = Field(min_length=1)
    depth: int | None = Field(
        default=None, ge=0,
        description="Ignored; depth is derived from the token chain, not claimed.",
    )
    aip_token: str | None = None


@app.post("/payout")
def payout(request: Request, req: Annotated[PayoutRequest, Body()]) -> dict[str, Any]:
    """Send funds to an account directly.

    This endpoint exists so an injected listing can genuinely persuade an agent
    to call it. It runs the same check order as /pay - it is not a trap that
    always refuses. It refuses because no token in this system is ever minted
    with `tool:payout` in scope, so step 4 has nothing to authorise it.

    That distinction matters for the demo: the model complies fully, the request
    is well formed, the endpoint is real, and the money still does not move.
    """
    if req.aip_token:
        request.state.a2a_token = req.aip_token
    raw = bearer_token(request)

    mid = "unknown"

    def deny(reason: str, status: int) -> HTTPException:
        entry = state.audit.append(
            mandate_id=mid, actor="unknown", tool="payout",
            decision=Decision.DENIED, reason=reason, context=req.context,
            amount_paise=req.amount_paise, detail={"account": req.account},
        )
        return HTTPException(status, {"denied": reason, "audit_seq": entry.seq})

    try:
        bearer = token.deserialize(raw, state.root_public_key)
    except token.Forged as exc:
        raise deny(f"forged token: {exc}", 401) from exc

    mid = token.mandate_id(bearer)

    if not req.context.strip():
        raise deny("empty context", 400)

    depth = _depth(bearer, req.depth, mid=mid, tool="payout", context=req.context)
    op = Operation("payout", req.amount_paise, depth=depth, at=datetime.now(timezone.utc))
    try:
        token.verify(bearer, op)
    except token.Denied as exc:
        raise deny("scope never granted: no block in this chain permits payout", 403) from exc

    # Unreachable with any token this gateway mints, because none carries
    # `tool:payout` in scope and step 4 has just refused it. Kept, and moved into
    # the policy file, because a path that cannot happen today is exactly the
    # path that happens after someone widens a scope next month - and when that
    # day comes, the switch should be somewhere a person would look.
    refusal = cedar.decide(action="payout", role=_role(bearer), mandate_id=mid)
    if refusal is not None:
        raise deny(refusal.reason, refusal.status)
    raise deny("payout reached the end of the money path without settling", 500)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern="^(approve|deny)$")
    by: str = Field(default="human", min_length=1)
    note: str = Field(default="")


def _sweep_expired() -> None:
    """Release the budget held by approvals nobody answered.

    Held budget that never expires is a way to lock up a mandate by escalating
    and walking away.
    """
    for approval in state.approvals.expired():
        if approval.kind == "policy":
            state.audit.append(
                mandate_id=approval.mandate_id, actor="timeout", tool="standing",
                decision=Decision.DENIED,
                reason="policy expired before anyone approved it",
                context=approval.context,
                detail={"approval_id": approval.id, "standing_order": approval.subject_id},
            )
            continue
        try:
            state.ledger.release(approval.reservation_id)
        except LedgerError:
            pass
        state.replays.forget(approval.idempotency_key)
        state.audit.append(
            mandate_id=approval.mandate_id, actor="timeout", tool="pay",
            decision=Decision.DENIED,
            reason="approval expired before anyone answered",
            context=approval.context, amount_paise=approval.amount_paise,
            detail={"approval_id": approval.id},
        )


@app.get("/approvals")
def list_approvals() -> dict[str, Any]:
    """Payments waiting on a person."""
    _sweep_expired()
    return {"pending": [a.summary() for a in state.approvals.pending()]}


@app.post("/approvals/{approval_id}")
def decide_approval(approval_id: str, req: ApprovalDecision) -> dict[str, Any]:
    """A human answers. Approving resumes the payment from where it stopped."""
    _sweep_expired()
    try:
        approval = state.approvals.get(approval_id)
    except UnknownApproval as exc:
        raise HTTPException(404, str(exc)) from exc

    approved = req.decision == "approve"
    try:
        approval = state.approvals.resolve(
            approval_id, approved=approved, by=req.by, note=req.note
        )
    except AlreadyResolved as exc:
        raise HTTPException(409, str(exc)) from exc

    # A policy approval activates a standing order. Nothing is held while it
    # waits, so a denial simply leaves it inert - there is no budget to release.
    if approval.kind == "policy":
        entry = state.audit.append(
            mandate_id=approval.mandate_id, actor=req.by, tool="standing",
            decision=Decision.ALLOWED if approved else Decision.DENIED,
            reason=f"policy {'approved' if approved else 'refused'} by {req.by}",
            context=approval.context, amount_paise=approval.amount_paise,
            detail={"approval_id": approval.id, "standing_order": approval.subject_id},
        )
        if approved:
            state.memory.approve(approval.subject_id, by=req.by)
        return {
            "approval_id": approval.id, "kind": "policy",
            "status": "approved" if approved else "denied",
            "standing_order": approval.subject_id, "audit_seq": entry.seq,
        }

    if not approved:
        state.ledger.release(approval.reservation_id)
        state.replays.forget(approval.idempotency_key)
        entry = state.audit.append(
            mandate_id=approval.mandate_id, actor=req.by, tool="pay",
            decision=Decision.DENIED, reason=f"refused by {req.by}: {req.note}"[:160],
            context=approval.context, amount_paise=approval.amount_paise,
            detail={"approval_id": approval.id},
        )
        # The strongest signal in the book: not a rule, not a model - a person
        # looked at this counterparty and said no.
        _flag(approval.counterparty, counterparties.VETOED)
        return {"approval_id": approval.id, "status": "denied", "audit_seq": entry.seq}

    # Why this payment stopped, recorded as what it actually was. A repeat
    # purchase was held because it was a repeat, not because the monitor
    # objected to it - writing "monitor: escalate" over both would put a
    # judgement in the log that no monitor ever made.
    held_by = "repurchase" if approval.kind == "repurchase" else "escalate"
    result = _settle(
        mandate_id=approval.mandate_id, reservation_id=approval.reservation_id,
        idempotency_key=approval.idempotency_key, amount_paise=approval.amount_paise,
        cart=approval.cart, context=approval.context,
        counterparty=approval.counterparty,
        detail={"approval_id": approval.id, "approved_by": req.by,
                "held_by": held_by, "monitor_reason": approval.reason,
                "monitor": "escalate" if held_by == "escalate" else "not-consulted"},
    )
    return {"approval_id": approval.id, "status": "approved", **result}


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    department: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    identity: str = Field(min_length=1)
    capabilities: list[str] = Field(min_length=1)
    max_budget_paise: int = Field(ge=0)
    model: str = ""
    description: str = ""


@app.get("/agents")
def discover_agents(capability: str | None = None, department: str | None = None) -> dict[str, Any]:
    """Cross-department discovery: who can do this, and who owns them."""
    found = state.registry.discover(capability=capability, department=department)
    return {
        "departments": state.registry.departments(),
        "agents": [card.summary() for card in found],
    }


@app.get("/agents/{name}")
def get_agent(name: str, version: str | None = None) -> dict[str, Any]:
    try:
        return state.registry.get(name, version).summary()
    except UnknownAgent as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/agents")
def publish_agent(req: PublishRequest) -> dict[str, Any]:
    """Publish a version. Re-publishing an existing one is refused.

    An agent whose prompt or capabilities changed is a different agent;
    overwriting in place would leave the audit trail unable to say which one
    spent the money.
    """
    try:
        card = state.registry.publish(AgentCard(
            name=req.name, version=req.version, department=req.department,
            owner=req.owner, identity=req.identity,
            capabilities=tuple(req.capabilities),
            max_budget_paise=req.max_budget_paise,
            model=req.model, description=req.description,
        ))
    except Exception as exc:  # noqa: BLE001 - registry raises its own types
        raise HTTPException(409, str(exc)) from exc
    return card.summary()


class StandingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1)
    department: str = Field(min_length=1)
    period: str = Field(default="month", pattern="^(week|month|quarter)$")
    budget_paise: int = Field(gt=0)


@app.post("/standing")
def register_standing_policy(req: StandingRequest) -> dict[str, Any]:
    """Draft a standing policy. It does nothing until a person approves it.

    A standing instruction turns one sentence into recurring authority, and does
    so in the one mode where nobody is watching. So it enters the same approvals
    queue a paused payment does, and waits.
    """
    from agent.nodes import establish

    try:
        order = establish(
            instruction=req.instruction, department=req.department,
            period=req.period, budget_paise=req.budget_paise,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    state.memory.remember(order)
    approval = state.approvals.open(
        kind="policy", subject_id=order.id, mandate_id=order.id,
        amount_paise=order.period_budget_paise, cart={},
        context=req.instruction,
        reason=f"a standing policy would spend up to {order.period_budget_paise}p "
               f"per {order.period}, unattended",
    )
    entry = state.audit.append(
        mandate_id=order.id, actor=req.department, tool="standing",
        decision=Decision.ESCALATED, reason="policy drafted, awaiting approval",
        context=req.instruction, amount_paise=order.period_budget_paise,
        detail={"approval_id": approval.id, "rules": len(order.rules),
                "rejected": [n for n in order.notes if n.startswith("rejected")]},
    )
    return {
        "standing_order": order.as_dict(),
        "approval_id": approval.id,
        "status": "pending_approval",
        "audit_seq": entry.seq,
    }


@app.get("/standing")
def list_standing(department: str | None = None,
                  include_pending: bool = False) -> dict[str, Any]:
    """Policies. Approved and active by default; add include_pending to see the
    ones still waiting on a person.

    The default stays narrow because the tick loop reads this, and a tick that
    acted on an unapproved policy would defeat the point of drafting one. The
    console asks for both, because a policy sitting unapproved is the thing most
    worth looking at.
    """
    orders = state.memory.standing_orders(department, include_pending=include_pending)
    return {"orders": [o.as_dict() for o in orders]}


@app.get("/mandates/{mandate_id}")
def mandate_state(mandate_id: str) -> dict[str, Any]:
    try:
        s = state.ledger.state(mandate_id)
    except UnknownMandate as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "mandate_id": s.mandate_id,
        "cap_paise": s.cap_paise,
        "committed_paise": s.committed_paise,
        "reserved_paise": s.reserved_paise,
        "available_paise": s.available_paise,
    }


def _flag(counterparty: str | None, kind: str) -> None:
    """Remember that something went wrong, without ever failing the request.

    Reputation is advisory. A book that is unreachable must not turn a refusal
    into a 500, nor a payment into one.
    """
    if not counterparty:
        return
    try:
        state.counterparties.flag(counterparty, kind)
    except Exception:  # noqa: BLE001
        pass


def _counterparty_history(counterparty: str | None) -> str:
    """Our own record of this party, in a sentence a model can weigh.

    Looked up BEFORE the payment is recorded, so "never paid before" is true of
    the moment the monitor is asked rather than of the moment after.
    """
    if not counterparty:
        return counterparties.UNKNOWN
    try:
        row = state.counterparties.lookup(counterparty)
    except Exception:  # noqa: BLE001 - a reputation lookup must never fail a payment
        return "record unavailable"
    return row.describe() if row else counterparties.NEW


@app.get("/counterparties")
def counterparty_book() -> dict[str, Any]:
    """Everyone this gateway has actually paid.

    The point of contrast with `seller_reputation`, which reports what a seller
    says about itself. Nothing here came from outside this process.
    """
    try:
        rows = state.counterparties.all()
    except Exception:  # noqa: BLE001
        rows = []
    return {"counterparties": [r.as_dict() for r in rows], "count": len(rows)}


@app.get("/facts")
def facts() -> dict[str, Any]:
    """What this system IS, derived rather than declared.

    The landing page used to hardcode its own headline numbers, and they went
    three releases stale: it advertised 258 tests against 372, 9 of 12 SoK
    vectors against 10, and a 0.164 ms enforcement figure that only holds for
    the in-memory ledger - a number the console beside it contradicted with
    3,600 ms. Every one of those drifted because a human had to remember to
    change them.

    So they are computed here, from the same objects the runtime uses. A bound
    added to funnel.Bounds shows up on the front page without anyone editing
    the front page.
    """
    from eval import vectors as sok

    # Five are fields on Bounds; cycles and conservation are enforced in _check
    # without a configurable limit, and are bounds all the same.
    bounds = len(funnels.Bounds.__dataclass_fields__) + 2

    live: dict[str, Any] = {}
    try:
        entries = state.audit.entries()
        live["mandates"] = sum(1 for e in entries if e.tool == "mandate")
        live["payments"] = sum(
            1 for e in entries if e.tool == "pay" and e.decision is Decision.ALLOWED)
        live["refused"] = sum(1 for e in entries if e.decision is Decision.DENIED)
        live["held"] = sum(1 for e in entries if e.decision is Decision.ESCALATED)
        live["audit_entries"] = len(state.audit)
        try:
            state.audit.verify()
            live["chain_intact"] = True
        except AuditTampered:
            live["chain_intact"] = False
    except Exception:  # noqa: BLE001 - the front page must not depend on this
        pass

    try:
        book = state.counterparties.all()
        live["counterparties"] = len(book)
        live["with_concerns"] = sum(1 for c in book if c.trouble)
    except Exception:  # noqa: BLE001
        pass

    return {
        "bounds": bounds,
        "sok": {"defended": len(sok.applicable()), "total": len(sok.VECTORS)},
        "verdicts": [v.value for v in Verdict],
        "rail": "razorpay-test" if not isinstance(state.rail, FakeRail) else "fake",
        "models": {
            "primary": "bedrock" if bedrock.available() else "none",
            "region": bedrock.region(),
            "fallbacks": [p.name for p in providers.configured()],
        },
        "live": live,
    }


@app.get("/audit/verify")
def verify_audit() -> dict[str, Any]:
    """Recompute the hash chain.

    AuditLog.verify() and .head have existed since day three with no way to reach
    them, which made the tamper-evidence a claim rather than a check.

    Be precise about what this proves. Each entry stores the hash of the one
    before it, so altering or removing an entry in place breaks every hash after
    it and is caught here. It does NOT prove the log was never rewritten from
    genesis - that needs an anchor outside this process, which we do not have.
    Saying otherwise would be exactly the overreach this project avoids.
    """
    try:
        state.audit.verify()
    except AuditTampered as exc:
        return {
            "ok": False,
            "entries": len(state.audit),
            "head": state.audit.head,
            "broken_at": str(exc),
            "proves": "nothing - the chain is broken",
        }
    return {
        "ok": True,
        "entries": len(state.audit),
        "head": state.audit.head,
        "proves": (
            "no entry was altered or removed in place. Not that the whole log "
            "was never rewritten - that needs an external anchor."
        ),
    }


@app.get("/incident/{audit_seq}")
def incident(audit_seq: int) -> dict[str, Any]:
    """Everything known about one payment, assembled for someone investigating it.

    The pieces already existed and were scattered: the entry is in /audit, the
    chain around it needs /audit/verify, what the monitor thought is buried in
    one entry's detail, and the delegation that led to it is spread across every
    earlier entry sharing a mandate. Answering "what happened here, and can I
    trust the answer" meant four calls and joining them by hand.

    Read-only, and deliberately so. An incident view that can change anything is
    a second way to move money, and this system already has one too many places
    where authority could accidentally live.

    It reports what it CANNOT establish as plainly as what it can. An audit tool
    that quietly presents a broken chain as a clean history is worse than no
    tool, because it converts an unanswered question into a false answer - the
    same failure as an unconfigured monitor recording `allow`.
    """
    entries = state.audit.entries()
    match = next((e for e in entries if e.seq == audit_seq), None)
    if match is None:
        raise HTTPException(404, f"no audit entry at seq {audit_seq}")

    # Is the record itself trustworthy? Asked first, because every answer below
    # is read out of this log and none of them mean anything if it was altered.
    try:
        state.audit.verify()
        chain_ok, broken_at = True, None
    except AuditTampered as exc:
        chain_ok, broken_at = False, str(exc)

    same_mandate = [e for e in entries if e.mandate_id == match.mandate_id]

    def summarise(e) -> dict[str, Any]:
        return {
            "seq": e.seq,
            "at": e.at.isoformat(),
            "actor": e.actor,
            "tool": e.tool,
            "decision": e.decision.value,
            "reason": e.reason,
            "amount_paise": e.amount_paise,
        }

    detail = match.detail or {}
    # Three states, not two. "The monitor was never consulted" and "the monitor
    # allowed this" are different facts and must not be printed the same way.
    verdict = detail.get("monitor")
    judgement = {
        "verdict": verdict or "not recorded",
        "reason": detail.get("monitor_reason", ""),
        "means": {
            "allow": "a monitor read this and approved it",
            "escalate": "a monitor objected and a person decided",
            "defer": "a monitor asked for a smaller amount",
            "skipped": "monitoring was switched off for this run",
            "unconfigured": "no monitor existed; this was NOT reviewed",
            "not-consulted": "held for another reason; no monitor judged it",
        }.get(verdict, "no verdict was recorded against this payment"),
    }

    return {
        "entry": summarise(match) | {"context": match.context, "detail": detail},
        "mandate": {
            "id": match.mandate_id,
            "intent": state.intents.get(match.mandate_id, "not recorded"),
            "ledger": _ledger_or_none(match.mandate_id),
        },
        # The chain of authority that reached this payment, in order.
        "leading_to_it": [summarise(e) for e in same_mandate if e.seq < match.seq],
        "after_it": [summarise(e) for e in same_mandate if e.seq > match.seq],
        "judgement": judgement,
        "record": {
            "chain_intact": chain_ok,
            "broken_at": broken_at,
            "head": state.audit.head[:16],
            "prev_hash": match.prev_hash[:16],
            "proves": (
                "no entry was altered or removed in place"
                if chain_ok else "nothing - the chain is broken"
            ),
        },
        "replayable": audit_seq in _REPLAYABLE,
    }


def _ledger_or_none(mandate_id: str) -> dict[str, Any] | None:
    """The mandate's budget state, or None if it is no longer known.

    A mandate can outlive the ledger's memory of it. Returning None says so
    rather than reporting zeros, which would read as a mandate that spent
    nothing.
    """
    try:
        st = state.ledger.state(mandate_id)
    except (UnknownMandate, LedgerError):
        return None
    return {
        "cap_paise": st.cap_paise,
        "committed_paise": st.committed_paise,
    }


@app.post("/replay/{audit_seq}")
def replay_payment(audit_seq: int) -> dict[str, Any]:
    """Send a settled payment again, byte for byte.

    Razorpay publishes idempotency for payouts, transfers and refunds and none
    for Orders creation, which is the endpoint a checkout uses. This is the
    runtime closing that gap: the key is derived from the request itself
    (`idempotency.derive_key`), never supplied by the caller, so a replay cannot
    be dressed up as a fresh purchase by changing a header.

    Only payments this gateway drove are replayable - it re-issues the exact
    recorded request, and it holds the token to do so. A payment made by an
    external agent is not here, because we never kept its token.
    """
    recorded = _REPLAYABLE.get(audit_seq)
    if recorded is None:
        raise HTTPException(404, f"no replayable payment at audit seq {audit_seq}")

    raw, body, first = recorded
    before = state.ledger.state(body_mandate := first["mandate_id"])

    shim = _Request({
        "type": "http", "method": "POST", "path": "/pay",
        "headers": [], "query_string": b"", "state": {},
    })
    response = pay(shim, body)
    after = state.ledger.state(body_mandate)

    return {
        "first": first,
        "second": {
            "order_id": response.order_id,
            "replayed": response.replayed,
            "audit_seq": response.audit_seq,
        },
        "same_order": response.order_id == first["order_id"],
        "committed_before_paise": before.committed_paise,
        "committed_after_paise": after.committed_paise,
        # NOT `after.committed_paise != before.committed_paise`. That diffs the
        # whole mandate, so any other payment settling on it between the two
        # reads above - nothing to do with this replay - reads identically to
        # this replay having charged again. `response.replayed` is the signal
        # that is actually about this call: pay() sets it inside the same
        # idempotency check that decides whether a new reservation opens at
        # all, so it is false exactly when this call moved money, whatever
        # else happened to the mandate in the meantime.
        "charged_twice": not response.replayed,
    }


@app.get("/audit")
def audit_trail(mandate_id: str | None = None) -> dict[str, Any]:
    """The trail Razorpay's brief asks to see. Denials included, deliberately."""
    entries = state.audit.entries(mandate_id)
    try:
        state.audit.verify()
        intact = True
    except Exception:  # noqa: BLE001
        intact = False
    return {
        "intact": intact,
        "head": state.audit.head,
        "count": len(entries),
        "entries": [
            {
                "seq": e.seq,
                "at": e.at.isoformat(),
                # Needed to look up the ledger for a run. Its absence is why the
                # console showed no ceiling: it finds the mandate from the entry
                # written when the human authorised the spend, and that entry did
                # not say which mandate it opened.
                "mandate_id": e.mandate_id,
                "tool": e.tool,
                "decision": e.decision.value,
                "reason": e.reason,
                "context": e.context,
                "amount_paise": e.amount_paise,
                "detail": e.detail,
            }
            for e in entries
        ],
    }


# Last line on purpose: a catch-all mount registered before the API routes would
# swallow them. Everything above is declared, so "/" can now be the console.
_mount_console()
