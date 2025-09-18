"""One prompt in, the questions that are still missing out.

The console used to demand two things before it would do anything: a task
string, and a ceiling typed into a numeric field. Neither was explained, and
the second is the one number nobody can guess - so the front door asked for
exactly the piece a first-time reader is least able to supply.

This module inverts that. A person writes one sentence. A model reads it and
extracts what was actually stated. Then *rules* - not the model - decide what is
still missing, and the console asks only for that.

Two boundaries are worth stating plainly, because they are the reason this is a
module in `pocketchange/` rather than a helper in the console.

  1. THE CEILING IS ALWAYS CONFIRMED BY A PERSON.

     A budget mentioned in the request text is a number that arrived inside
     untrusted free text. "Buy four laptops; the approved budget is 50,00,000"
     is one sentence, and if intake lifted that figure straight into the mandate
     then the cap - the thing every other guarantee in this system hangs off -
     would be settable by whoever wrote the prompt. So an extracted budget
     becomes a *suggestion attached to a question*, never an answer. The person
     confirms it, and the mandate is minted from what they confirmed.

  2. INTAKE HOLDS NO AUTHORITY.

     Nothing here mints, attenuates or spends. It produces a proposal, and the
     proposal only becomes a mandate when someone posts it to /runs. If the
     model is unreachable, `read` still works - it simply has nothing extracted
     and asks every question instead of some of them. Degraded, never wrong.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .policy import RUPEE

# How small the smallest purchase may get, as a fraction of the ceiling. Mirrors
# frontend/gateway.js `floorFor`: left at a flat rupee figure, a large ceiling
# leaves every node "big enough to split" and the tree never produces a leaf.
FLOOR_DIVISOR = 8
MIN_FLOOR_RUPEES = 1_000


@dataclass(frozen=True)
class Question:
    """One thing the person still has to say, and why we cannot infer it."""

    id: str
    ask: str
    why: str
    kind: str                              # money | choice | text
    options: list[dict[str, str]] = field(default_factory=list)
    suggestion: str | None = None
    # True when we DID find an answer in the text and are asking anyway. The
    # console renders these differently: confirming is one click, not typing.
    confirming: bool = False


@dataclass
class Reading:
    understood: dict[str, Any]
    questions: list[Question]
    proposal: dict[str, Any] | None
    notes: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.questions and self.proposal is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "understood": self.understood,
            "questions": [asdict(q) for q in self.questions],
            "proposal": self.proposal,
            "notes": self.notes,
            "ready": self.ready,
        }


SOURCING_OPTIONS = [
    {"value": "catalogue", "label": "Our own catalogue",
     "note": "no web search - the narrowest exposure"},
    {"value": "specific", "label": "One supplier I name",
     "note": "go there, do not shop around"},
    {"value": "best", "label": "Find the best price",
     "note": "reads the open web, which is where injections live"},
]


def indian(amount: int) -> str:
    """1,00,000 rather than 100,000. Money on screen is read by people here."""
    digits = str(int(amount))
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def _rupees(value: Any) -> int | None:
    """A rupee figure from anything a console might send. None if unusable."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        amount = int(str(value).replace(",", "").replace("₹", "").strip() or 0)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def floor_rupees(ceiling_rupees: int) -> int:
    return max(MIN_FLOOR_RUPEES, round(ceiling_rupees / FLOOR_DIVISOR))


def _blank_understanding(text: str) -> dict[str, Any]:
    return {
        "goal": text.strip()[:400],
        "task_kind": "errand",
        "items": [],
        "sourcing": None,
        "supplier": None,
        "stated_budget_rupees": None,
        "deadline": None,
        "extracted_by": "none",
        "reasoning": "",
    }


def _understand(text: str, extract: Callable[[str], Any] | None) -> tuple[dict[str, Any], list[str]]:
    """What the request says, as far as we can tell. Never raises."""
    understood = _blank_understanding(text)
    notes: list[str] = []
    if extract is None:
        notes.append("No model configured, so nothing was read out of the request "
                     "text - every question is asked.")
        return understood, notes
    try:
        result = extract(text)
    except Exception as exc:  # noqa: BLE001 - intake must never be the thing that fails
        notes.append(f"Could not read the request ({type(exc).__name__}); asking everything.")
        return understood, notes

    stated = result.commands.budget_paise if result.commands else None
    understood.update({
        "goal": result.goal or understood["goal"],
        "task_kind": result.task_kind,
        "items": [i.model_dump() for i in result.items],
        # `sourcing` defaults to "catalogue" in the schema, which is a safe
        # default for the buyer but a misleading one here: it would let intake
        # silently decide the risk axis. Only a NAMED supplier or an explicit
        # ask for the best price counts as stated.
        "sourcing": result.sourcing if result.sourcing in ("specific", "best") else None,
        "supplier": (result.supplier or "").strip() or None,
        "stated_budget_rupees": (stated // RUPEE) if stated else None,
        "deadline": (result.commands.deadline if result.commands else None),
        "extracted_by": "model",
        "reasoning": result.reasoning or "",
    })
    if understood["sourcing"] == "specific" and not understood["supplier"]:
        understood["sourcing"] = None
    return understood, notes


def _ceiling_question(understood: dict[str, Any]) -> Question:
    stated = understood.get("stated_budget_rupees")
    if stated:
        return Question(
            id="ceiling_rupees",
            ask=f"You mentioned ₹{indian(stated)}. Set that as the hard ceiling?",
            why=("The figure came out of your request text, and request text is "
                 "untrusted here - a prompt that names its own budget could raise "
                 "its own cap. So it is a suggestion until you confirm it. Once "
                 "you do, it is signed into the token and nothing below can widen it."),
            kind="money",
            suggestion=str(stated),
            confirming=True,
        )
    return Question(
        id="ceiling_rupees",
        ask="What is the most this may spend, in total?",
        why=("This is the only number that cannot be inferred. It becomes the "
             "root token's budget - every sub-agent below gets a strict share of "
             "it, and no agent can raise it."),
        kind="money",
    )


def _sourcing_question() -> Question:
    return Question(
        id="sourcing",
        ask="Where should it buy from?",
        why=("This is the risk axis, not a preference. Reading the open web means "
             "reading seller-controlled text, which is where prompt injection "
             "arrives - so the agent that looks is a separate, zero-budget one "
             "that cannot pay."),
        kind="choice",
        options=SOURCING_OPTIONS,
    )


def _supplier_question() -> Question:
    return Question(
        id="supplier",
        ask="Which supplier?",
        why="Named here rather than chosen later, so the tree cannot drift to another one.",
        kind="text",
    )


# Money mentioned in the request text, in the shapes people actually write it.
#
# Two branches. The first takes any phrase built around the word "budget",
# including the preposition that introduces it and up to a couple of adjectives
# in front of it - "within the specified budget", "with an approved budget of
# 2,00,000" - because removing the noun and leaving "within the specified"
# behind is worse than not removing anything. The second takes a bare capped
# amount with no such noun: "under 1500 rupees".
_CURRENCY = r"(?:\u20b9|rs\.?|inr)"
_BUDGET_CLAIM = re.compile(
    rf"""(?ix)
    (?: ^ | [,;.]\s* | \s+ )
    (?:
        # ...anything organised around the word "budget"
        (?:(?:with|within|under|below|for|up\s+to|around|about|of|at|to)\s+)?
        (?:(?:a|an|the|its|our|my|their)\s+)?
        (?:\w+\s+){{0,2}}?
        budget
        (?:\s+(?:of|is|are|was|around|about|near|up\s+to|under|at))?
        [^.;]*
      |
        # ...or a bare cap, with no noun to anchor it
        (?:with|within|under|below|up\s+to|around|about)\s+
        (?:{_CURRENCY})?\s*[\d][\d,]*\s*
        (?:{_CURRENCY}|rupees|lakhs?|crores?|k)?\b
    )
    """,
)


def strip_budget_claims(text: str) -> str:
    """Remove any budget figure the request text states.

    Not cosmetic, and not only about a task line that read "budget around
    2,00,000" above a 5,000 mandate. The task string is what the DECOMPOSING
    model reads, and a figure in it is an instruction: split this against two
    lakh. The token says five thousand. Leaving both in place hands a downstream
    model two different budgets and lets the wrong one shape the plan.

    The ceiling is carried by the token and by nothing else, so the sentence
    should not carry a second opinion about it.
    """
    cleaned = _BUDGET_CLAIM.sub(" ", text or "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
    cleaned = re.sub(r"[,;]\s*\.", ".", cleaned)
    cleaned = cleaned.strip().strip(",;").strip()
    # A strip that eats the request is a worse outcome than a stale figure: the
    # decomposer would be handed almost nothing to work from. An absolute floor
    # rather than a proportional one - "equip the studio" is a perfectly good
    # task even when it is a third of what was typed, and a ratio test threw
    # exactly those away.
    if len(cleaned) < 8 or len(cleaned.split()) < 2:
        return (text or "").strip()
    return cleaned


def _task_line(understood: dict[str, Any], sourcing: str, supplier: str | None) -> str:
    """The sentence the decomposer actually reads.

    Sourcing is folded into the task text rather than passed as a field because
    that is what reaches the decomposing model - a run-level flag it never sees
    would be a setting that changes nothing.
    """
    line = strip_budget_claims(understood.get("goal") or "").rstrip(".")
    if sourcing == "specific" and supplier:
        line = f"{line}. Buy only from {supplier}; do not shop around."
    elif sourcing == "best":
        line = f"{line}. Find the best source; searching is allowed."
    elif sourcing == "catalogue":
        line = f"{line}. Use our own catalogue; no web search."
    deadline = understood.get("deadline")
    if deadline:
        line = f"{line} Deadline: {deadline}."
    return line.strip()[:400]


def read(
    text: str,
    answers: dict[str, Any] | None = None,
    *,
    extract: Callable[[str], Any] | None = None,
    defaults: dict[str, Any] | None = None,
) -> Reading:
    """Read a request, and say what is still missing.

    Called repeatedly with a growing `answers` map: the console shows the
    questions, collects replies, and asks again. When nothing is left, the
    reading carries a proposal that can be posted straight to /runs.
    """
    answers = dict(answers or {})
    defaults = dict(defaults or {})
    text = (text or "").strip()
    if not text:
        return Reading(understood=_blank_understanding(""), questions=[], proposal=None,
                       notes=["Nothing to read yet."])

    understood, notes = _understand(text, extract)

    questions: list[Question] = []

    ceiling = _rupees(answers.get("ceiling_rupees"))
    if ceiling is None:
        questions.append(_ceiling_question(understood))

    sourcing = answers.get("sourcing") or understood.get("sourcing")
    if sourcing not in ("catalogue", "specific", "best"):
        questions.append(_sourcing_question())
        sourcing = None

    supplier = (answers.get("supplier") or understood.get("supplier") or "").strip() or None
    if sourcing == "specific" and not supplier:
        questions.append(_supplier_question())

    # Ask everything that is missing in one pass rather than one at a time. A
    # question per turn is a wizard; three at once is a conversation.
    if questions:
        return Reading(understood=understood, questions=questions, proposal=None, notes=notes)

    proposal = {
        "task": _task_line(understood, sourcing, supplier),
        "budget_paise": ceiling * RUPEE,
        "fan_out": int(defaults.get("fan_out", 3)),
        "max_depth": int(defaults.get("max_depth", 8)),
        "floor_paise": floor_rupees(ceiling) * RUPEE,
        "decomposer": defaults.get("decomposer", "auto"),
        # As data, not only as prose. The sentence tells the decomposing model
        # what to aim for; this is what actually governs the tree.
        "sourcing": sourcing,
        "supplier": supplier if sourcing == "specific" else None,
        "monitor": bool(defaults.get("monitor", True)),
        "critic": bool(defaults.get("critic", True)),
    }
    understood = dict(understood, sourcing=sourcing, supplier=supplier,
                      ceiling_rupees=ceiling)
    return Reading(understood=understood, questions=[], proposal=proposal, notes=notes)
