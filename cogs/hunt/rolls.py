"""Item rolls. Chances come from the theme's items, not from branches in the caller."""

from __future__ import annotations

import random

from cogs.hunt.theme import HuntItem


def choose_item(
    items: tuple[HuntItem, ...] | list[HuntItem],
    rng: random.Random,
    *,
    force_key: str | None = None,
) -> HuntItem:
    """Weighted roll. ``force_key`` replaces the roll when that item exists."""
    if not items:
        raise ValueError("hunt has no items to roll")
    if force_key:
        for item in items:
            if item.key == force_key:
                return item
    total = sum(item.weight for item in items if item.weight > 0)
    if total <= 0:
        return items[0]
    ticket = rng.random() * total
    upto = 0.0
    for item in items:
        if item.weight <= 0:
            continue
        upto += item.weight
        if ticket < upto:
            return item
    return items[-1]


def slipped(grease_chance: float, rng: random.Random) -> bool:
    """True when this grab slips. A chance of 0 never slips."""
    if grease_chance <= 0:
        return False
    return rng.random() < grease_chance
