"""PNG weekly stats card. Names are passed in. This module does not store them.

The picture is one of the dashboards in ``cogs.utils.stats_designs``.
Switch the default with the constant below.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from cogs.utils.constants import POOPER_APP_ID
from cogs.utils.stats_store import WeeklyStats

# Nick: set this to "A", "B", "C", or "D".
DEFAULT_CARD_DESIGN = "A"

CARD_WIDTH = 1440
MARGIN = 32
PANEL_PAD = 16
SECTION_GAP = 14
LINE_H = 22
TITLE_H = 26
HEADER_H = 128
HEAT_CELL_H = 16
HEAT_LABEL_W = 40
BODY_FONT_SIZE = 15
EMOJI_FONT_SIZE = 15

COLOR_BG = (22, 24, 33)
COLOR_PANEL = (33, 36, 49)
COLOR_INK = (244, 239, 230)
COLOR_MUTED = (168, 164, 156)
COLOR_ACCENT = (232, 146, 74)
COLOR_GOOD = (126, 184, 138)
COLOR_BAD = (214, 112, 102)
COLOR_LINE = (58, 62, 78)
COLOR_HEAT_ZERO = (40, 44, 58)
COLOR_HEAT_HOT = (232, 146, 74)

_FONT_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "assets" / "fonts" / "DejaVuSans.ttf",
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf",
    Path(r"C:\Windows\Fonts\arial.ttf"),
)
# The text face has no emoji glyphs, so those characters draw as empty boxes.
# Segoe UI Emoji supplies the color glyphs. Reactions use Unicode names when it is missing.
_EMOJI_FONT_CANDIDATES = (
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "seguiemj.ttf",
    Path(r"C:\Windows\Fonts\seguiemj.ttf"),
)
_VOICE_SPEAKER = "🔊"
_EMOJI_JOINERS = frozenset("\u200d\ufe0f\ufe0e\u20e3")
_FONT_CACHE: dict[int, ImageFont.ImageFont] = {}
_EMOJI_FONT_CACHE: dict[tuple[int, str], ImageFont.ImageFont] = {}
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass
class CardNames:
    channels: dict[int, str]
    users: dict[int, str]
    emojis: dict[str, str]


def _load_font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            if path.is_file():
                return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _font(size: int) -> ImageFont.ImageFont:
    cached = _FONT_CACHE.get(size)
    if cached is None:
        cached = _load_font(size)
        _FONT_CACHE[size] = cached
    return cached


def _emoji_font(size: int) -> ImageFont.ImageFont | None:
    """Return an emoji font, or None when every candidate is missing or unreadable."""
    for candidate in _EMOJI_FONT_CANDIDATES:
        try:
            path = Path(candidate)
            if not path.is_file():
                continue
            key = (int(size), str(path))
            cached = _EMOJI_FONT_CACHE.get(key)
            if cached is not None:
                return cached
            font = ImageFont.truetype(str(path), int(size))
        except (OSError, TypeError, ValueError):
            continue
        _EMOJI_FONT_CACHE[key] = font
        return font
    return None


def _coerce_names(names: CardNames | dict | None) -> CardNames:
    if isinstance(names, CardNames):
        return names
    if not isinstance(names, dict):
        return CardNames({}, {}, {})
    channels = {int(key): str(value) for key, value in (names.get("channels") or {}).items()}
    users = {int(key): str(value) for key, value in (names.get("users") or {}).items()}
    emojis = {str(key): str(value) for key, value in (names.get("emojis") or {}).items()}
    return CardNames(channels, users, emojis)


def _fit(text: str, font: ImageFont.ImageFont, width: int) -> str:
    cleaned = " ".join(str(text).split()) or "-"
    draw = _fit._draw
    if draw.textlength(cleaned, font=font) <= width:
        return cleaned
    ellipsis = "..."
    while cleaned and draw.textlength(cleaned + ellipsis, font=font) > width:
        cleaned = cleaned[:-1]
    return (cleaned + ellipsis) if cleaned else ellipsis


_fit._draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))  # type: ignore[attr-defined]


def _month_day(value) -> str:
    return f"{value.strftime('%b')} {value.day}"


def _week_label(stats: WeeklyStats) -> str:
    return f"{_month_day(stats.week_start)} - {_month_day(stats.week_end)}, {stats.week_end.year}"


def _channel_name(names: CardNames, channel_id: int) -> str:
    return names.channels.get(channel_id) or "unknown channel"


def _emoji_name(names: CardNames, key: str, stored: str) -> str:
    return names.emojis.get(key) or stored or key


def _fmt(value: int) -> str:
    return f"{int(value):,}"


def _body_width() -> int:
    return CARD_WIDTH - 2 * MARGIN - 2 * PANEL_PAD - 8


def _count_line(label: str, weekly: int, all_time: int | None = None) -> str:
    if all_time is None:
        return f"{label}  {_fmt(weekly)}"
    return f"{label}  {_fmt(weekly)} this week  /  {_fmt(all_time)} all time"


def _command_label(name, kind: str | None = None, app_id: int = 0, bot_name: str = "") -> str:
    """Slash and prefix label. A row may be passed as the first argument."""
    if kind is None or not isinstance(name, str):
        row = name
        kind = str(getattr(row, "kind", "") or "")
        name = str(getattr(row, "name", "") or "")
        app_id = getattr(row, "app_id", app_id)
        bot_name = str(getattr(row, "bot_name", bot_name) or "")
    else:
        kind = str(kind or "")
        name = str(name or "")
    if kind == "both":
        return name
    if kind != "slash":
        return f"!{name}"
    try:
        stored_app = int(app_id or 0)
    except (TypeError, ValueError):
        stored_app = 0
    label = f"/{name}"
    if stored_app in (0, POOPER_APP_ID):
        return label
    bot = str(bot_name or "").strip() or "Unknown bot"
    return f"{label} · {bot}"


def _pct(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{rate * 100:.0f}%"


def _seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}s"


def _join_dims(rows) -> str:
    if not rows:
        return "none"
    return ", ".join(f"{row.key} ({_fmt(row.weekly)})" for row in rows)


def _member_line(stats: WeeklyStats) -> str:
    current = _fmt(stats.active_members)
    last = stats.active_members_last_week
    if last is None:
        return f"{current} active members this week. No prior week to compare."
    delta = stats.active_members - last
    shown = f"+{_fmt(delta)}" if delta > 0 else _fmt(delta)
    return f"{current} active members this week. {shown} versus last week's {_fmt(last)}."


def _summary_line(stats: WeeklyStats) -> str:
    return (
        f"{_fmt(stats.messages_weekly)} messages   {_fmt(stats.reactions_weekly)} reactions   "
        f"{_fmt(stats.commands_weekly)} commands   {_fmt(stats.voice_minutes)} voice min"
    )


def _is_emoji_char(char: str) -> bool:
    code = ord(char)
    if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF:
        return True
    if 0x2300 <= code <= 0x23FF or 0x2B00 <= code <= 0x2BFF:
        return True
    return unicodedata.category(char) == "So" and code > 255


def _leading_emoji_cluster(text: str) -> str:
    """Return the emoji sequence at the start of text, or an empty string."""
    if not text:
        return ""
    if text[0] in "0123456789#*" and "\u20e3" in text[:3]:
        return text[: text.index("\u20e3") + 1]
    if not _is_emoji_char(text[0]):
        return ""
    end = 0
    for index, char in enumerate(text):
        if _is_emoji_char(char) or char in _EMOJI_JOINERS:
            end = index + 1
            continue
        break
    return text[:end]


def _name_emoji_cluster(cluster: str) -> str:
    names: list[str] = []
    for char in cluster:
        category = unicodedata.category(char)
        if char in _EMOJI_JOINERS or category in {"Mn", "Me", "Cf", "Sk"}:
            continue
        try:
            names.append(unicodedata.name(char).lower())
        except ValueError:
            continue
    return " ".join(names) if names else "emoji"


def _emoji_fallback_label(text: str) -> str:
    """Replace emoji characters with Unicode names. Plain text is left as-is."""
    parts: list[str] = []
    plain: list[str] = []

    def flush() -> None:
        if plain:
            parts.append("".join(plain))
            plain.clear()

    index = 0
    raw = str(text or "")
    while index < len(raw):
        cluster = _leading_emoji_cluster(raw[index:])
        if cluster:
            flush()
            parts.append(_name_emoji_cluster(cluster))
            index += len(cluster)
            continue
        plain.append(raw[index])
        index += 1
    flush()
    label = " ".join(part.strip() for part in parts if part.strip())
    return label or "emoji"


def _reaction_label(names: CardNames, row, emoji_ready: bool) -> str:
    label = _emoji_name(names, row.emoji_key, row.name)
    if row.is_custom or emoji_ready:
        return label
    return _emoji_fallback_label(label)


def _voice_channel_label(names: CardNames, channel_id: int, emoji_ready: bool) -> str:
    name = _channel_name(names, channel_id)
    if emoji_ready:
        return f"{_VOICE_SPEAKER} {name}"
    return name


def _sections(stats: WeeklyStats, names: CardNames) -> list[tuple[str, list[str]]]:
    emoji_ready = _emoji_font(EMOJI_FONT_SIZE) is not None
    channels: list[str] = []
    if stats.top_channels:
        channels.append("Busiest")
        channels.extend(
            _count_line("#" + _channel_name(names, row.channel_id), row.weekly, row.all_time)
            for row in stats.top_channels
        )
    if stats.bottom_channels:
        channels.append("Quietest")
        channels.extend(
            _count_line("#" + _channel_name(names, row.channel_id), row.weekly, row.all_time)
            for row in stats.bottom_channels
        )
    if stats.dead_channels:
        channels.append("Dead (no messages this week)")
        channels.extend(f"#{_channel_name(names, row.channel_id)}" for row in stats.dead_channels[:8])
        extra = len(stats.dead_channels) - 8
        if extra > 0:
            channels.append(f"+{_fmt(extra)} more")
    if not channels:
        channels = ["No channel activity yet."]

    reactions = [
        _count_line(_reaction_label(names, row, emoji_ready), row.weekly, row.all_time)
        for row in stats.top_reactions
    ] or ["No reactions this week."]

    emojis = [
        _count_line(_emoji_name(names, row.emoji_key, row.name), row.all_time)
        + " all time"
        for row in stats.least_custom_emojis
    ] or ["No custom emoji counts yet."]

    commands: list[str] = []
    if stats.top_commands:
        commands.append("Most used")
        commands.extend(
            _count_line(_command_label(row), row.weekly, row.all_time)
            for row in stats.top_commands
        )
    if stats.bottom_commands:
        commands.append("Least used")
        commands.extend(
            _count_line(_command_label(row), row.weekly, row.all_time)
            for row in stats.bottom_commands
        )
    if not commands:
        commands = ["No commands this week."]

    image = stats.imagegen
    if image.generation_count == 0 and image.success_count == 0 and image.error_count == 0:
        image_lines = ["No image generations this week."]
    else:
        image_lines = [
            f"Average generation {_seconds(image.avg_seconds)}",
            f"Success {_pct(image.success_rate)}    Errors {_pct(image.error_rate)}    Runs {_fmt(image.generation_count)}",
            (
                f"Kept the first result {_fmt(image.kept_first)}    "
                f"Regenerated {_fmt(image.regenerate_count)}    Edited {_fmt(image.edit_count)}"
            ),
            "Models: " + _join_dims(image.models),
            "LoRAs: " + _join_dims(image.loras),
            "Edit modes: " + _join_dims(image.edit_modes),
            "Resolutions: " + _join_dims(image.resolutions),
            "Steps: " + _join_dims(image.steps),
            "Samplers: " + _join_dims(image.samplers),
        ]

    voice = [f"{_fmt(stats.voice_minutes)} minutes this week  /  {_fmt(stats.voice_minutes_all_time)} all time"]
    if stats.top_voice_channels:
        voice.extend(
            _count_line(_voice_channel_label(names, row.channel_id, emoji_ready), row.weekly, row.all_time)
            for row in stats.top_voice_channels
        )
    else:
        voice.append("No voice time this week.")

    return [
        ("Channels", channels),
        ("Reactions", reactions),
        ("Least-used custom emojis", emojis),
        ("Commands", commands),
        ("Image generation", image_lines),
        ("Active members", [_member_line(stats)]),
        ("Voice", voice),
    ]


def _section_height(line_count: int) -> int:
    return PANEL_PAD + TITLE_H + 6 + max(1, line_count) * LINE_H + PANEL_PAD


def _heat_height() -> int:
    return PANEL_PAD + TITLE_H + 8 + 14 + 7 * (HEAT_CELL_H + 3) + PANEL_PAD


def _heat_color(value: int, peak: int) -> tuple[int, int, int]:
    if peak <= 0 or value <= 0:
        return COLOR_HEAT_ZERO
    blend = value / peak
    return tuple(int(COLOR_HEAT_ZERO[index] + (COLOR_HEAT_HOT[index] - COLOR_HEAT_ZERO[index]) * blend) for index in range(3))


def _draw_header(draw: ImageDraw.ImageDraw, stats: WeeklyStats, guild_name: str, fonts) -> int:
    box = (MARGIN, MARGIN, CARD_WIDTH - MARGIN, MARGIN + HEADER_H)
    draw.rounded_rectangle(box, radius=16, fill=COLOR_PANEL)
    draw.rectangle((MARGIN, MARGIN, MARGIN + 6, MARGIN + HEADER_H), fill=COLOR_ACCENT)
    text_w = CARD_WIDTH - 2 * MARGIN - 36
    x = MARGIN + 22
    y = MARGIN + 16
    draw.text((x, y), "WEEKLY STATS", font=fonts["small"], fill=COLOR_ACCENT)
    y += 22
    draw.text((x, y), _fit(guild_name or "Server", fonts["title"], text_w), font=fonts["title"], fill=COLOR_INK)
    y += 40
    draw.text((x, y), _fit(_week_label(stats), fonts["body"], text_w), font=fonts["body"], fill=COLOR_MUTED)
    y += 24
    draw.text((x, y), _fit(_summary_line(stats), fonts["small"], text_w), font=fonts["small"], fill=COLOR_INK)
    return MARGIN + HEADER_H


def _emoji_draw_y(y: int, cluster: str, body_font, emoji_font) -> float:
    """Drop the emoji so its middle lines up with the text, instead of sitting above it."""
    try:
        text_box = body_font.getbbox("Ag")
        emoji_box = emoji_font.getbbox(cluster)
        text_mid = (text_box[1] + text_box[3]) / 2
        emoji_mid = (emoji_box[1] + emoji_box[3]) / 2
        return y + (text_mid - emoji_mid)
    except (OSError, ValueError, TypeError):
        return float(y)


def _draw_fitted_line(draw, x: int, y: int, line: str, body_font, emoji_font, fill, width: int) -> None:
    if emoji_font is not None:
        raw = " ".join(str(line).split()) or "-"
        cluster = _leading_emoji_cluster(raw)
        if cluster:
            rest = raw[len(cluster):]
            try:
                emoji_width = float(draw.textlength(cluster, font=emoji_font))
                room = width - emoji_width
                fitted = rest
                if fitted and (room < 1 or draw.textlength(fitted, font=body_font) > room):
                    fitted = _fit(fitted, body_font, max(1, int(room))) if room >= 1 else ""
                draw.text(
                    (x, _emoji_draw_y(y, cluster, body_font, emoji_font)),
                    cluster,
                    font=emoji_font,
                    fill=fill,
                    embedded_color=True,
                )
                if fitted:
                    draw.text((x + emoji_width, y), fitted, font=body_font, fill=fill)
                return
            except Exception:
                line = _emoji_fallback_label(cluster) + rest
    draw.text((x, y), _fit(str(line), body_font, width), font=body_font, fill=fill)


def _draw_section(draw: ImageDraw.ImageDraw, y: int, title: str, lines: list[str], fonts) -> int:
    height = _section_height(len(lines))
    bottom = y + height
    draw.rounded_rectangle((MARGIN, y, CARD_WIDTH - MARGIN, bottom), radius=14, fill=COLOR_PANEL)
    draw.rectangle((MARGIN, y, MARGIN + 6, bottom), fill=COLOR_ACCENT)
    text_w = _body_width()
    x = MARGIN + PANEL_PAD + 6
    cursor = y + PANEL_PAD
    draw.text((x, cursor), _fit(title, fonts["section"], text_w), font=fonts["section"], fill=COLOR_INK)
    cursor += TITLE_H + 6
    for line in lines:
        fill = COLOR_MUTED if line.endswith(":") or line in {"Busiest", "Quietest", "Most used", "Least used", "Dead (no messages this week)"} else COLOR_INK
        if line in {"Busiest", "Quietest", "Most used", "Least used"} or line.startswith("Dead"):
            fill = COLOR_ACCENT
        _draw_fitted_line(draw, x, cursor, line, fonts["body"], fonts["emoji"], fill, text_w)
        cursor += LINE_H
    return bottom


def _draw_heatmap(draw: ImageDraw.ImageDraw, y: int, title: str, grid, fonts) -> int:
    height = _heat_height()
    bottom = y + height
    draw.rounded_rectangle((MARGIN, y, CARD_WIDTH - MARGIN, bottom), radius=14, fill=COLOR_PANEL)
    draw.rectangle((MARGIN, y, MARGIN + 6, bottom), fill=COLOR_ACCENT)
    x = MARGIN + PANEL_PAD + 6
    cursor = y + PANEL_PAD
    draw.text((x, cursor), title, font=fonts["section"], fill=COLOR_INK)
    cursor += TITLE_H + 4
    inner_right = CARD_WIDTH - MARGIN - PANEL_PAD
    grid_left = x + HEAT_LABEL_W
    grid_width = max(24, inner_right - grid_left)
    cell_w = grid_width / 24
    peak = max((value for row in grid for value in row), default=0)
    for hour in (0, 6, 12, 18):
        label_x = grid_left + hour * cell_w
        draw.text((label_x, cursor), str(hour), font=fonts["tiny"], fill=COLOR_MUTED)
    cursor += 14
    for day, row in enumerate(grid):
        draw.text((x, cursor), _DAY_NAMES[day], font=fonts["tiny"], fill=COLOR_MUTED)
        for hour, value in enumerate(row):
            left = grid_left + hour * cell_w
            draw.rectangle(
                (left, cursor, left + cell_w - 2, cursor + HEAT_CELL_H),
                fill=_heat_color(int(value), peak),
            )
        cursor += HEAT_CELL_H + 3
    return bottom


def render_stats_card(
    stats: WeeklyStats,
    names: CardNames | dict | None,
    guild_name: str,
    *,
    watermark: str | None = None,
    scope: str = "weekly",
) -> bytes:
    """Render the weekly card, or the all-time card when ``scope`` is ``all_time``.

    The default stays the weekly Design A card. Callers that monkeypatch this
    function still receive ``(stats, names, guild_name)``.
    """
    if scope == "all_time":
        return render_alltime_card(stats, names, guild_name, watermark=watermark)
    from cogs.utils.stats_designs import render_design

    return render_design(DEFAULT_CARD_DESIGN, stats, names, guild_name, watermark=watermark)


def render_alltime_card(
    stats: WeeklyStats,
    names: CardNames | dict | None,
    guild_name: str,
    *,
    watermark: str | None = None,
) -> bytes:
    """All-time Design A card. Empty stats still produce a valid PNG."""
    from cogs.utils.stats_designs.design_a import render_alltime

    return render_alltime(stats, names, guild_name, watermark=watermark)
