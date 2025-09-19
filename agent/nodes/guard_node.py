"""The buyer's own check on its own output, before the gateway ever sees it.

Deterministic. No model, no prompt, no judgement - every check is arithmetic or a
lookup, so it cannot be argued out of a refusal.

It runs between the chooser and the broker, and it is called from the tool surface
rather than exposed as a tool the agent may choose to invoke. A gate an agent can
decline to walk through is not a gate.

Six checks, each answering an attack the plan named:

  traceable   every line matches an offer a shopper actually read     (T2T)
  priced      the total equals the sum of those real offers           (T2T)
  known       no seller that does not exist in the market             (S2I)
  capped      a sub-mandate never exceeds that seller's subtotal      (M2A)
  provenance  no unmarked seller text in an instruction position      (T2R)
  divergence  how much the parallel shoppers actually disagreed       (D3/D4)

Divergence is not a refusal. Three shoppers on one model family reading one
catalogue are correlated by construction, so unanimity is a signal rather than a
comfort - it is recorded for the observer to act on, not blocked here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent.utils import provenance
from merchant import offers, sellers

if TYPE_CHECKING:  # pragma: no cover
    from agent.tools import ToolSurface


@dataclass
class GuardVerdict:
    ok: bool = True
    failures: list[str] = field(default_factory=list)
    divergence: int = 0
    checked_lines: int = 0

    def fail(self, reason: str) -> None:
        self.ok = False
        self.failures.append(reason)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "failures": self.failures,
            "divergence": self.divergence,
            "checked_lines": self.checked_lines,
        }


def divergence_of(proposals: dict) -> int:
    """How many genuinely different carts the shoppers produced.

    0 means every shopper returned the same cart. That is the monoculture
    symptom: same model family, same catalogue, same answer - and it is exactly
    the correlated-failure mode D4 describes, appearing inside our own system
    rather than being done to us.
    """
    if not proposals:
        return 0
    shapes = {
        tuple(sorted((line.sku, line.quantity, line.seller_id) for line in p.lines))
        for p in proposals.values()
    }
    return len(shapes) - 1


def inspect_cart(tools: "ToolSurface") -> GuardVerdict:
    """Check the adopted cart before any authority is minted for it."""
    verdict = GuardVerdict(divergence=divergence_of(tools.proposals))

    if not tools.cart_lines:
        verdict.fail("no cart was adopted")
        return verdict

    verdict.checked_lines = len(tools.cart_lines)
    running_total = 0

    for line in tools.cart_lines:
        # known: a seller that does not exist was invented somewhere (S2I)
        if not sellers.known(line.seller_id):
            verdict.fail(f"unknown seller: {line.seller_id}")
            continue

        # traceable: did a shopper actually see this offer? (T2T)
        seen = tools.offers_seen.get(line.sku, [])
        if seen and not any(seller == line.seller_id for seller, _price in seen):
            verdict.fail(
                f"{line.seller_id} was never returned by a search for {line.sku}"
            )

        # priced: the price must match the real offer, not one that was supplied
        try:
            real_unit = offers.price_line(line.sku, line.seller_id, 1)
        except KeyError:
            verdict.fail(f"{line.seller_id} does not offer {line.sku}")
            continue
        if real_unit != line.unit_price_paise:
            verdict.fail(
                f"price for {line.sku} at {line.seller_id} is {real_unit}p, "
                f"cart says {line.unit_price_paise}p"
            )
        running_total += real_unit * line.quantity

    stated = sum(line.line_total_paise for line in tools.cart_lines)
    if running_total != stated:
        verdict.fail(f"cart total {stated}p does not match offers at {running_total}p")

    # provenance: seller text must never reach an instruction position unmarked
    leaked = provenance.contains_unmarked_untrusted(
        [line.as_dict() for line in tools.cart_lines], tools.untrusted_seen
    )
    for item in leaked:
        verdict.fail(f"unmarked seller text in the cart: {item!r}")

    return verdict


def check_sub_mandate(tools: "ToolSurface", seller_id: str, requested_paise: int) -> GuardVerdict:
    """A sub-mandate may be capped at that seller's subtotal, and no more (M2A).

    Refusing an inflated cap here is belt and braces - the gateway's cumulative
    ledger would stop the spend anyway - but it stops the *authority* being
    created, which is what keeps the blast radius honest.
    """
    verdict = GuardVerdict()
    subtotal = sum(
        line.line_total_paise for line in tools.cart_lines if line.seller_id == seller_id
    )
    if subtotal <= 0:
        verdict.fail(f"nothing in the cart is from {seller_id}")
        return verdict
    if requested_paise > subtotal:
        verdict.fail(
            f"sub-mandate for {seller_id} requested {requested_paise}p, "
            f"its share of the cart is {subtotal}p"
        )
    return verdict
