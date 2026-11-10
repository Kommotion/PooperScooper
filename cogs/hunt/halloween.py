"""Halloween theme identity.

The live candy hunt stays in ``cogs.candyhunt``. This theme lets the engine
accept a Halloween caller without starting a second event.
"""

from __future__ import annotations

from dataclasses import dataclass

from cogs.candyhunt import MAIN_DROP_CHANNEL_ID, MTS_GUILD_ID


@dataclass(frozen=True)
class HalloweenTheme:
    key: str = "halloween"
    drop_channel_id: int = MAIN_DROP_CHANNEL_ID
    guild_id: int = MTS_GUILD_ID
    timezone_name: str = "America/Los_Angeles"

    def handles_live_event(self) -> bool:
        return False
