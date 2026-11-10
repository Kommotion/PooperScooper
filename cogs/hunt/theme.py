"""Theme identity for the hunt engine.

Halloween's live event stays in ``cogs.candyhunt``. Thanksgiving uses the
engine's scheduler, book, and turkey cog. Both themes expose the same identity
fields so callers can take a theme without caring which hunt it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HuntItem:
    """One catchable thing. Odds, points, and grease live on the item."""

    key: str
    name: str
    points: int
    weight: float
    grease_chance: float
    emoji: str


class HuntTheme(Protocol):
    """What the engine needs from a theme before any Discord call."""

    key: str
    drop_channel_id: int
    guild_id: int
    timezone_name: str

    def handles_live_event(self) -> bool:
        """False when another cog already runs the live event for this theme."""


def contains_role_ping(text: str) -> bool:
    """True when copy would mention everyone, here, or a role."""
    folded = text.casefold()
    return "@everyone" in folded or "@here" in folded or "<@&" in folded
