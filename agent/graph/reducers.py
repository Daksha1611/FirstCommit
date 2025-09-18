"""Merging concurrent writes without clobbering.

Direct translation of RabbitHole's merge_perspectives. Three shoppers run at the
same time and each produces one cart; a plain assignment would leave only the
last writer's, and the fan-out would be decorative.

Keyed merge is what makes parallel agents actually parallel.
"""

from __future__ import annotations

from typing import TypeVar

K = TypeVar("K")
V = TypeVar("V")


def merge_by_key(left: dict[K, V], right: dict[K, V]) -> dict[K, V]:
    """Right wins per key; keys only in left survive untouched."""
    if not left:
        return dict(right or {})
    if not right:
        return dict(left)
    return {**left, **right}


def merge_proposals(existing: dict, incoming) -> dict:
    """Add one proposal to the round's set, keyed by strategy."""
    return merge_by_key(existing, {incoming.strategy: incoming})
