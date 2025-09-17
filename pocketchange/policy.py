"""Datalog policy templates: tool, budget, depth, time.

AIP calls this set the "Simple profile" - four check shapes covering every
constraint we need, kept deliberately narrow so verification stays fast and no
token can smuggle in arbitrary logic.

Policies live apart from token.py because they are the part that changes most
while tuning behaviour. The token machinery underneath should stay still.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

RUPEE = 100  # paise

# Money is integer paise everywhere. Floats round, and rounding money is how you
# lose it: 0.1 + 0.2 is 0.30000000000000004, and a running total built on that
# eventually lands on the wrong side of a budget comparison.


@dataclass(frozen=True)
class Grant:
    """What one delegation hands to one recipient.

    Frozen because a grant is a statement about authority, not a workspace. If
    something wants different terms it makes a new Grant.
    """

    tools: tuple[str, ...]
    budget_paise: int
    expires: datetime | None = None

    def __post_init__(self) -> None:
        if not self.tools:
            raise ValueError("a grant with no tools cannot authorise anything")
        if self.budget_paise < 0:
            raise ValueError(f"negative budget: {self.budget_paise}")
        if self.expires is not None and self.expires.tzinfo is None:
            raise ValueError("expires must be timezone-aware; use timezone.utc")


@dataclass(frozen=True)
class Operation:
    """One concrete thing an agent is trying to do, right now.

    Checks describe conditions but never say what is happening. This is the
    other half: the facts an authorizer supplies so those checks have something
    real to evaluate against.
    """

    tool: str
    amount_paise: int = 0
    depth: int = 0
    at: datetime | None = None

    def __post_init__(self) -> None:
        if self.amount_paise < 0:
            raise ValueError(f"negative amount: {self.amount_paise}")
        if self.at is not None and self.at.tzinfo is None:
            raise ValueError("at must be timezone-aware; use timezone.utc")


# --- Datalog sources -------------------------------------------------------
#
# These are templates with {named} placeholders, never f-strings. Biscuit fills
# the placeholders as typed terms, so a value containing ";" or a counterfeit
# check cannot break out of its slot and become executable policy. Same reasoning
# as parameterised SQL: the boundary between data and code has to be structural.

AUTHORITY = """
    identity({identity});
    max_depth({max_depth});
    check if budget($b), $b <= {budget};
    check if depth($d), $d <= {max_depth};
    check if time($t), $t <= {expires};
"""

DELEGATION = """
    delegate({delegate});
    context({context});
    check if tool($t), {tools}.contains($t);
    check if budget($b), $b <= {budget};
"""

DELEGATION_WITH_EXPIRY = DELEGATION + """
    check if time($t), $t <= {expires};
"""

# `allow if true` reads as "if every check in every block passed, permit it".
# A Biscuit with no matching allow policy is refused, which is what makes the
# system fail-closed: denial is the resting state, permission must be
# positively established.
AUTHORIZER = """
    tool({tool});
    budget({amount});
    depth({depth});
    time({at});
    allow if true;
"""
