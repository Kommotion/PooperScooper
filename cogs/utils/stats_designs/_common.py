"""Shared fonts, chart images, and data prep for the weekly stats dashboards.

Rendering is pure: no network and no disk writes. Matplotlib figures are built
without pyplot's global state and closed before the function returns.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[3]
FONT_DIR = ROOT / "assets" / "fonts"
_FONT_FILES = {
    "regular": FONT_DIR / "Inter-Regular.ttf",
    "medium": FONT_DIR / "Inter-Medium.ttf",
    "semibold": FONT_DIR / "Inter-SemiBold.ttf",
    "bold": FONT_DIR / "Inter-Bold.ttf",
}
_FONT_FALLBACKS = (
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf",
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
    Path(r"C:\Windows\Fonts\arial.ttf"),
)
_EMOJI_FONTS = (
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "seguiemj.ttf",
    Path(r"C:\Windows\Fonts\seguiemj.ttf"),
)
_EMOJI_JOINERS = frozenset("\u200d\ufe0f\ufe0e\u20e3")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HOUR_TICKS = ((0, "12a"), (3, "3a"), (6, "6a"), (9, "9a"), (12, "12p"), (15, "3p"), (18, "6p"), (21, "9p"))
FOOTER = "Aggregate counts only. Message text and prompts are not stored."
EMPTY = "No data yet"
_MODEL_LABELS = {"krea2_identity_edit": "krea2 edit"}

_FONTS: dict[tuple[str, int], ImageFont.ImageFont] = {}
_EMOJI_FONT: dict[int, ImageFont.ImageFont | None] = {}
_GLYPHS: dict[tuple[str, int], Image.Image | None] = {}
_MEASURE = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def font(weight: str, size: int) -> ImageFont.ImageFont:
    key = (weight, int(size))
    cached = _FONTS.get(key)
    if cached is not None:
        return cached
    path = _FONT_FILES.get(weight)
    candidates = [path] if path is not None else []
    candidates.extend(_FONT_FALLBACKS)
    loaded = None
    for candidate in candidates:
        try:
            if candidate is not None and candidate.is_file():
                loaded = ImageFont.truetype(str(candidate), int(size))
                break
        except (OSError, ValueError):
            continue
    if loaded is None:
        try:
            loaded = ImageFont.load_default(size=int(size))
        except TypeError:
            loaded = ImageFont.load_default()
    _FONTS[key] = loaded
    return loaded


def emoji_font(size: int) -> ImageFont.ImageFont | None:
    key = int(size)
    if key in _EMOJI_FONT:
        return _EMOJI_FONT[key]
    loaded = None
    for path in _EMOJI_FONTS:
        try:
            if path.is_file():
                loaded = ImageFont.truetype(str(path), key)
                break
        except (OSError, ValueError):
            continue
    _EMOJI_FONT[key] = loaded
    return loaded


def hx(color: tuple) -> str:
    return "#{:02x}{:02x}{:02x}".format(int(color[0]), int(color[1]), int(color[2]))


def fmt(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def fit(text: str, face: ImageFont.ImageFont, width: int) -> str:
    cleaned = " ".join(str(text).split()) or "—"
    if width <= 0:
        return "…"
    if _MEASURE.textlength(cleaned, font=face) <= width:
        return cleaned
    ellipsis = "…"
    lo, hi = 0, len(cleaned)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _MEASURE.textlength(cleaned[:mid] + ellipsis, font=face) <= width:
            lo = mid
        else:
            hi = mid - 1
    if lo <= 0:
        return ellipsis
    return cleaned[:lo] + ellipsis


def _is_emoji_char(char: str) -> bool:
    code = ord(char)
    if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF:
        return True
    if 0x2300 <= code <= 0x23FF or 0x2B00 <= code <= 0x2BFF:
        return True
    return unicodedata.category(char) == "So" and code > 255


def _leading_emoji_cluster(text: str) -> str:
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


def emoji_fallback_label(text: str) -> str:
    """Unicode names for emoji. Plain text is unchanged. Never returns a glyph."""
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
            names: list[str] = []
            for char in cluster:
                category = unicodedata.category(char)
                if char in _EMOJI_JOINERS or category in {"Mn", "Me", "Cf", "Sk"}:
                    continue
                try:
                    names.append(unicodedata.name(char).lower())
                except ValueError:
                    continue
            parts.append(" ".join(names) if names else "emoji")
            index += len(cluster)
            continue
        plain.append(raw[index])
        index += 1
    flush()
    label = " ".join(part.strip() for part in parts if part.strip())
    return label or "emoji"


def emoji_glyph(cluster: str, size: int) -> Image.Image | None:
    """Color emoji bitmap, or None so the caller can draw a Unicode name instead."""
    key = (str(cluster), int(size))
    if key in _GLYPHS:
        return _GLYPHS[key]
    face = emoji_font(max(16, int(size)))
    glyph = None
    if face is not None and cluster:
        pad = max(8, int(size) * 3)
        image = Image.new("RGBA", (pad, pad), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        try:
            draw.text((pad / 2, pad / 2), cluster, font=face, embedded_color=True, anchor="mm")
        except Exception:
            glyph = None
        else:
            bbox = image.getbbox()
            if bbox:
                crop = image.crop(bbox)
                longest = max(crop.size)
                if longest > 0:
                    scale = int(size) / longest
                    resized = (
                        max(1, int(round(crop.size[0] * scale))),
                        max(1, int(round(crop.size[1] * scale))),
                    )
                    if resized != crop.size:
                        crop = crop.resize(resized, Image.Resampling.LANCZOS)
                    glyph = crop
    _GLYPHS[key] = glyph
    return glyph


class Canvas:
    def __init__(self, size: tuple[int, int], bg: tuple[int, int, int]) -> None:
        self.size = (int(size[0]), int(size[1]))
        self.bg = bg
        self.image = Image.new("RGBA", self.size, bg + (255,))
        self.draw = ImageDraw.Draw(self.image)

    def text(
        self,
        xy,
        value: str,
        face: ImageFont.ImageFont,
        fill,
        max_width: int | None = None,
        anchor: str = "lt",
    ) -> str:
        shown = fit(value, face, int(max_width)) if max_width is not None else str(value)
        self.draw.text((int(xy[0]), int(xy[1])), shown, font=face, fill=fill, anchor=anchor)
        return shown

    def paste(self, image: Image.Image, xy, mask: Image.Image | None = None) -> None:
        self.image.paste(image, (int(xy[0]), int(xy[1])), mask if mask is not None else image)

    def rounded(self, box, radius: int, fill, outline=None, width: int = 1) -> None:
        self.draw.rounded_rectangle(
            [int(v) for v in box],
            radius=radius,
            fill=fill,
            outline=outline,
            width=width if outline is not None else 0,
        )

    def png(self) -> bytes:
        flat = Image.new("RGB", self.size, self.bg)
        flat.paste(self.image, mask=self.image.getchannel("A"))
        buffer = BytesIO()
        flat.save(buffer, format="PNG")
        return buffer.getvalue()


def hsplit(box, weights, gap: int = 0) -> list[tuple]:
    x0, y0, x1, y1 = box
    count = len(weights)
    usable = (x1 - x0) - gap * (count - 1)
    total = sum(weights) or 1
    cursor = x0
    boxes = []
    for index, weight in enumerate(weights):
        width = round(usable * weight / total)
        if index == count - 1:
            width = x1 - cursor
        boxes.append((cursor, y0, cursor + width, y1))
        cursor += width + gap
    return boxes


def vsplit(box, weights, gap: int = 0) -> list[tuple]:
    x0, y0, x1, y1 = box
    count = len(weights)
    usable = (y1 - y0) - gap * (count - 1)
    total = sum(weights) or 1
    cursor = y0
    boxes = []
    for index, weight in enumerate(weights):
        height = round(usable * weight / total)
        if index == count - 1:
            height = y1 - cursor
        boxes.append((x0, cursor, x1, cursor + height))
        cursor += height + gap
    return boxes


def inset(box, pad) -> tuple:
    if isinstance(pad, tuple):
        left, top, right, bottom = pad
    else:
        left = top = right = bottom = pad
    x0, y0, x1, y1 = box
    return (x0 + left, y0 + top, x1 - right, y1 - bottom)


def tone_of(current: int, previous: int | None) -> tuple[str, str]:
    if previous is None:
        return "new", "new"
    diff = int(current) - int(previous)
    if diff > 0:
        return f"▲ {fmt(diff)}", "up"
    if diff < 0:
        return f"▼ {fmt(abs(diff))}", "down"
    return "—", "flat"


def tone_color(tone: str, up, down, muted, accent):
    if tone == "up":
        return up
    if tone == "down":
        return down
    if tone == "new":
        return accent
    return muted


@dataclass
class Row:
    label: str
    weekly: int
    all_time: int
    emoji: str | None = None
    custom_id: str | None = None


@dataclass
class Kpi:
    label: str
    current: int
    previous: int | None
    hide_delta: bool = False

    @property
    def delta(self) -> str:
        if self.hide_delta:
            return ""
        return tone_of(self.current, self.previous)[0]

    @property
    def tone(self) -> str:
        return tone_of(self.current, self.previous)[1]


@dataclass
class View:
    guild: str
    week: str
    kpis: list[Kpi]
    top_channels: list[Row]
    bottom_channels: list[Row]
    dead_channels: list[Row]
    top_commands: list[Row]
    bottom_commands: list[Row]
    reactions: list[Row]
    least_emojis: list[Row]
    voice_channels: list[Row]
    voice_minutes: int
    voice_all_time: int
    heatmap: list[list[int]]
    voice_heatmap: list[list[int]]
    daily: list[int]
    voice_daily: list[int]
    voice_by_hour: list[int]
    models: list[tuple[str, int]]
    resolutions: list[tuple[str, int]]
    steps: list[tuple[str, int]]
    success_rate: float | None
    avg_seconds: float | None
    image_runs: int
    image_success: int
    image_errors: int
    kept: int
    regenerated: int
    edited: int
    history_messages: list | None = None
    history_reactions: list | None = None
    history_members: list | None = None
    earliest: str = ""
    tracking_since: str = ""
    imagegen_since: str = ""
    image_runs_all_time: int = 0


def _attr_map(names, attr: str) -> dict:
    """Read channels or emojis only. User names are never touched."""
    if attr not in {"channels", "emojis"} or names is None:
        return {}
    if isinstance(names, dict):
        raw = names.get(attr) or {}
    else:
        raw = getattr(names, attr, None) or {}
    try:
        items = list(raw.items())
    except Exception:
        return {}
    return {key: str(value) for key, value in items}


def _channel_map(names) -> dict[int, str]:
    found: dict[int, str] = {}
    for key, value in _attr_map(names, "channels").items():
        try:
            found[int(key)] = value
        except (TypeError, ValueError):
            continue
    return found


def _emoji_map(names) -> dict[str, str]:
    return {str(key): value for key, value in _attr_map(names, "emojis").items()}


def _grid(raw) -> list[list[int]]:
    rows = []
    source = list(raw or [])
    for index in range(7):
        row = list(source[index]) if index < len(source) else []
        padded = [int(value) for value in row[:24]]
        if len(padded) < 24:
            padded.extend([0] * (24 - len(padded)))
        rows.append(padded)
    return rows


def _week_label(stats) -> str:
    start = stats.week_start
    end = stats.week_end
    if start.year == end.year and start.month == end.month:
        return f"{start.strftime('%b')} {start.day} – {end.day}, {end.year}"
    if start.year == end.year:
        return f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {end.year}"
    return f"{start.strftime('%b')} {start.day}, {start.year} – {end.strftime('%b')} {end.day}, {end.year}"


def _channel_label(channels: dict[int, str], channel_id: int, hashed: bool) -> str:
    name = channels.get(int(channel_id))
    if not name:
        return "unknown channel"
    return f"#{name}" if hashed else name


def _short_name(name: str) -> str:
    """Emoji name with the wrapping colons removed."""
    cleaned = str(name or "").strip().strip(":")
    return cleaned or "emoji"


def _unicode_row_label(glyph: str) -> str:
    """Lowercased Unicode name beside a glyph. Empty when no real name exists."""
    if not glyph:
        return ""
    label = emoji_fallback_label(glyph)
    if not label or label == glyph or label == "emoji":
        return ""
    return label


def _glyph_size(row_h: float, label_size: int) -> int:
    """Icon edge. Taller rows grow past the old font cap and keep a few pixels of pad."""
    room = max(12, int(row_h) - 2)
    preferred = max(int(row_h) - 4, int(label_size) + 4)
    return min(room, max(12, preferred))


def _command_label(row) -> str:
    kind = str(getattr(row, "kind", "") or "")
    name = str(getattr(row, "name", "") or "")
    if kind == "both":
        return name
    if kind == "slash":
        return f"/{name}"
    return f"!{name}"


def _model_label(key: str) -> str:
    text = str(key or "")
    return _MODEL_LABELS.get(text, text)


def _dim_pairs(rows) -> list[tuple[str, int]]:
    return [(str(row.key), int(row.weekly)) for row in rows if int(row.weekly) > 0]


def _month_day_year(value) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def prepare(stats, names, guild_name: str, *, scope: str = "weekly", least_metric: str = "all_time") -> View:
    channels = _channel_map(names)
    emojis = _emoji_map(names)
    all_time = scope == "all_time"
    heat_source = stats.heatmap
    voice_source = stats.voice_heatmap
    if all_time and getattr(stats, "heatmap_all_time", None):
        heat_source = stats.heatmap_all_time
    if all_time and getattr(stats, "voice_heatmap_all_time", None):
        voice_source = stats.voice_heatmap_all_time
    heat = _grid(heat_source)
    voice_heat = _grid(voice_source)
    reactions: list[Row] = []
    for row in stats.top_reactions:
        if row.is_custom:
            label = _short_name(emojis.get(row.emoji_key) or row.name)
            reactions.append(Row(label, row.weekly, row.all_time, None, str(row.emoji_key)))
        else:
            glyph = str(row.name or row.emoji_key or "")
            reactions.append(Row(_unicode_row_label(glyph), row.weekly, row.all_time, glyph or None))
    least_source = stats.least_custom_emojis
    if least_metric == "weekly":
        weekly_least = getattr(stats, "least_custom_emojis_weekly", None)
        if weekly_least:
            least_source = weekly_least
    if all_time:
        least_source = stats.least_custom_emojis
    least = [
        Row(
            _short_name(emojis.get(row.emoji_key) or row.name),
            row.weekly,
            row.all_time,
            None,
            str(row.emoji_key) if row.is_custom else None,
        )
        for row in least_source
    ]
    image = stats.imagegen
    if all_time:
        models = [(_model_label(row.key), int(row.all_time)) for row in image.models if int(row.all_time) > 0]
    else:
        # Weekly MODEL MIX stays a top-5 donut (IMAGE_TOP_N). All-time folding needs every row.
        weekly_models = [(_model_label(row.key), int(row.weekly)) for row in image.models if int(row.weekly) > 0]
        weekly_models.sort(key=lambda item: (-item[1], item[0]))
        models = weekly_models[:5]
    channel_rows = stats.top_channels
    quiet_rows = stats.bottom_channels
    dead_rows = stats.dead_channels
    command_rows = stats.top_commands
    least_command_rows = stats.bottom_commands
    reaction_rows = None
    voice_rows = stats.top_voice_channels
    if all_time:
        channel_rows = getattr(stats, "top_channels_all_time", None) or ()
        quiet_rows = getattr(stats, "bottom_channels_all_time", None) or ()
        dead_rows = ()
        command_rows = getattr(stats, "top_commands_all_time", None) or ()
        least_command_rows = getattr(stats, "bottom_commands_all_time", None) or ()
        reaction_rows = getattr(stats, "top_reactions_all_time", None) or ()
        voice_rows = getattr(stats, "top_voice_channels_all_time", None) or ()
        reactions = []
        for row in reaction_rows:
            if row.is_custom:
                label = _short_name(emojis.get(row.emoji_key) or row.name)
                reactions.append(Row(label, row.weekly, row.all_time, None, str(row.emoji_key)))
            else:
                glyph = str(row.name or row.emoji_key or "")
                reactions.append(Row(_unicode_row_label(glyph), row.weekly, row.all_time, glyph or None))
    history = list(getattr(stats, "week_history", ()) or ())
    earliest = getattr(stats, "earliest_week", None) or stats.week_start
    live_since = getattr(stats, "live_since", None)
    if live_since is None:
        from datetime import date as _date

        live_since = _date(2026, 11, 19)
    imagegen_since = getattr(stats, "imagegen_since", None)
    messages_total = int(stats.messages_all_time)
    reactions_total = int(stats.reactions_all_time)
    commands_total = int(stats.commands_all_time)
    if all_time:
        kpis = [
            Kpi("Messages", messages_total, None, hide_delta=True),
            Kpi("Reactions", reactions_total, None, hide_delta=True),
            Kpi("Commands", commands_total, None, hide_delta=True),
        ]
    else:
        kpis = [
            Kpi("Messages", int(stats.messages_weekly), stats.messages_last_week),
            Kpi("Reactions", int(stats.reactions_weekly), stats.reactions_last_week),
            Kpi("Active members", int(stats.active_members), stats.active_members_last_week),
            Kpi("Voice minutes", int(stats.voice_minutes), stats.voice_minutes_last_week),
            Kpi("Image gens", int(image.generation_count), stats.imagegen_last_week),
        ]
    return View(
        guild=str(guild_name or "Server"),
        week=_week_label(stats),
        kpis=kpis,
        top_channels=[
            Row(_channel_label(channels, row.channel_id, True), row.weekly, row.all_time)
            for row in channel_rows
        ],
        bottom_channels=[
            Row(_channel_label(channels, row.channel_id, True), row.weekly, row.all_time)
            for row in quiet_rows
        ],
        dead_channels=[
            Row(_channel_label(channels, row.channel_id, True), row.weekly, row.all_time)
            for row in dead_rows
        ],
        top_commands=[Row(_command_label(row), row.weekly, row.all_time) for row in command_rows],
        bottom_commands=[Row(_command_label(row), row.weekly, row.all_time) for row in least_command_rows],
        reactions=reactions,
        least_emojis=least,
        voice_channels=[
            Row(_channel_label(channels, row.channel_id, False), row.weekly, row.all_time)
            for row in voice_rows
        ],
        voice_minutes=int(stats.voice_minutes),
        voice_all_time=int(stats.voice_minutes_all_time),
        heatmap=heat,
        voice_heatmap=voice_heat,
        daily=[sum(row) for row in heat],
        voice_daily=[sum(row) for row in voice_heat],
        voice_by_hour=[sum(heat_row[hour] for heat_row in voice_heat) for hour in range(24)],
        models=models,
        resolutions=_dim_pairs(image.resolutions),
        steps=_dim_pairs(image.steps),
        success_rate=image.success_rate,
        avg_seconds=image.avg_seconds,
        image_runs=int(image.generation_count),
        image_success=int(image.success_count),
        image_errors=int(image.error_count),
        kept=int(image.kept_first),
        regenerated=int(image.regenerate_count),
        edited=int(image.edit_count),
        history_messages=[int(point.messages) for point in history],
        history_reactions=[int(point.reactions) for point in history],
        history_members=[int(point.active_members) for point in history],
        earliest=_month_day_year(earliest),
        tracking_since=_month_day_year(live_since),
        imagegen_since=_month_day_year(imagegen_since) if imagegen_since else "",
        image_runs_all_time=int(getattr(image, "generation_count_all_time", 0) or 0),
    )


def draw_watermark(canvas: Canvas, text: str | None, face, fg, bg, border=None, margin: int = 18) -> tuple:
    if not text:
        return (canvas.size[0], 0, canvas.size[0], 0)
    pad_x, pad_y = 12, 6
    width = int(canvas.draw.textlength(str(text), font=face)) + pad_x * 2
    height = int(face.size + pad_y * 2)
    x1 = canvas.size[0] - margin
    x0 = x1 - width
    y0 = margin
    y1 = y0 + height
    canvas.rounded((x0, y0, x1, y1), height // 2, bg, outline=border, width=1)
    canvas.text((x0 + pad_x, (y0 + y1) / 2), str(text), face, fg, anchor="lm")
    return (x0, y0, x1, y1)


def soft_card(canvas: Canvas, box, radius: int, fill, shadow=(15, 23, 42, 28), border=None) -> None:
    x0, y0, x1, y1 = [int(v) for v in box]
    pad = 14
    crop = (
        max(0, x0 - pad),
        max(0, y0 - pad),
        min(canvas.size[0], x1 + pad),
        min(canvas.size[1], y1 + pad + 8),
    )
    layer = Image.new("RGBA", (crop[2] - crop[0], crop[3] - crop[1]), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle(
        (x0 - crop[0] + 1, y0 - crop[1] + 4, x1 - crop[0] + 1, y1 - crop[1] + 7),
        radius=radius,
        fill=shadow,
    )
    blurred = layer.filter(ImageFilter.GaussianBlur(7))
    canvas.image.alpha_composite(blurred, crop[:2])
    canvas.rounded((x0, y0, x1, y1), radius, fill, outline=border, width=1)


def cut_panel(canvas: Canvas, box, fill, outline, cut: int = 16) -> None:
    x0, y0, x1, y1 = [int(v) for v in box]
    cut = max(6, min(cut, (x1 - x0) // 5, (y1 - y0) // 5))
    poly = [
        (x0 + cut, y0),
        (x1 - cut, y0),
        (x1, y0 + cut),
        (x1, y1 - cut),
        (x1 - cut, y1),
        (x0 + cut, y1),
        (x0, y1 - cut),
        (x0, y0 + cut),
    ]
    canvas.draw.polygon(poly, fill=fill)
    if outline is not None:
        canvas.draw.line(poly + [poly[0]], fill=outline, width=2)


def centered(canvas: Canvas, box, value: str, face, fill) -> None:
    x0, y0, x1, y1 = box
    canvas.text(((x0 + x1) / 2, (y0 + y1) / 2), value, face, fill, max_width=max(8, x1 - x0 - 16), anchor="mm")


def _rows_fit(height: int, count: int, min_row: int, max_row: int, floor: int) -> tuple[int, int, float]:
    """Fit every row when the type still fits. Otherwise keep a final "+N more" line inside the box."""
    if count <= 0 or height <= 0:
        return 0, 0, float(max(floor, 1))
    floor = max(8, int(floor))
    preferred = max(floor, int(min_row))
    if count * preferred <= height:
        return count, 0, min(float(max_row), height / count)
    if count * floor <= height:
        return count, 0, height / count
    slots = max(1, int(height // floor))
    if slots <= 1 or count == 1:
        shown = 1 if count else 0
        return shown, max(0, count - shown), min(float(max_row), float(max(height, floor)))
    shown = slots - 1
    return shown, count - shown, height / slots


def _scaled_rgba(image: Image.Image, size: int) -> Image.Image:
    """Fit ``image`` inside a square of ``size`` without stretching it."""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    longest = max(width, height, 1)
    scale = max(1, int(size)) / longest
    resized = (
        max(1, int(round(width * scale))),
        max(1, int(round(height * scale))),
    )
    if resized != rgba.size:
        rgba = rgba.resize(resized, Image.Resampling.LANCZOS)
    return rgba


def _custom_emoji_image(emoji_id: str | None, size: int) -> Image.Image | None:
    """Cached custom emoji for a row, or None so the caller draws the text label."""
    if not emoji_id:
        return None
    from cogs.utils.emoji_cache import load_image

    cached = load_image(emoji_id)
    if cached is None:
        return None
    return _scaled_rgba(cached, size)


def row_value(row: Row, metric: str) -> int:
    return int(row.all_time if metric == "all_time" else row.weekly)


def row_bar_max(rows: list[Row], metric: str = "weekly") -> int:
    """Largest bar value in a list. Paired panels share this so short lists stay honest."""
    if not rows:
        return 0
    return max(row_value(row, metric) for row in rows)


def metric_peak(rows: list[Row], metric: str = "weekly", scale: int | None = None) -> int:
    peak = row_bar_max(rows, metric)
    if scale is not None:
        peak = max(peak, int(scale))
    return peak or 1


def bar_fill_width(value: int, peak: int, bar_w: int, *, min_px: int = 2) -> int:
    if bar_w <= 0 or value <= 0 or peak <= 0:
        return 0
    fill = int(round(bar_w * int(value) / int(peak)))
    return max(min_px, min(int(bar_w), fill))


def draw_metric_rows(
    canvas: Canvas,
    box,
    rows: list[Row],
    *,
    title: str,
    title_font,
    label_font,
    value_font,
    ink,
    muted,
    bar,
    track,
    title_fill=None,
    metric: str = "weekly",
    show_all_time: bool = True,
    bar_height: int = 8,
    min_row: int = 22,
    max_row: int = 40,
    pad: int = 14,
    empty: str = EMPTY,
    meta: str | None = None,
    show_columns: bool = True,
    bar_scale: int | None = None,
) -> None:
    x0, y0, x1, y1 = [int(v) for v in box]
    title_fill = ink if title_fill is None else title_fill
    cursor_y = y0 + pad
    title_w = (x1 - x0) - pad * 2
    if meta:
        meta_w = int(canvas.draw.textlength(meta, font=label_font)) + 8
        canvas.text((x1 - pad, cursor_y), meta, label_font, muted, anchor="rt")
        title_w -= meta_w
    canvas.text((x0 + pad, cursor_y), title, title_font, title_fill, max_width=max(20, title_w))
    cursor_y += int(title_font.size) + 8
    if not rows:
        centered(canvas, (x0 + pad, cursor_y, x1 - pad, y1 - pad), empty, label_font, muted)
        return
    if show_all_time:
        samples = [fmt(row.weekly) for row in rows] + [fmt(row.all_time) for row in rows]
    else:
        samples = [fmt(row_value(row, metric)) for row in rows]
    number_w = max(int(canvas.draw.textlength(sample, font=value_font)) for sample in samples) + 10
    all_right = x1 - pad
    week_right = all_right - (number_w + 14 if show_all_time else 0)
    if show_columns:
        header = "Week" if show_all_time or metric != "all_time" else "Total"
        canvas.text((week_right, cursor_y), header, value_font, muted, anchor="rt")
        if show_all_time:
            canvas.text((all_right, cursor_y), "All", value_font, muted, anchor="rt")
        cursor_y += int(label_font.size) + 6
    font_h = max(1, int(label_font.size))
    available = (y1 - 3) - cursor_y
    shown, extra, _row_h = _rows_fit(int(available), len(rows), min_row, max_row, font_h + 1)
    visible = list(rows[:shown])
    slots = shown + (1 if extra else 0)
    row_h = min(float(max_row), available / slots) if slots else float(font_h + 2)
    inner_left = x0 + pad
    emoji_only = bool(visible) and all(row.emoji and not row.label for row in visible)
    number_left = week_right - number_w
    bar_right = number_left - 12
    room = max(0, bar_right - inner_left)
    glyph_size = _glyph_size(row_h, font_h)
    if emoji_only:
        label_w = font_h + 14
    else:
        labels = [row.label for row in visible if row.label]
        natural = max((int(canvas.draw.textlength(sample, font=label_font)) for sample in labels), default=0)
        if any(row.custom_id or (row.emoji and row.label) for row in visible):
            natural += glyph_size + 6
        reserve = 56 if room > 140 else max(28, room // 4)
        label_w = min(max(font_h + 8, natural + 4), max(font_h + 8, room - reserve))
    bar_x = inner_left + label_w + 8
    bar_w = max(0, bar_right - bar_x)
    peak = metric_peak(rows, metric, bar_scale)
    for index, row in enumerate(visible):
        cy = cursor_y + index * row_h + row_h / 2
        label_x = inner_left
        room = max(8, bar_x - 8 - inner_left)
        custom = _custom_emoji_image(row.custom_id, glyph_size)
        if custom is not None:
            canvas.paste(custom, (label_x, cy - custom.size[1] / 2))
            label_x += custom.size[0] + 6
            room = max(8, bar_x - 8 - label_x)
            if row.label:
                canvas.text((label_x, cy), row.label, label_font, ink, max_width=room, anchor="lm")
        elif row.emoji:
            glyph = emoji_glyph(row.emoji, glyph_size)
            if glyph is None:
                fallback = emoji_fallback_label(row.emoji)
                canvas.text((label_x, cy), fallback, label_font, ink, max_width=room, anchor="lm")
            else:
                canvas.paste(glyph, (label_x, cy - glyph.size[1] / 2))
                label_x += glyph.size[0] + 6
                room = max(8, bar_x - 8 - label_x)
                if row.label:
                    canvas.text((label_x, cy), row.label, label_font, ink, max_width=room, anchor="lm")
        else:
            canvas.text((label_x, cy), row.label, label_font, ink, max_width=max(8, bar_x - 8 - label_x), anchor="lm")
        value = row_value(row, metric)
        if bar_w > 4:
            by0 = cy - bar_height / 2
            canvas.rounded((bar_x, by0, bar_x + bar_w, by0 + bar_height), bar_height / 2, track)
            fill_w = bar_fill_width(value, peak, bar_w)
            if fill_w > 1:
                canvas.rounded((bar_x, by0, bar_x + fill_w, by0 + bar_height), bar_height / 2, bar)
        primary = int(row.weekly) if show_all_time else row_value(row, metric)
        canvas.text((week_right, cy), fmt(primary), value_font, ink, anchor="rm")
        if show_all_time:
            canvas.text((all_right, cy), fmt(row.all_time), value_font, muted, anchor="rm")
    if extra:
        cy = cursor_y + shown * row_h + row_h / 2
        canvas.text((inner_left, cy), f"+{extra} more", label_font, muted, anchor="lm")


def draw_kpi(
    canvas: Canvas,
    box,
    kpi: Kpi,
    *,
    label_font,
    value_font,
    delta_font,
    ink,
    muted,
    up,
    down,
    accent,
    spark: Image.Image | None = None,
) -> None:
    x0, y0, x1, y1 = [int(v) for v in box]
    pad = 14
    canvas.text((x0 + pad, y0 + 12), kpi.label.upper(), label_font, muted, max_width=x1 - x0 - pad * 2)
    canvas.text((x0 + pad, y0 + 12 + label_font.size + 8), fmt(kpi.current), value_font, ink, anchor="lt")
    if spark is not None:
        sw, sh = spark.size
        sx = x1 - pad - sw
        sy = y1 - pad - sh
        if sx > x0 + pad:
            canvas.paste(spark, (sx, sy))
    if kpi.hide_delta or not kpi.delta:
        return
    color = tone_color(kpi.tone, up, down, muted, accent)
    delta_y = y1 - pad - 2
    if spark is not None:
        delta_y = sy - 4
    canvas.text((x0 + pad, delta_y), kpi.delta, delta_font, color, max_width=x1 - x0 - pad * 2 - (spark.size[0] + 8 if spark is not None else 0), anchor="lb")


def sparkline(values: list[int], size: tuple[int, int], color, fill) -> Image.Image:
    width, height = size
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    series = list(values) or [0]
    peak = max(series) if series else 0
    if peak <= 0:
        draw.line((0, height // 2, width, height // 2), fill=color + (140,), width=2)
        return image
    points = []
    last = max(1, len(series) - 1)
    for index, value in enumerate(series):
        x = index * (width - 1) / last
        y = (height - 3) - (value / peak) * (height - 6)
        points.append((x, y))
    polygon = [(points[0][0], height - 1)] + points + [(points[-1][0], height - 1)]
    draw.polygon(polygon, fill=fill + (80,))
    draw.line(points, fill=color + (255,), width=2)
    return image


def render_figure(width: int, height: int, facecolor, draw_fn) -> Image.Image:
    width = max(1, int(width))
    height = max(1, int(height))
    dpi = 144
    fig = Figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor=facecolor)
    FigureCanvasAgg(fig)
    try:
        draw_fn(fig)
        fig.canvas.draw()
        raw = np.array(fig.canvas.buffer_rgba(), dtype=np.uint8, copy=True)
        image = Image.fromarray(raw, mode="RGBA")
    finally:
        fig.clf()
        plt.close(fig)
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    return image


def chart_area(values: list[int], size: tuple[int, int], line, fill, bg, grid) -> Image.Image:
    series = [int(value) for value in values] or [0] * 7
    peak = max(series) if series else 0

    def draw(fig: Figure) -> None:
        ax = fig.add_axes((0.0, 0.04, 1.0, 0.94))
        ax.set_facecolor(hx(bg))
        xs = np.arange(len(series), dtype=float)
        ys = np.array(series, dtype=float)
        top = peak * 1.18 if peak else 1
        ax.axhline(top / 2, color=hx(grid), linewidth=0.8, zorder=0)
        ax.fill_between(xs, ys, color=(*[c / 255 for c in fill], 0.22), linewidth=0)
        ax.plot(xs, ys, color=hx(line), linewidth=2.4, solid_capstyle="round", zorder=2)
        ax.scatter(xs, ys, s=16, color=hx(line), zorder=3)
        ax.set_xlim(-0.15, len(series) - 0.85)
        ax.set_ylim(0, top)
        ax.axis("off")

    return render_figure(size[0], size[1], hx(bg), draw)


def chart_trends(series: list[tuple[list[int], tuple]], size: tuple[int, int], bg, grid) -> Image.Image:
    """One line per series. Each line is scaled to its own peak so member counts stay visible."""
    cleaned = []
    for values, color in series:
        points = [int(value) for value in values] or [0]
        cleaned.append((points, color))
    width = max(1, len(cleaned[0][0]) if cleaned else 1)

    def draw(fig: Figure) -> None:
        ax = fig.add_axes((0.03, 0.08, 0.94, 0.86))
        ax.set_facecolor(hx(bg))
        ax.axhline(0.5, color=hx(grid), linewidth=0.8, zorder=0)
        for values, color in cleaned:
            peak = max(values) if values else 0
            ys = [value / peak if peak else 0.0 for value in values]
            if len(ys) == 1:
                ys = [ys[0], ys[0]]
            xs = np.linspace(0, max(1, width - 1), len(ys))
            ax.plot(xs, ys, color=hx(color), linewidth=2.2, solid_capstyle="round", zorder=2)
        ax.set_xlim(-0.15, max(1, width - 1) + 0.15)
        ax.set_ylim(-0.05, 1.08)
        ax.axis("off")

    return render_figure(size[0], size[1], hx(bg), draw)


def chart_donut(values: list[int], colors: list[tuple], size: int, bg, hole: float = 0.64) -> Image.Image:
    cleaned = [max(0, int(value)) for value in values]
    if not cleaned or sum(cleaned) <= 0:
        image = Image.new("RGBA", (size, size), bg + (255,))
        draw = ImageDraw.Draw(image)
        width = max(8, int(size * (1 - hole) / 2))
        draw.ellipse((8, 8, size - 8, size - 8), outline=(colors[0] if colors else (120, 120, 120)) + (255,), width=width)
        return image

    def draw(fig: Figure) -> None:
        ax = fig.add_axes((0.02, 0.02, 0.96, 0.96))
        ax.set_facecolor(hx(bg))
        ax.pie(
            cleaned,
            colors=[hx(color) for color in colors[: len(cleaned)]],
            startangle=90,
            counterclock=False,
            wedgeprops={"width": 1 - hole, "edgecolor": hx(bg), "linewidth": 2},
        )

    return render_figure(size, size, hx(bg), draw)


def chart_stacked(segments: list[tuple[int, tuple]], size: tuple[int, int], bg) -> Image.Image:
    total = sum(max(0, int(value)) for value, _color in segments) or 1

    def draw(fig: Figure) -> None:
        ax = fig.add_axes((0.0, 0.18, 1.0, 0.64))
        ax.set_facecolor(hx(bg))
        left = 0.0
        for value, color in segments:
            amount = max(0, int(value))
            if amount <= 0:
                continue
            ax.barh([0], [amount], left=left, height=0.62, color=hx(color), linewidth=0)
            left += amount
        ax.set_xlim(0, total)
        ax.set_ylim(-0.8, 0.8)
        ax.axis("off")

    return render_figure(size[0], size[1], hx(bg), draw)


def _srgb_channel_to_linear(channel: float) -> float:
    value = max(0.0, min(255.0, float(channel))) / 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def _linear_channel_to_srgb(value: float) -> int:
    value = max(0.0, min(1.0, float(value)))
    if value <= 0.0031308:
        encoded = value * 12.92
    else:
        encoded = 1.055 * (value ** (1.0 / 2.4)) - 0.055
    return int(round(max(0.0, min(1.0, encoded)) * 255.0))


def _lerp_linear(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    mix = max(0.0, min(1.0, float(t)))
    return tuple(
        _linear_channel_to_srgb(_srgb_channel_to_linear(a[index]) * (1.0 - mix) + _srgb_channel_to_linear(b[index]) * mix)
        for index in range(3)
    )


def heat_color(value: int, peak: int, stops: list[tuple]) -> tuple[int, int, int]:
    """Map one cell onto a sequential ramp.

    ``stops[0]`` is the zero color and is never used for a positive count, so a
    single message stays visibly hotter than an empty hour.
    """
    if not stops:
        return (0, 0, 0)
    zero = tuple(int(channel) for channel in stops[0][:3])
    if int(value) <= 0 or int(peak) <= 0 or len(stops) == 1:
        return zero
    ramp = [tuple(int(channel) for channel in color[:3]) for color in stops[1:]]
    if len(ramp) == 1:
        return ramp[0]
    # Square root keeps a low count out of the zero swatch without blowing out the peak.
    position = (float(value) / float(peak)) ** 0.5
    position = max(0.0, min(1.0, position)) * (len(ramp) - 1)
    index = int(position)
    if index >= len(ramp) - 1:
        return ramp[-1]
    return _lerp_linear(ramp[index], ramp[index + 1], position - index)


def heat_legend_swatches(stops: list[tuple], peak: int, count: int = 5) -> list[tuple[int, int, int]]:
    count = max(2, int(count))
    zero = heat_color(0, peak, stops)
    if peak <= 0 or len(stops) < 2:
        return [zero] * count
    swatches = [zero]
    steps = count - 1
    for index in range(1, count):
        # Even steps in the painted ramp. heat_color applies its own sqrt, so square here.
        visual = index / steps
        value = max(1, int(round(peak * visual * visual)))
        swatches.append(heat_color(value, peak, stops))
    return swatches


def heat_geometry(box, *, gutter_left: int = 42, gutter_bottom: int = 22, gap: int = 3) -> tuple[int, int, int, int, int]:
    """Origin and cell size for a 7×24 grid. Cells are top-left aligned in the plot."""
    x0, y0, x1, y1 = [int(v) for v in box]
    plot_w = max(1, x1 - (x0 + int(gutter_left)))
    plot_h = max(1, (y1 - int(gutter_bottom)) - y0)
    gap = 2 if int(gap) < 2 or plot_w < 24 * 5 else min(3, int(gap))
    cell_w = max(1, (plot_w - gap * 23) // 24)
    cell_h = max(1, (plot_h - gap * 6) // 7)
    return x0 + int(gutter_left), y0, cell_w, cell_h, gap


def _cell_rect(origin_x: int, origin_y: int, cell_w: int, cell_h: int, gap: int, row: int, col: int) -> tuple[int, int, int, int]:
    left = origin_x + col * (cell_w + gap)
    top = origin_y + row * (cell_h + gap)
    return (left, top, left + cell_w - 1, top + cell_h - 1)


def _cell_radius(cell_w: int, cell_h: int) -> int:
    return max(1, min(3, cell_w // 3, cell_h // 3))


def draw_heat_legend(canvas: Canvas, right_xy, stops: list[tuple], peak: int, face, fill) -> int:
    """Right-aligned ramp from zero to the peak. ``right_xy`` is (right, mid_y). Returns the left edge."""
    label = f"0 → {int(peak):,}"
    text_w = int(canvas.draw.textlength(label, font=face))
    swatch = max(10, int(getattr(face, "size", 12)) - 2)
    count = 5
    gap = 2
    strip = count * swatch + (count - 1) * gap
    right, mid = int(right_xy[0]), float(right_xy[1])
    left = int(right - (strip + 8 + text_w))
    for index, color in enumerate(heat_legend_swatches(stops, peak, count)):
        x = left + index * (swatch + gap)
        canvas.rounded((x, mid - swatch / 2, x + swatch - 1, mid + swatch / 2), 2, color)
    canvas.text((left + strip + 8, mid), label, face, fill, anchor="lm")
    return left


def draw_heatmap(
    canvas: Canvas,
    box,
    grid: list[list[int]],
    colors: list[tuple],
    label_font,
    label_fill,
    *,
    smooth: bool = False,
    glow: int = 0,
    radius: int = 3,
    gutter_left: int = 42,
    gutter_bottom: int = 22,
    gap: int = 3,
    empty_font=None,
    empty_fill=None,
) -> None:
    """Discrete 7×24 cells. ``smooth`` is ignored; the grid is never interpolated."""
    del smooth, radius
    x0, y0, x1, y1 = [int(v) for v in box]
    origin_x, origin_y, cell_w, cell_h, gap = heat_geometry(
        box, gutter_left=gutter_left, gutter_bottom=gutter_bottom, gap=gap
    )
    rows = []
    for day in range(7):
        source = list(grid[day]) if day < len(grid or []) else []
        padded = [int(value) for value in source[:24]]
        if len(padded) < 24:
            padded.extend([0] * (24 - len(padded)))
        rows.append(padded)
    peak = max((value for row in rows for value in row), default=0)
    cells = []
    for day, row in enumerate(rows):
        for hour, value in enumerate(row):
            rect = _cell_rect(origin_x, origin_y, cell_w, cell_h, gap, day, hour)
            cells.append((rect, heat_color(value, peak, colors), value))
    if glow and peak > 0:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        paint = ImageDraw.Draw(layer)
        blur = max(2, min(4, int(glow)))
        for rect, color, value in cells:
            if value <= 0:
                continue
            visual = (value / peak) ** 0.5
            if visual < 0.55:
                continue
            alpha = int(36 + 70 * visual)
            pad = 2
            paint.rounded_rectangle(
                (rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad),
                radius=_cell_radius(cell_w, cell_h) + 1,
                fill=tuple(color) + (alpha,),
            )
        canvas.image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius=blur)))
    corner = _cell_radius(cell_w, cell_h)
    for rect, color, _value in cells:
        canvas.rounded(rect, corner, color)
    for day, name in enumerate(DAY_NAMES):
        rect = _cell_rect(origin_x, origin_y, cell_w, cell_h, gap, day, 0)
        canvas.text((x0, (rect[1] + rect[3]) / 2), name, label_font, label_fill, max_width=max(8, gutter_left - 6), anchor="lm")
    label_top = origin_y + 7 * cell_h + 6 * gap + 3
    for hour, text in HOUR_TICKS:
        rect = _cell_rect(origin_x, origin_y, cell_w, cell_h, gap, 0, hour)
        center = (rect[0] + rect[2]) / 2
        anchor = "mt"
        text_w = canvas.draw.textlength(text, font=label_font)
        if center - text_w / 2 < origin_x:
            center = rect[0]
            anchor = "lt"
        elif center + text_w / 2 > x1 - 2:
            center = min(rect[2], x1 - 2)
            anchor = "rt"
        canvas.text((center, label_top), text, label_font, label_fill, anchor=anchor)
    if peak <= 0:
        centered(
            canvas,
            (origin_x, origin_y, origin_x + 24 * cell_w + 23 * gap, origin_y + 7 * cell_h + 6 * gap),
            EMPTY,
            empty_font or label_font,
            empty_fill or label_fill,
        )


def draw_day_labels(canvas: Canvas, box, font_face, fill) -> None:
    x0, y0, x1, y1 = box
    for index, name in enumerate(DAY_NAMES):
        x = x0 + (index + 0.5) * (x1 - x0) / 7
        canvas.text((x, (y0 + y1) / 2), name, font_face, fill, anchor="mm")


def draw_hour_bars(canvas: Canvas, box, values: list[int], color, track, radius: int = 2, *, colors=None, gap: int = 3) -> None:
    """One rounded cell per hour, colored on the same ramp as the heatmap."""
    del radius
    x0, y0, x1, y1 = [int(v) for v in box]
    if x1 - x0 < 8 or y1 - y0 < 4:
        return
    series = [int(value) for value in list(values[:24])]
    if len(series) < 24:
        series.extend([0] * (24 - len(series)))
    stops = [tuple(item[:3]) for item in colors] if colors else [tuple(track[:3]), tuple(color[:3])]
    if len(stops) == 1:
        stops = [stops[0], stops[0]]
    gap = 2 if int(gap) < 2 or (x1 - x0) < 24 * 5 else min(3, int(gap))
    cell_w = max(1, ((x1 - x0) - gap * 23) // 24)
    cell_h = max(1, y1 - y0)
    grid_w = cell_w * 24 + gap * 23
    origin_x = x0 + max(0, (x1 - x0 - grid_w) // 2)
    peak = max(series) if series else 0
    corner = _cell_radius(cell_w, cell_h)
    for hour, value in enumerate(series):
        rect = _cell_rect(origin_x, y0, cell_w, cell_h, gap, 0, hour)
        canvas.rounded(rect, corner, heat_color(value, peak, stops))


def draw_legend(
    canvas: Canvas,
    box,
    items: list[tuple[str, int, tuple]],
    font_face,
    ink,
    muted,
    *,
    row_h: float | None = None,
) -> None:
    x0, y0, x1, y1 = [int(v) for v in box]
    if not items or sum(value for _label, value, _color in items) <= 0:
        centered(canvas, box, EMPTY, font_face, muted)
        return
    total = sum(value for _label, value, _color in items) or 1
    slots = max(1, len(items))
    if row_h is None:
        row_h = min(26, max(16, (y1 - y0) / slots))
    else:
        row_h = min(float(row_h), (y1 - y0) / slots)
    for index, (label, value, color) in enumerate(items):
        top = y0 + index * row_h
        if top + 8 > y1:
            canvas.text((x0, y1 - 2), f"+{len(items) - index} more", font_face, muted, anchor="lb")
            break
        cy = top + row_h / 2
        canvas.rounded((x0, cy - 5, x0 + 10, cy + 5), 2, color)
        canvas.text((x0 + 16, cy), label, font_face, ink, max_width=max(20, x1 - x0 - 70), anchor="lm")
        canvas.text((x1, cy), f"{value / total * 100:.0f}%", font_face, muted, anchor="rm")


def draw_gauge(canvas: Canvas, box, rate: float | None, track, color, width: int = 14) -> None:
    x0, y0, x1, y1 = [int(v) for v in box]
    inset = int(width / 2) + 1
    x0, y0, x1, y1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    if x1 - x0 < 8 or y1 - y0 < 8:
        return
    canvas.draw.arc((x0, y0, x1, y1), start=135, end=405, fill=track, width=width)
    if rate is None or rate <= 0:
        return
    extent = max(2, int(round(270 * min(1.0, float(rate)))))
    canvas.draw.arc((x0, y0, x1, y1), start=135, end=135 + extent, fill=color, width=width)


def percent(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{rate * 100:.0f}%"


def seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}s"


def kpi_series(kpi: Kpi, daily: list[int], voice_daily: list[int]) -> list[int]:
    if kpi.label == "Messages" and any(daily):
        return daily
    if kpi.label == "Voice minutes" and any(voice_daily):
        return voice_daily
    if kpi.previous is None:
        return [0, kpi.current]
    return [kpi.previous, kpi.current]


def outcome_segments(view: View, colors: dict) -> list[tuple[int, tuple]]:
    return [
        (view.kept, colors["kept"]),
        (view.regenerated, colors["regen"]),
        (view.edited, colors["edit"]),
        (view.image_errors, colors["error"]),
    ]


def model_colors(count: int, palette: list[tuple]) -> list[tuple]:
    if not palette:
        return [(120, 120, 120)] * count
    return [palette[index % len(palette)] for index in range(count)]
