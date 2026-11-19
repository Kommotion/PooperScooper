"""Alternate weekly stats card designs. Nick picks one with DEFAULT_CARD_DESIGN."""

from __future__ import annotations

from cogs.utils.stats_designs.design_a import SIZE as SIZE_A
from cogs.utils.stats_designs.design_a import render as render_a
from cogs.utils.stats_designs.design_b import SIZE as SIZE_B
from cogs.utils.stats_designs.design_b import render as render_b
from cogs.utils.stats_designs.design_c import SIZE as SIZE_C
from cogs.utils.stats_designs.design_c import render as render_c
from cogs.utils.stats_designs.design_d import SIZE as SIZE_D
from cogs.utils.stats_designs.design_d import render as render_d

DESIGNS = {
    "A": render_a,
    "B": render_b,
    "C": render_c,
    "D": render_d,
}

DESIGN_SIZES = {
    "A": SIZE_A,
    "B": SIZE_B,
    "C": SIZE_C,
    "D": SIZE_D,
}


def render_design(key: str, stats, names, guild_name: str, *, watermark: str | None = None) -> bytes:
    """Render one named design. ``watermark`` is stamped only when it is set."""
    try:
        render = DESIGNS[str(key).upper()]
    except KeyError as exc:
        known = ", ".join(DESIGNS)
        raise KeyError(f"unknown stats design {key!r}; expected {known}") from exc
    return render(stats, names, guild_name, watermark=watermark)
