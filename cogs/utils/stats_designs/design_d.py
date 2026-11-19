"""Design D. One Discord-preview screen, 1600×900, 18px minimum type."""

from __future__ import annotations

from cogs.utils.stats_designs._common import (
    EMPTY,
    Canvas,
    chart_donut,
    chart_stacked,
    draw_gauge,
    draw_heat_legend,
    draw_heatmap,
    draw_hour_bars,
    draw_legend,
    draw_metric_rows,
    draw_watermark,
    font,
    hsplit,
    model_colors,
    outcome_segments,
    percent,
    prepare,
    row_bar_max,
    seconds,
    tone_color,
    vsplit,
)

WIDTH = 1600
HEIGHT = 900
SIZE = (WIDTH, HEIGHT)
M = 16
G = 8

BG = (8, 10, 14)
PANEL = (22, 27, 38)
BORDER = (58, 68, 88)
INK = (255, 255, 255)
MUTED = (186, 196, 214)
CYAN = (110, 231, 255)
AMBER = (251, 191, 36)
GOOD = (74, 222, 128)
BAD = (251, 113, 133)
TRACK = (40, 48, 64)
# Dark empty cells, then amber into gold. Peak stays gold rather than white.
HEAT = [(52, 38, 22), (122, 72, 16), (196, 112, 18), (245, 176, 48), (255, 214, 120)]
MODELS = [CYAN, AMBER, (167, 139, 250), GOOD, (96, 165, 250), BAD]
OUTCOMES = {"kept": GOOD, "regen": CYAN, "edit": (167, 139, 250), "error": BAD}

BODY = 18
TITLE = 18


def _panel(canvas: Canvas, box) -> None:
    canvas.rounded(box, 12, PANEL, outline=BORDER, width=1)


def _rows(canvas, box, rows, title, color, **extra) -> None:
    options = dict(
        title=title,
        title_font=font("semibold", TITLE),
        label_font=font("regular", BODY),
        value_font=font("medium", BODY),
        ink=INK,
        muted=MUTED,
        bar=color,
        track=TRACK,
        bar_height=8,
        min_row=24,
        max_row=32,
        pad=10,
    )
    options.update(extra)
    draw_metric_rows(canvas, box, rows, **options)


def _emoji_columns(canvas: Canvas, box, rows) -> None:
    x0, y0, x1, y1 = [int(v) for v in box]
    if not rows:
        canvas.text(((x0 + x1) / 2, (y0 + y1) / 2), EMPTY, font("regular", BODY), MUTED, anchor="mm")
        return
    face = font("medium", BODY)
    mid = x0 + (x1 - x0) / 2
    columns = (rows[: (len(rows) + 1) // 2], rows[(len(rows) + 1) // 2 :])
    origins = (x0, mid + 6)
    row_h = (y1 - y0) / max(1, len(columns[0]))
    for origin, column in zip(origins, columns):
        for index, row in enumerate(column):
            cy = y0 + (index + 0.5) * row_h
            label = f"{row.label}  {row.all_time:,}"
            canvas.text((origin, cy), label, face, INK, anchor="lm", max_width=int(mid - x0 - 12))


def _image_panel(canvas: Canvas, box, view) -> None:
    _panel(canvas, box)
    x0, y0, x1, y1 = box
    canvas.text((x0 + 12, y0 + 8), "Image generation", font("semibold", TITLE), INK)
    colors = model_colors(len(view.models), MODELS)
    donut_size = 108
    donut_y = y0 + 32
    donut = chart_donut([value for _name, value in view.models], colors, donut_size, PANEL, hole=0.62)
    canvas.paste(donut, (x0 + 8, donut_y))
    draw_legend(
        canvas,
        (x0 + donut_size + 16, donut_y, x1 - 10, donut_y + donut_size),
        [(name, value, colors[index]) for index, (name, value) in enumerate(view.models)],
        font("medium", BODY),
        INK,
        MUTED,
        row_h=donut_size / max(1, len(view.models)),
    )
    lower_top = donut_y + donut_size + 8
    canvas.text((x0 + 12, lower_top + 4), seconds(view.avg_seconds), font("bold", 26), CYAN)
    canvas.text((x0 + 12, lower_top + 34), "avg time", font("regular", BODY), MUTED)
    gauge_size = 84
    gauge = (x1 - 14 - gauge_size, lower_top, x1 - 14, lower_top + gauge_size)
    draw_gauge(canvas, gauge, view.success_rate, TRACK, GOOD, width=11)
    center_x = (gauge[0] + gauge[2]) / 2
    center_y = (gauge[1] + gauge[3]) / 2 - 4
    canvas.text((center_x, center_y), percent(view.success_rate), font("bold", 22), INK, anchor="mm")
    canvas.text((center_x, gauge[3] + 2), "success", font("medium", BODY), MUTED, anchor="mt")
    segments = outcome_segments(view, OUTCOMES)
    bar = (x0 + 12, y1 - 78, x1 - 12, y1 - 52)
    if sum(value for value, _color in segments) > 0:
        stacked = chart_stacked(segments, (max(1, bar[2] - bar[0]), max(1, bar[3] - bar[1])), PANEL)
        canvas.paste(stacked, (bar[0], bar[1]))
    else:
        canvas.text((x0 + 12, y1 - 70), EMPTY, font("regular", BODY), MUTED)
    bits = (
        ("Kept", view.kept, GOOD),
        ("Regen", view.regenerated, CYAN),
        ("Edit", view.edited, (167, 139, 250)),
        ("Err", view.image_errors, BAD),
    )
    cells = hsplit((x0 + 12, y1 - 48, x1 - 12, y1 - 8), [1, 1, 1, 1], 4)
    for cell, (label, value, color) in zip(cells, bits):
        canvas.text((cell[0], cell[1]), f"{value:,}", font("bold", BODY), color, max_width=cell[2] - cell[0])
        canvas.text((cell[0], cell[3]), label, font("medium", BODY), MUTED, anchor="lb", max_width=cell[2] - cell[0])


def _voice_panel(canvas: Canvas, box, view) -> None:
    _panel(canvas, box)
    x0, y0, x1, y1 = box
    canvas.text((x0 + 12, y0 + 8), "Voice", font("semibold", TITLE), INK)
    canvas.text((x0 + 12, y0 + 32), f"{view.voice_minutes:,}", font("bold", 32), INK)
    canvas.text(
        (x0 + 12, y0 + 66),
        f"{view.voice_minutes / 60:.1f} h  ·  {view.voice_all_time:,} all",
        font("medium", BODY),
        MUTED,
        max_width=x1 - x0 - 24,
    )
    if view.voice_channels:
        _rows(
            canvas,
            (x0, y0 + 88, x1, y1 - 70),
            view.voice_channels,
            "Channels",
            CYAN,
            pad=8,
            min_row=20,
            max_row=26,
            show_all_time=False,
        )
    else:
        canvas.text(((x0 + x1) / 2, y0 + 150), EMPTY, font("regular", BODY), MUTED, anchor="mm")
    canvas.text((x0 + 12, y1 - 64), "Hours PT", font("medium", BODY), MUTED)
    draw_hour_bars(canvas, (x0 + 12, y1 - 40, x1 - 12, y1 - 12), view.voice_by_hour, CYAN, TRACK, colors=HEAT)


def render(stats, names, guild_name: str, *, watermark: str | None = None) -> bytes:
    view = prepare(stats, names, guild_name)
    canvas = Canvas(SIZE, BG)
    mark = draw_watermark(canvas, watermark, font("bold", BODY), BG, AMBER, margin=M)
    canvas.text((M, 12), "WEEKLY STATS", font("bold", BODY), AMBER)
    canvas.text((mark[0] - 12, 14), view.week, font("medium", BODY), MUTED, anchor="rt", max_width=max(120, mark[0] - M - 280))
    canvas.text((M, 36), view.guild, font("bold", 26), INK, max_width=WIDTH - 2 * M)

    tiles = hsplit((M, 68, WIDTH - M, 174), [1, 1, 1, 1, 1], G)
    for tile, kpi in zip(tiles, view.kpis):
        _panel(canvas, tile)
        color = tone_color(kpi.tone, GOOD, BAD, MUTED, CYAN)
        canvas.text((tile[0] + 12, tile[1] + 6), kpi.label, font("semibold", BODY), MUTED, max_width=tile[2] - tile[0] - 24)
        canvas.text((tile[0] + 12, tile[1] + 26), f"{kpi.current:,}", font("bold", 40), INK, max_width=tile[2] - tile[0] - 24)
        canvas.text((tile[0] + 12, tile[3] - 8), kpi.delta, font("bold", BODY), color, anchor="lb", max_width=tile[2] - tile[0] - 24)

    row1 = (M, 182, WIDTH - M, 518)
    channels, heat, commands = hsplit(row1, [520, 620, 408], G)
    _panel(canvas, channels)
    _rows(canvas, channels, view.top_channels, "Top channels", CYAN, pad=10, min_row=20, max_row=28)
    _panel(canvas, heat)
    peak = max((value for row in view.heatmap for value in row), default=0)
    legend_left = draw_heat_legend(canvas, (heat[2] - 12, heat[1] + 20), HEAT, peak, font("medium", BODY), MUTED)
    canvas.text(
        (heat[0] + 12, heat[1] + 10),
        "Busiest hours (PT)",
        font("semibold", TITLE),
        INK,
        max_width=max(80, legend_left - heat[0] - 20),
    )
    draw_heatmap(
        canvas,
        (heat[0] + 8, heat[1] + 42, heat[2] - 10, heat[3] - 8),
        view.heatmap,
        HEAT,
        font("medium", BODY),
        MUTED,
        gutter_left=58,
        gutter_bottom=28,
        empty_font=font("regular", BODY),
        empty_fill=MUTED,
    )
    _panel(canvas, commands)
    top_cmd, low_cmd = vsplit(commands, [268, 80], 0)
    _rows(canvas, top_cmd, view.top_commands, "Top commands", AMBER, pad=10, min_row=20, max_row=26)
    _rows(
        canvas,
        low_cmd,
        view.bottom_commands,
        "Least used",
        (167, 139, 250),
        pad=8,
        min_row=20,
        max_row=24,
        show_columns=False,
        bar_scale=row_bar_max(view.top_commands),
    )

    row2 = (M, 526, WIDTH - M, 868)
    cold, reactions, image, voice = hsplit(row2, [410, 500, 328, 310], G)
    _panel(canvas, cold)
    quiet, dead = vsplit(cold, [148, 194], 0)
    _rows(
        canvas,
        quiet,
        view.bottom_channels,
        "Quiet channels",
        (167, 139, 250),
        pad=8,
        min_row=20,
        max_row=26,
        bar_scale=row_bar_max(view.top_channels),
    )
    _rows(canvas, dead, view.dead_channels, "Dead channels", MUTED, pad=8, min_row=20, max_row=26)

    _panel(canvas, reactions)
    react_body, rare_body = vsplit(reactions, [250, 92], 0)
    _rows(canvas, react_body, view.reactions, "Reactions", BAD, pad=6, min_row=20, max_row=22)
    canvas.text((rare_body[0] + 10, rare_body[1] + 2), "Least-used emojis", font("semibold", TITLE), AMBER)
    _emoji_columns(canvas, (rare_body[0] + 10, rare_body[1] + 24, rare_body[2] - 10, rare_body[3] - 4), view.least_emojis)

    _image_panel(canvas, image, view)
    _voice_panel(canvas, voice, view)

    canvas.text((M, HEIGHT - 8), "Aggregate counts only. Message text and prompts are not stored.", font("regular", BODY), MUTED, anchor="lb", max_width=WIDTH - 2 * M)
    return canvas.png()
