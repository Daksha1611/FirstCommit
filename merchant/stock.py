"""What the company currently holds, and what it wants to hold.

A standing instruction - "keep the stationery cupboard stocked" - only means
something against a stock position. Without one there is nothing to compare and
the agent is guessing.

Reordering itself is arithmetic, not judgement: below the reorder point, buy up
to the target. That is deliberately code rather than a model. Where a model is
useful is turning a sentence into reorder points in the first place, and that
happens once rather than every time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import get


@dataclass(frozen=True)
class StockLevel:
    """One line of inventory, and the policy that governs it."""

    sku: str
    on_hand: int
    reorder_point: int      # at or below this, top up
    target_level: int       # top up to here

    @property
    def below_reorder(self) -> bool:
        return self.on_hand <= self.reorder_point

    @property
    def shortfall(self) -> int:
        """How many to buy. Zero unless we are at or under the reorder point."""
        return max(0, self.target_level - self.on_hand) if self.below_reorder else 0

    def as_dict(self) -> dict:
        return {
            "sku": self.sku,
            "on_hand": self.on_hand,
            "reorder_point": self.reorder_point,
            "target_level": self.target_level,
            "below_reorder": self.below_reorder,
            "shortfall": self.shortfall,
        }


# The consumables a standing instruction would plausibly cover. Deliberately
# small: a standing order over rack servers would be alarming.
DEFAULT_LEVELS: tuple[StockLevel, ...] = (
    StockLevel("STA-A4-5", on_hand=2, reorder_point=3, target_level=12),
    StockLevel("TNR-LSR-1", on_hand=1, reorder_point=2, target_level=6),
    StockLevel("CBL-CAT6-1", on_hand=8, reorder_point=4, target_level=20),
    StockLevel("HDS-ANC-1", on_hand=5, reorder_point=2, target_level=8),
)

BY_SKU = {level.sku: level for level in DEFAULT_LEVELS}


def levels() -> tuple[StockLevel, ...]:
    return DEFAULT_LEVELS


def get_level(sku: str) -> StockLevel:
    if sku not in BY_SKU:
        raise KeyError(f"no stock policy for {sku}")
    return BY_SKU[sku]


def needs_reorder(stock: tuple[StockLevel, ...] | None = None) -> tuple[StockLevel, ...]:
    """Lines at or below their reorder point."""
    return tuple(level for level in (stock or DEFAULT_LEVELS) if level.below_reorder)


def reorder_cost(stock: tuple[StockLevel, ...] | None = None) -> int:
    """What topping everything up would cost, in paise."""
    return sum(
        get(level.sku).price_paise * level.shortfall
        for level in needs_reorder(stock)
    )


def as_report(stock: tuple[StockLevel, ...] | None = None) -> list[dict]:
    return [level.as_dict() for level in (stock or DEFAULT_LEVELS)]
