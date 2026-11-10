"""Theme-shaped hunt engine.

A theme that does not handle its own live event (Halloween) is a no-op here.
Thanksgiving prepares the persisted book and returns the plan. Discord sends
stay in the cog.
"""

from __future__ import annotations

from datetime import datetime

from cogs.hunt.book import HuntBook, LivePlan


class HuntEngine:
    def __init__(self, theme, book: HuntBook | None = None):
        self.theme = theme
        self.book = book

    def handles_live_event(self) -> bool:
        return bool(self.theme.handles_live_event())

    async def tick(self, now: datetime, *, avatar_exists: bool = False) -> LivePlan | None:
        if not self.handles_live_event():
            return None
        if self.book is None:
            return None
        return await self.book.prepare_live(now, avatar_exists=avatar_exists)
