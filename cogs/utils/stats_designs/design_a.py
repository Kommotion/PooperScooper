"""Design A. Dark analytics dashboard in the Grafana / Vercel style."""

from __future__ import annotations

from cogs.utils.stats_designs._common import (
    EMPTY,
    Canvas,
    chart_donut,
    chart_stacked,
    chart_trends,
    Row,
    draw_heat_legend,
    draw_heatmap,
    draw_hour_bars,
    draw_kpi,
    draw_legend,
    draw_metric_rows,
    draw_watermark,
    font,
    fmt,
    hsplit,
    model_colors,
    outcome_segments,
    percent,
    prepare,
    row_bar_max,
    seconds,
    vsplit,
    _rows_fit,
)

WIDTH = 1440
M = 24
G = 12
HEADER = 100
KPI_H = 122
CHART_H = 430
HEAT_H = 268
MID_H = 312
LOW_H = 336
FOOT = 20
HEIGHT = M + HEADER + G + KPI_H + G + CHART_H + G + HEAT_H + G + MID_H + G + LOW_H + G + FOOT + M
SIZE = (WIDTH, HEIGHT)

BG = (9, 11, 16)
PANEL = (18, 22, 32)
BORDER = (42, 49, 66)
INK = (244, 245, 247)
MUTED = (148, 156, 172)
ACCENT = (129, 140, 248)
GOLD = (251, 191, 36)
CYAN = (34, 211, 238)
GOOD = (52, 211, 153)
BAD = (248, 113, 113)
TRACK = (32, 38, 54)
# Zero is a dark navy cell. The ramp starts at teal so a count of 1 is not the empty color.
HEAT = [(26, 40, 68), (14, 92, 112), (16, 156, 150), (48, 214, 196), (186, 236, 96)]
MODELS = [(129, 140, 248), (34, 211, 238), (167, 139, 250), (52, 211, 153), (251, 191, 36), (251, 113, 133)]
OUTCOMES = {"kept": GOOD, "regen": ACCENT, "edit": CYAN, "error": BAD}


def _panel(canvas: Canvas, box) -> None:
    canvas.rounded(box, 14, PANEL, outline=BORDER, width=1)


def _rows(canvas, box, rows, title, color, **extra) -> None:
    options = dict(
        title=title,
        title_font=font("semibold", 15),
        label_font=font("regular", 14),
        value_font=font("medium", 13),
        ink=INK,
        muted=MUTED,
        bar=color,
        track=TRACK,
        title_fill=INK,
        bar_height=8,
        min_row=22,
        max_row=34,
        show_all_time=False,
    )
    options.update(extra)
    draw_metric_rows(canvas, box, rows, **options)


def _reaction_width_samples(canvas, box, rows, *, pad, min_row, max_row):
    """Number and label text widths behind a TOP REACTIONS column grid.

    ``draw_metric_rows`` adds 10 to the number width, 4 to the label width, and
    the icon width when a row has a glyph or custom image. The pair returned
    here is the raw text widths so that addition happens once.
    """
    if not rows:
        return None
    label_font = font("regular", 14)
    value_font = font("medium", 13)
    title_font = font("semibold", 15)
    x0, y0, x1, y1 = [int(v) for v in box]
    cursor_y = y0 + pad + int(title_font.size) + 8 + int(label_font.size) + 6
    font_h = max(1, int(label_font.size))
    available = (y1 - 3) - cursor_y
    shown, _extra, _row_h = _rows_fit(int(available), len(rows), min_row, max_row, font_h + 1)
    visible = list(rows[:shown])
    samples = [fmt(row.weekly) for row in rows]
    number_text_w = max(int(canvas.draw.textlength(sample, font=value_font)) for sample in samples)
    emoji_only = bool(visible) and all(row.emoji and not row.label for row in visible)
    if emoji_only:
        label_w = font_h + 14
    else:
        labels = [row.label for row in visible if row.label]
        natural = max((int(canvas.draw.textlength(sample, font=label_font)) for sample in labels), default=0)
        return number_text_w, natural
    return number_text_w, max(0, label_w - 4)


def _rows_on_reaction_columns(canvas, box, rows, title, color, samples, **extra) -> None:
    """Draw ``rows`` with ``draw_metric_rows``, forced onto a reaction column grid."""
    if not samples:
        _rows(canvas, box, rows, title, color, **extra)
        return
    number_text_w, label_text_w = samples
    value_font = font("medium", 13)
    label_font = font("regular", 14)
    draw = canvas.draw
    real = draw.textlength

    def textlength(text, font=None, *args, **kwargs):
        if font is value_font:
            return number_text_w
        if font is label_font:
            return label_text_w
        return real(text, font, *args, **kwargs)

    draw.textlength = textlength
    try:
        _rows(canvas, box, rows, title, color, **extra)
    finally:
        draw.textlength = real


def _breakdown(canvas: Canvas, box, pairs, title, color) -> None:
    rows = [Row(str(label), int(value), int(value)) for label, value in pairs]
    _rows(
        canvas,
        box,
        rows,
        title,
        color,
        pad=12,
        min_row=18,
        max_row=24,
        show_all_time=False,
        show_columns=False,
        bar_height=6,
        title_font=font("semibold", 13),
        label_font=font("regular", 12),
        value_font=font("medium", 12),
    )


def _image_panel(canvas: Canvas, box, view) -> None:
    _panel(canvas, box)
    summary, detail = vsplit(box, [198, 114], 0)
    x0, y0, x1, y1 = summary
    pad = 16
    canvas.text((x0 + pad, y0 + 12), "IMAGE GENERATION", font("semibold", 15), INK)
    canvas.text((x0 + pad, y0 + 38), percent(view.success_rate), font("bold", 32), INK)
    canvas.text((x0 + pad, y0 + 74), "success", font("medium", 13), MUTED)
    canvas.text((x1 - pad, y0 + 46), seconds(view.avg_seconds), font("bold", 24), CYAN, anchor="rt")
    canvas.text((x1 - pad, y0 + 74), "avg time", font("medium", 13), MUTED, anchor="rt")
    bar = (x0 + pad, y0 + 98, x1 - pad, y0 + 128)
    segments = outcome_segments(view, OUTCOMES)
    if sum(value for value, _color in segments) <= 0:
        canvas.text((x0 + pad, y0 + 104), EMPTY, font("regular", 14), MUTED)
    else:
        image = chart_stacked(segments, (max(1, bar[2] - bar[0]), max(1, bar[3] - bar[1])), PANEL)
        canvas.paste(image, (bar[0], bar[1]))
    stats = (
        ("Kept", view.kept, GOOD),
        ("Regen", view.regenerated, ACCENT),
        ("Edited", view.edited, CYAN),
        ("Errors", view.image_errors, BAD),
    )
    cells = hsplit((x0 + pad, y0 + 136, x1 - pad, y1 - 4), [1, 1, 1, 1], 8)
    for cell, (label, value, color) in zip(cells, stats):
        canvas.text((cell[0], cell[1] + 2), label.upper(), font("medium", 11), MUTED, max_width=cell[2] - cell[0])
        canvas.text((cell[0], cell[1] + 18), f"{value:,}", font("semibold", 16), color, max_width=cell[2] - cell[0])
    left, right = hsplit(detail, [1.15, 1], 0)
    _breakdown(canvas, left, view.resolutions, "RESOLUTION", CYAN)
    _breakdown(canvas, right, [(f"{name} steps", count) for name, count in view.steps], "STEPS", ACCENT)


def _voice_panel(canvas: Canvas, box, view) -> None:
    _panel(canvas, box)
    head, body, hours = vsplit(box, [78, 150, 84], 0)
    canvas.text((head[0] + 16, head[1] + 14), "VOICE", font("semibold", 15), INK)
    canvas.text((head[0] + 16, head[1] + 40), f"{view.voice_minutes:,} min", font("bold", 24), INK)
    canvas.text(
        (head[2] - 16, head[1] + 48),
        f"{view.voice_minutes / 60:.1f} h this week",
        font("medium", 13),
        MUTED,
        anchor="rt",
        max_width=head[2] - head[0] - 180,
    )
    if view.voice_channels:
        _rows(canvas, (body[0], body[1] - 8, body[2], body[3]), view.voice_channels, "Channels", CYAN, pad=12, min_row=22)
    else:
        canvas.text(((body[0] + body[2]) / 2, (body[1] + body[3]) / 2), EMPTY, font("regular", 14), MUTED, anchor="mm")
    canvas.text((hours[0] + 16, hours[1] + 2), "HOURS (PT)", font("medium", 11), MUTED)
    draw_hour_bars(
        canvas,
        (hours[0] + 16, hours[1] + 20, hours[2] - 16, hours[3] - 14),
        view.voice_by_hour,
        CYAN,
        TRACK,
        colors=HEAT,
    )


def _donut_panel(canvas: Canvas, box, view) -> None:
    _panel(canvas, box)
    x0, y0, x1, y1 = box
    canvas.text((x0 + 16, y0 + 14), "MODEL MIX", font("semibold", 15), INK)
    body_top = y0 + 42
    body_bottom = y1 - 12
    legend_w = 168
    size = min(body_bottom - body_top, max(96, (x1 - x0) - 36 - legend_w))
    top = body_top + max(0, (body_bottom - body_top - size) // 2)
    colors = model_colors(len(view.models), MODELS)
    donut = chart_donut([value for _name, value in view.models], colors, size, PANEL)
    canvas.paste(donut, (x0 + 12, top))
    count = max(1, len(view.models))
    draw_legend(
        canvas,
        (x0 + 22 + size, top, x1 - 14, top + size),
        [(name, value, colors[index]) for index, (name, value) in enumerate(view.models)],
        font("regular", 14),
        INK,
        MUTED,
        row_h=size / count,
    )


def render(stats, names, guild_name: str, *, watermark: str | None = None) -> bytes:
    view = prepare(stats, names, guild_name, least_metric="weekly")
    canvas = Canvas(SIZE, BG)
    mark = draw_watermark(canvas, watermark, font("semibold", 12), INK, (28, 34, 48), BORDER, margin=M)
    limit = mark[0] - 16
    canvas.text((M, M + 6), "THIS WEEK", font("medium", 13), ACCENT)
    canvas.text((limit, M + 8), view.week, font("medium", 15), MUTED, anchor="rt", max_width=max(80, limit - M - 200))
    canvas.text((M, M + 36), view.guild, font("bold", 34), INK, max_width=WIDTH - 2 * M)
    canvas.draw.line((M, M + HEADER - 10, WIDTH - M, M + HEADER - 10), fill=BORDER, width=1)

    y = M + HEADER + G
    tiles = hsplit((M, y, WIDTH - M, y + KPI_H), [1, 1, 1, 1, 1], G)
    for tile, kpi in zip(tiles, view.kpis):
        _panel(canvas, tile)
        draw_kpi(
            canvas,
            tile,
            kpi,
            label_font=font("medium", 12),
            value_font=font("bold", 30),
            delta_font=font("semibold", 14),
            ink=INK,
            muted=MUTED,
            up=GOOD,
            down=BAD,
            accent=ACCENT,
        )

    y += KPI_H + G
    left, right = hsplit((M, y, WIDTH - M, y + CHART_H), [1.25, 1], G)
    _panel(canvas, left)
    _rows(canvas, left, view.top_channels, "TOP CHANNELS", ACCENT, meta="messages")
    # 10 reactions over 6 least-used emojis fill this panel at the same row height.
    # max_row lets a shorter list grow instead of leaving a blank band.
    top_react, low_emoji = vsplit(right, [1.5, 1], 0)
    _panel(canvas, right)
    reaction_pad = 12
    _rows(
        canvas,
        top_react,
        view.reactions,
        "TOP REACTIONS",
        (251, 113, 133),
        pad=reaction_pad,
        min_row=20,
        max_row=100,
    )
    # Vertical pad stays 6 so the six emoji rows keep that shared row height.
    # The box inset makes the left/right content pad match the reactions list.
    emoji_pad = 6
    side = reaction_pad - emoji_pad
    emoji_box = (low_emoji[0] + side, low_emoji[1], low_emoji[2] - side, low_emoji[3])
    _rows_on_reaction_columns(
        canvas,
        emoji_box,
        view.least_emojis,
        "LEAST-USED EMOJIS",
        (251, 191, 36),
        _reaction_width_samples(
            canvas,
            top_react,
            view.reactions,
            pad=reaction_pad,
            min_row=20,
            max_row=100,
        ),
        pad=emoji_pad,
        min_row=18,
        max_row=80,
        metric="weekly",
        bar_scale=row_bar_max(view.reactions, "weekly"),
    )

    y += CHART_H + G
    heat = (M, y, WIDTH - M, y + HEAT_H)
    _panel(canvas, heat)
    peak = max((value for row in view.heatmap for value in row), default=0)
    legend_left = draw_heat_legend(canvas, (WIDTH - M - 16, y + 24), HEAT, peak, font("medium", 12), MUTED)
    canvas.text((M + 16, y + 14), "BUSIEST HOURS  ·  PT", font("semibold", 15), INK, max_width=max(80, legend_left - (M + 24)))
    draw_heatmap(
        canvas,
        (M + 16, y + 46, WIDTH - M - 16, y + HEAT_H - 12),
        view.heatmap,
        HEAT,
        font("medium", 12),
        MUTED,
        gutter_left=46,
        gutter_bottom=22,
        empty_font=font("regular", 14),
        empty_fill=MUTED,
    )

    y += HEAT_H + G
    donut, image, voice = hsplit((M, y, WIDTH - M, y + MID_H), [1.05, 1.15, 1.15], G)
    _donut_panel(canvas, donut, view)
    _image_panel(canvas, image, view)
    _voice_panel(canvas, voice, view)

    y += MID_H + G
    quiet, dead, commands, least_commands = hsplit((M, y, WIDTH - M, y + LOW_H), [1, 1, 1.15, 1], G)
    for box in (quiet, dead, commands, least_commands):
        _panel(canvas, box)
    _rows(
        canvas,
        quiet,
        view.bottom_channels,
        "QUIET CHANNELS",
        (167, 139, 250),
        bar_scale=row_bar_max(view.top_channels),
    )
    _rows(canvas, dead, view.dead_channels, "DEAD CHANNELS", (100, 116, 139))
    _rows(canvas, commands, view.top_commands, "TOP COMMANDS", CYAN)
    _rows(
        canvas,
        least_commands,
        view.bottom_commands,
        "LEAST-USED COMMANDS",
        (251, 191, 36),
        bar_scale=row_bar_max(view.top_commands),
    )

    canvas.text((M, HEIGHT - M), "Aggregate counts only. Message text and prompts are not stored.", font("regular", 12), MUTED, anchor="lb")
    return canvas.png()


def fold_imagegen_models(pairs) -> list[tuple[str, int]]:
    """Top five all-time models, then one other row. Unknown stays its own line."""
    named: list[tuple[str, int]] = []
    unknown = 0
    for label, count in pairs or ():
        value = int(count)
        if value <= 0:
            continue
        text = str(label)
        if text == "unknown":
            unknown += value
        else:
            named.append((text, value))
    ranked = list(named)
    if unknown > 0:
        ranked.append(("unknown", unknown))
    ranked.sort(key=lambda item: item[1], reverse=True)
    top = ranked[:5]
    other = 0
    unknown_outside = 0
    folded: list[tuple[str, int]] = []
    for label, value in top:
        if label == "unknown":
            folded.append(("unknown", value))
        else:
            folded.append((label, value))
    for label, value in ranked[5:]:
        if label == "unknown":
            unknown_outside += value
        else:
            other += value
    if other > 0:
        folded.append(("other", other))
    if unknown_outside > 0:
        folded.append(("unknown", unknown_outside))
    return folded


def _imagegen_alltime_panel(canvas: Canvas, box, view) -> None:
    """All-time image runs, the tracking date, and a compact per-model list."""
    _panel(canvas, box)
    x0, y0, x1, y1 = box
    canvas.text((x0 + 16, y0 + 12), "IMAGE GENERATION", font("semibold", 15), INK)
    since = view.imagegen_since
    note = f"tracking since {since}" if since else ""
    total = int(view.image_runs_all_time or 0)
    rows = fold_imagegen_models(view.models)
    if total > 0:
        canvas.text((x0 + 16, y0 + 36), f"{total:,} runs", font("bold", 22), CYAN)
        cursor = y0 + 64
        if note:
            canvas.text((x0 + 16, cursor), note, font("medium", 13), MUTED, max_width=x1 - x0 - 32)
            cursor += 20
    elif note:
        canvas.text((x0 + 16, y0 + 40), note, font("medium", 14), GOLD, max_width=x1 - x0 - 32)
        canvas.text(
            (x0 + 16, y0 + 62),
            "Not counted before that date.",
            font("regular", 12),
            MUTED,
            max_width=x1 - x0 - 32,
        )
        cursor = y0 + 84
    else:
        cursor = y0 + 42
    if rows and cursor < y1 - 24:
        _breakdown(canvas, (x0, cursor, x1, y1), rows, "", CYAN)


def _tracking_panel(canvas: Canvas, box, title: str, total: int, unit: str, since: str, color) -> None:
    """Totals for a category that only has live data, plus the day tracking started."""
    _panel(canvas, box)
    x0, y0, x1, y1 = box
    canvas.text((x0 + 16, y0 + 16), title, font("semibold", 15), INK)
    note = f"tracking since {since}" if since else ""
    if total > 0:
        canvas.text((x0 + 16, y0 + 48), f"{total:,} {unit}", font("bold", 28), color)
        if note:
            canvas.text((x0 + 16, y0 + 92), note, font("medium", 14), MUTED, max_width=x1 - x0 - 32)
    elif note:
        canvas.text((x0 + 16, y0 + 52), note, font("medium", 16), GOLD, max_width=x1 - x0 - 32)
        canvas.text(
            (x0 + 16, y0 + 80),
            "Not counted before that date.",
            font("regular", 13),
            MUTED,
            max_width=x1 - x0 - 32,
        )


def render_alltime(stats, names, guild_name: str, *, watermark: str | None = None) -> bytes:
    """All-time dashboard. Gold header so it is not the weekly card."""
    view = prepare(stats, names, guild_name, scope="all_time")
    canvas = Canvas(SIZE, BG)
    mark = draw_watermark(canvas, watermark, font("semibold", 12), INK, (48, 36, 16), GOLD, margin=M)
    limit = mark[0] - 16
    canvas.text((M, M + 6), "ALL TIME", font("medium", 13), GOLD)
    canvas.text(
        (limit, M + 8),
        f"since {view.earliest}",
        font("medium", 15),
        GOLD,
        anchor="rt",
        max_width=max(80, limit - M - 200),
    )
    canvas.text((M, M + 36), view.guild, font("bold", 34), INK, max_width=WIDTH - 2 * M)
    canvas.draw.line((M, M + HEADER - 10, WIDTH - M, M + HEADER - 10), fill=GOLD, width=3)

    y = M + HEADER + G
    tiles = hsplit((M, y, WIDTH - M, y + KPI_H), [1, 1, 1], G)
    for tile, kpi in zip(tiles, view.kpis):
        _panel(canvas, tile)
        draw_kpi(
            canvas,
            tile,
            kpi,
            label_font=font("medium", 12),
            value_font=font("bold", 30),
            delta_font=font("semibold", 14),
            ink=INK,
            muted=MUTED,
            up=GOOD,
            down=BAD,
            accent=GOLD,
        )

    y += KPI_H + G
    trend, channels = hsplit((M, y, WIDTH - M, y + CHART_H), [1.35, 1], G)
    _panel(canvas, trend)
    tx0, ty0, tx1, ty1 = trend
    canvas.text((tx0 + 16, ty0 + 14), "WEEKLY TREND", font("semibold", 15), INK)
    canvas.text((tx0 + 150, ty0 + 16), "each line scaled to its own peak", font("regular", 12), MUTED)
    series = [
        (view.history_messages or [0], ACCENT),
        (view.history_reactions or [0], (251, 113, 133)),
        (view.history_members or [0], GOLD),
    ]
    chart = chart_trends(series, (max(1, tx1 - tx0 - 32), max(1, ty1 - ty0 - 78)), PANEL, BORDER)
    canvas.paste(chart, (tx0 + 16, ty0 + 42))
    legend = (("Messages", ACCENT), ("Reactions", (251, 113, 133)), ("Active members", GOLD))
    axis_font = font("medium", 12)
    legend_y = ty1 - 28
    left_label = view.earliest
    right_label = view.week
    left_w = int(canvas.draw.textlength(left_label, font=axis_font)) if left_label else 0
    right_w = int(canvas.draw.textlength(right_label, font=axis_font)) if right_label else 0
    if left_label:
        canvas.text((tx0 + 16, legend_y), left_label, axis_font, MUTED)
    if right_label:
        canvas.text((tx1 - 16, legend_y), right_label, axis_font, MUTED, anchor="rt")
    cursor = tx0 + 16 + (left_w + 18 if left_label else 0)
    right_limit = tx1 - 16 - (right_w + 18 if right_label else 0)
    for label, color in legend:
        label_w = int(canvas.draw.textlength(label, font=axis_font))
        if cursor + 18 + label_w > right_limit:
            break
        canvas.rounded((cursor, legend_y, cursor + 12, legend_y + 12), 2, color)
        canvas.text((cursor + 18, legend_y), label, axis_font, MUTED)
        cursor += 18 + label_w + 22
    _panel(canvas, channels)
    _rows(canvas, channels, view.top_channels, "TOP CHANNELS", GOLD, meta="all time", metric="all_time")

    y += CHART_H + G
    heat = (M, y, WIDTH - M, y + HEAT_H)
    _panel(canvas, heat)
    peak = max((value for row in view.heatmap for value in row), default=0)
    legend_left = draw_heat_legend(canvas, (WIDTH - M - 16, y + 24), HEAT, peak, font("medium", 12), MUTED)
    canvas.text(
        (M + 16, y + 14),
        "BUSIEST HOURS  ·  ALL TIME  ·  PT",
        font("semibold", 15),
        INK,
        max_width=max(80, legend_left - (M + 24)),
    )
    draw_heatmap(
        canvas,
        (M + 16, y + 46, WIDTH - M - 16, y + HEAT_H - 12),
        view.heatmap,
        HEAT,
        font("medium", 12),
        MUTED,
        gutter_left=46,
        gutter_bottom=22,
        empty_font=font("regular", 14),
        empty_fill=MUTED,
    )

    y += HEAT_H + G
    reactions, emojis, commands = hsplit((M, y, WIDTH - M, y + MID_H), [1, 1, 1], G)
    for box in (reactions, emojis, commands):
        _panel(canvas, box)
    _rows(canvas, reactions, view.reactions, "TOP REACTIONS", (251, 113, 133), metric="all_time", pad=12, min_row=20, max_row=40)
    _rows(canvas, emojis, view.least_emojis, "LEAST-USED EMOJIS", GOLD, metric="all_time", pad=12, min_row=18, max_row=36)
    _rows(canvas, commands, view.top_commands, "TOP COMMANDS", CYAN, metric="all_time")

    y += MID_H + G
    quiet, least_commands, image, voice = hsplit((M, y, WIDTH - M, y + LOW_H), [1, 1, 1, 1], G)
    _panel(canvas, quiet)
    _panel(canvas, least_commands)
    _rows(
        canvas,
        quiet,
        view.bottom_channels,
        "QUIET CHANNELS",
        (167, 139, 250),
        metric="all_time",
        bar_scale=row_bar_max(view.top_channels, "all_time"),
    )
    _rows(
        canvas,
        least_commands,
        view.bottom_commands,
        "LEAST-USED COMMANDS",
        GOLD,
        metric="all_time",
        bar_scale=row_bar_max(view.top_commands, "all_time"),
    )
    _imagegen_alltime_panel(canvas, image, view)
    _tracking_panel(canvas, voice, "VOICE", view.voice_all_time, "min", view.tracking_since, CYAN)

    canvas.text((M, HEIGHT - M), "Aggregate counts only. Message text and prompts are not stored.", font("regular", 12), MUTED, anchor="lb")
    return canvas.png()
