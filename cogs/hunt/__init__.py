"""Themeable hunt engine. Halloween's live loop stays in ``cogs.candyhunt``."""

from cogs.hunt.engine import HuntEngine
from cogs.hunt.halloween import HalloweenTheme
from cogs.hunt.thanksgiving import ThanksgivingTheme, TurkeySettings, load_turkey_settings

__all__ = [
    "HalloweenTheme",
    "HuntEngine",
    "ThanksgivingTheme",
    "TurkeySettings",
    "load_theme",
    "load_turkey_settings",
]


def load_theme(name: str, settings: TurkeySettings | None = None):
    """Build a theme by name. Thanksgiving uses defaults when settings are omitted."""
    key = (name or "").strip().casefold()
    if key == "halloween":
        return HalloweenTheme()
    if key == "thanksgiving":
        return ThanksgivingTheme(settings if settings is not None else TurkeySettings())
    raise KeyError(name)
