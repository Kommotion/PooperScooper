"""Design C. Bold esports board: neon, cut corners, gauge, stacked outcomes."""

from __future__ import annotations

from cogs.utils.stats_designs._common import (
    EMPTY,
    Canvas,
    chart_donut,
    chart_stacked,
    cut_panel,
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

WIDTH = 1440
M = 22
G = 12
HEADER = 124
KPI_H = 136
MAIN_H = 440
ROW_H = 360
ROW2_H = 300
FOOT = 18
HEIGHT = M + HEADER + G + KPI_H + G + MAIN_H + G + ROW_H + G + ROW2_H + G + FOOT + M
SIZE = (WIDTH, HEIGHT)

BG = (7, 6, 12)
PANEL = (16, 12, 24)
INK = (245, 243, 255)
MUTED = (168, 160, 196)
MAGENTA = (255, 43, 214)
CYAN = (0, 240, 255)
LIME = (198, 255, 58)
BAD = (255, 77, 109)
AMBER = (255, 196, 61)
TRACK = (38, 28, 52)
# Near-black empty cells, then magenta into hot pink. The last stop stays pink so peaks do not blow out white.
HEAT = [(34, 16, 40), (112, 18, 92), (196, 28, 150), (255, 48, 176), (255, 96, 188)]
MODELS = [MAGENTA, CYAN, LIME, AMBER, (124, 92, 255), BAD]
OUTCOMES = {"kept": LIME, "regen": MAGENTA, "edit": CYAN, "error": BAD}


def _panel(canvas: Canvas, box, outline=MAGENTA) -> None:
    cut_panel(canvas, box, PANEL, outline, cut=16)


def _rows(canvas, box, rows, title, color, **extra) -> None:
    options = dict(
        title=title,
        title_font=font("bold", 14),
        label_font=font("medium", 14),
        value_font=font("semibold", 13),
        ink=INK,
        muted=MUTED,
        bar=color,
        track=TRACK,
        title_fill=color,
        bar_height=9,
        min_row=22,
        max_row=32,
    )
    options.update(extra)
    draw_metric_rows(canvas, box, rows, **options)


def render(stats, names, guild_name: str, *, watermark: str | None = None) -> bytes:
    view = prepare(stats, names, guild_name)
    canvas = Canvas(SIZE, BG)
    canvas.draw.polygon([(0, 0), (WIDTH, 0), (WIDTH, 86), (0, 118)], fill=(18, 8, 28))
    canvas.draw.line([(0, 118), (WIDTH, 86)], fill=MAGENTA, width=3)
    canvas.draw.line([(0, 122), (WIDTH, 90)], fill=CYAN, width=1)
    mark = draw_watermark(canvas, watermark, font("bold", 12), BG, LIME, margin=M)
    canvas.text((M, 18), "WEEKLY  //  LIVE", font("bold", 13), MAGENTA)
    canvas.text((mark[0] - 14, 18), view.week, font("semibold", 14), INK, anchor="rt", max_width=max(80, mark[0] - M - 240))
    canvas.text((M, 44), view.guild, font("bold", 36), INK, max_width=WIDTH - 2 * M)

    y = HEADER + G
    tiles = hsplit((M, y, WIDTH - M, y + KPI_H), [1, 1, 1, 1, 1], G)
    for tile, kpi, outline in zip(tiles, view.kpis, (CYAN, MAGENTA, LIME, AMBER, CYAN)):
        _panel(canvas, tile, outline)
        color = tone_color(kpi.tone, LIME, BAD, MUTED, CYAN)
        canvas.text((tile[0] + 16, tile[1] + 14), kpi.label.upper(), font("bold", 12), outline, max_width=tile[2] - tile[0] - 28)
        canvas.text((tile[0] + 16, tile[1] + 40), f"{kpi.current:,}", font("bold", 36), INK, max_width=tile[2] - tile[0] - 28)
        canvas.text((tile[0] + 16, tile[3] - 16), kpi.delta, font("bold", 16), color, anchor="lb", max_width=tile[2] - tile[0] - 28)

    y += KPI_H + G
    heat, side = hsplit((M, y, WIDTH - M, y + MAIN_H), [1.35, 1], G)
    _panel(canvas, heat, CYAN)
    peak = max((value for row in view.heatmap for value in row), default=0)
    legend_left = draw_heat_legend(canvas, (heat[2] - 18, heat[1] + 24), HEAT, peak, font("bold", 12), MUTED)
    canvas.text(
        (heat[0] + 18, heat[1] + 14),
        "BUSIEST HOURS  ·  PT",
        font("bold", 15),
        CYAN,
        max_width=max(80, legend_left - heat[0] - 30),
    )
    draw_heatmap(
        canvas,
        (heat[0] + 14, heat[1] + 46, heat[2] - 14, heat[3] - 14),
        view.heatmap,
        HEAT,
        font("bold", 12),
        MUTED,
        glow=3,
        gutter_left=46,
        gutter_bottom=22,
        empty_font=font("bold", 14),
        empty_fill=MUTED,
    )

    _panel(canvas, side, MAGENTA)
    canvas.text((side[0] + 18, side[1] + 14), "IMAGEGEN", font("bold", 15), MAGENTA)
    gauge = (side[0] + 36, side[1] + 48, side[0] + 196, side[1] + 208)
    draw_gauge(canvas, gauge, view.success_rate, TRACK, LIME, width=16)
    canvas.text(((gauge[0] + gauge[2]) / 2, (gauge[1] + gauge[3]) / 2 - 6), percent(view.success_rate), font("bold", 28), INK, anchor="mm")
    canvas.text(((gauge[0] + gauge[2]) / 2, (gauge[1] + gauge[3]) / 2 + 22), "SUCCESS", font("bold", 11), MUTED, anchor="mm")
    canvas.text((side[0] + 214, side[1] + 70), seconds(view.avg_seconds), font("bold", 28), CYAN)
    canvas.text((side[0] + 214, side[1] + 104), "AVG TIME", font("bold", 12), MUTED)
    canvas.text((side[0] + 214, side[1] + 136), f"{view.image_runs:,} runs", font("semibold", 14), INK)
    segments = outcome_segments(view, OUTCOMES)
    bar = (side[0] + 18, side[1] + 214, side[2] - 18, side[1] + 258)
    if sum(value for value, _color in segments) <= 0:
        canvas.text((side[0] + 18, side[1] + 228), EMPTY, font("medium", 14), MUTED)
    else:
        image = chart_stacked(segments, (bar[2] - bar[0], bar[3] - bar[1]), PANEL)
        canvas.paste(image, (bar[0], bar[1]))
    labels = (("KEPT", view.kept, LIME), ("REGEN", view.regenerated, MAGENTA), ("EDIT", view.edited, CYAN), ("ERR", view.image_errors, BAD))
    cells = hsplit((side[0] + 16, side[1] + 264, side[2] - 16, side[1] + 312), [1, 1, 1, 1], 6)
    for cell, (label, value, color) in zip(cells, labels):
        canvas.text((cell[0], cell[1]), label, font("bold", 11), color, max_width=cell[2] - cell[0])
        canvas.text((cell[0], cell[1] + 16), f"{value:,}", font("bold", 16), INK, max_width=cell[2] - cell[0])
    colors = model_colors(len(view.models), MODELS)
    donut = chart_donut([value for _name, value in view.models], colors, 78, PANEL, hole=0.62)
    canvas.paste(donut, (side[0] + 16, side[3] - 96))
    draw_legend(
        canvas,
        (side[0] + 104, side[3] - 100, side[2] - 14, side[3] - 12),
        [(name, value, colors[index]) for index, (name, value) in enumerate(view.models)],
        font("medium", 12),
        INK,
        MUTED,
    )

    y += MAIN_H + G
    channels, commands = hsplit((M, y, WIDTH - M, y + ROW_H), [1, 1], G)
    _panel(canvas, channels, CYAN)
    _rows(canvas, channels, view.top_channels, "TOP CHANNELS", CYAN)
    _panel(canvas, commands, MAGENTA)
    top_cmd, low_cmd = vsplit(commands, [3, 1], 0)
    _rows(canvas, top_cmd, view.top_commands, "TOP COMMANDS", MAGENTA, max_row=30)
    _rows(
        canvas,
        low_cmd,
        view.bottom_commands,
        "BOTTOM COMMANDS",
        AMBER,
        pad=8,
        min_row=20,
        bar_scale=row_bar_max(view.top_commands),
    )

    y += ROW_H + G
    reactions, rare, voice, cold = hsplit((M, y, WIDTH - M, y + ROW2_H), [1.1, 1, 1.15, 1.15], G)
    _panel(canvas, reactions, (251, 113, 133))
    _rows(canvas, reactions, view.reactions, "REACTIONS", (251, 113, 133))
    _panel(canvas, rare, AMBER)
    _rows(
        canvas,
        rare,
        view.least_emojis,
        "RARE EMOJIS",
        AMBER,
        metric="all_time",
        bar_scale=row_bar_max(view.reactions, "all_time"),
    )
    _panel(canvas, voice, LIME)
    canvas.text((voice[0] + 16, voice[1] + 12), "VOICE", font("bold", 14), LIME)
    canvas.text((voice[0] + 16, voice[1] + 34), f"{view.voice_minutes:,}", font("bold", 26), INK)
    canvas.text((voice[2] - 14, voice[1] + 46), f"{view.voice_minutes / 60:.1f} H", font("bold", 13), CYAN, anchor="rt")
    if view.voice_channels:
        _rows(canvas, (voice[0], voice[1] + 64, voice[2], voice[3] - 58), view.voice_channels, "CHANNELS", LIME, pad=10, min_row=20)
    else:
        canvas.text(((voice[0] + voice[2]) / 2, voice[1] + 120), EMPTY, font("medium", 14), MUTED, anchor="mm")
    draw_hour_bars(
        canvas,
        (voice[0] + 14, voice[3] - 42, voice[2] - 14, voice[3] - 14),
        view.voice_by_hour,
        LIME,
        TRACK,
        colors=HEAT,
    )
    _panel(canvas, cold, MUTED)
    quiet, dead = vsplit(cold, [1, 1.3], 0)
    _rows(
        canvas,
        quiet,
        view.bottom_channels,
        "QUIET",
        CYAN,
        pad=10,
        min_row=18,
        max_row=26,
        bar_scale=row_bar_max(view.top_channels),
    )
    _rows(canvas, dead, view.dead_channels, "DEAD", MUTED, pad=10, min_row=18, max_row=24)

    canvas.text((M, HEIGHT - M), "AGGREGATE ONLY  ·  NO MESSAGE TEXT  ·  NO PROMPTS", font("bold", 11), MUTED, anchor="lb")
    return canvas.png()
