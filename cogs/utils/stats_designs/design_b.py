"""Design B. Light SaaS report in the Stripe / Linear style."""

from __future__ import annotations

from cogs.utils.stats_designs._common import (
    EMPTY,
    Canvas,
    chart_area,
    chart_donut,
    draw_day_labels,
    draw_heat_legend,
    draw_heatmap,
    draw_hour_bars,
    draw_kpi,
    draw_legend,
    draw_metric_rows,
    draw_watermark,
    font,
    hsplit,
    kpi_series,
    model_colors,
    percent,
    prepare,
    row_bar_max,
    seconds,
    soft_card,
    sparkline,
    tone_color,
    vsplit,
)

WIDTH = 1440
M = 28
G = 16
HEADER = 108
KPI_H = 168
AREA_H = 236
TABLE_H = 420
MID_H = 300
LOW_H = 268
FOOT = 18
HEIGHT = M + HEADER + G + KPI_H + G + AREA_H + G + TABLE_H + G + MID_H + G + LOW_H + G + FOOT + M
SIZE = (WIDTH, HEIGHT)

BG = (244, 246, 248)
CARD = (255, 255, 255)
INK = (15, 23, 42)
MUTED = (100, 116, 139)
LINE = (226, 232, 240)
ACCENT = (79, 70, 229)
GOOD = (5, 150, 105)
BAD = (225, 29, 72)
TRACK = (241, 245, 249)
# Near-white empty cells, then a light indigo that a single count cannot miss.
HEAT = [(236, 240, 246), (214, 222, 255), (165, 176, 252), (91, 86, 224), (46, 42, 120)]
MODELS = [(79, 70, 229), (14, 165, 233), (16, 185, 129), (245, 158, 11), (244, 63, 94), (139, 92, 246)]
OUTCOMES = {"kept": (5, 150, 105), "regen": (79, 70, 229), "edit": (14, 165, 233), "error": (225, 29, 72)}


def _card(canvas: Canvas, box) -> None:
    soft_card(canvas, box, 16, CARD, shadow=(15, 23, 42, 26), border=LINE)


def _rows(canvas, box, rows, title, **extra) -> None:
    options = dict(
        title=title,
        title_font=font("semibold", 16),
        label_font=font("regular", 14),
        value_font=font("medium", 13),
        ink=INK,
        muted=MUTED,
        bar=ACCENT,
        track=TRACK,
        bar_height=5,
        min_row=22,
        max_row=32,
    )
    options.update(extra)
    draw_metric_rows(canvas, box, rows, **options)


def render(stats, names, guild_name: str, *, watermark: str | None = None) -> bytes:
    view = prepare(stats, names, guild_name)
    canvas = Canvas(SIZE, BG)
    mark = draw_watermark(canvas, watermark, font("semibold", 12), ACCENT, (238, 242, 255), LINE, margin=M)
    canvas.text((M, M + 4), "Weekly report", font("medium", 14), ACCENT)
    canvas.text((mark[0] - 16, M + 6), view.week, font("medium", 15), MUTED, anchor="rt", max_width=max(80, mark[0] - M - 220))
    canvas.text((M, M + 36), view.guild, font("bold", 34), INK, max_width=WIDTH - 2 * M)
    canvas.text((M, M + 78), "Monday–Sunday  ·  Pacific time", font("regular", 14), MUTED)

    y = M + HEADER + G
    tiles = hsplit((M, y, WIDTH - M, y + KPI_H), [1, 1, 1, 1, 1], G)
    for tile, kpi in zip(tiles, view.kpis):
        _card(canvas, tile)
        series = kpi_series(kpi, view.daily, view.voice_daily)
        color = tone_color(kpi.tone, GOOD, BAD, MUTED, ACCENT)
        spark = sparkline(series, (108, 36), color, color)
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
            spark=spark,
        )

    y += KPI_H + G
    area = (M, y, WIDTH - M, y + AREA_H)
    _card(canvas, area)
    total = sum(view.daily)
    canvas.text((area[0] + 18, area[1] + 16), "Daily activity", font("semibold", 16), INK)
    canvas.text((area[2] - 18, area[1] + 18), f"{total:,} events", font("medium", 14), MUTED, anchor="rt")
    if total <= 0:
        canvas.text(((area[0] + area[2]) / 2, (area[1] + area[3]) / 2), EMPTY, font("regular", 15), MUTED, anchor="mm")
    else:
        chart = chart_area(view.daily, (WIDTH - 2 * M - 36, AREA_H - 78), ACCENT, ACCENT, CARD, LINE)
        canvas.paste(chart, (area[0] + 18, area[1] + 46))
        draw_day_labels(canvas, (area[0] + 18, area[3] - 28, area[2] - 18, area[3] - 8), font("medium", 12), MUTED)

    y += AREA_H + G
    channels, commands = hsplit((M, y, WIDTH - M, y + TABLE_H), [1, 1], G)
    _card(canvas, channels)
    _rows(canvas, channels, view.top_channels, "Channels")
    _card(canvas, commands)
    top_cmd, low_cmd = vsplit(commands, [3, 1], 0)
    _rows(canvas, top_cmd, view.top_commands, "Commands", max_row=30)
    _rows(
        canvas,
        low_cmd,
        view.bottom_commands,
        "Least used",
        pad=10,
        min_row=20,
        max_row=28,
        bar_scale=row_bar_max(view.top_commands),
    )

    y += TABLE_H + G
    heat, image, voice = hsplit((M, y, WIDTH - M, y + MID_H), [1.25, 1, 1], G)
    _card(canvas, heat)
    peak = max((value for row in view.heatmap for value in row), default=0)
    legend_left = draw_heat_legend(canvas, (heat[2] - 16, heat[1] + 24), HEAT, peak, font("medium", 12), MUTED)
    canvas.text(
        (heat[0] + 16, heat[1] + 14),
        "Busiest hours  ·  PT",
        font("semibold", 16),
        INK,
        max_width=max(80, legend_left - heat[0] - 28),
    )
    draw_heatmap(
        canvas,
        (heat[0] + 12, heat[1] + 46, heat[2] - 12, heat[3] - 12),
        view.heatmap,
        HEAT,
        font("medium", 12),
        MUTED,
        gutter_left=46,
        gutter_bottom=22,
        empty_font=font("regular", 14),
        empty_fill=MUTED,
    )

    _card(canvas, image)
    canvas.text((image[0] + 16, image[1] + 14), "Image generation", font("semibold", 16), INK)
    colors = model_colors(len(view.models), MODELS)
    donut = chart_donut([value for _name, value in view.models], colors, 132, CARD, hole=0.66)
    canvas.paste(donut, (image[0] + 8, image[1] + 48))
    draw_legend(
        canvas,
        (image[0] + 148, image[1] + 52, image[2] - 14, image[1] + 176),
        [(name, value, colors[index]) for index, (name, value) in enumerate(view.models)],
        font("regular", 13),
        INK,
        MUTED,
    )
    canvas.text((image[0] + 16, image[3] - 78), percent(view.success_rate), font("bold", 22), INK)
    canvas.text((image[0] + 16, image[3] - 52), "success", font("regular", 12), MUTED)
    canvas.text((image[0] + 120, image[3] - 78), seconds(view.avg_seconds), font("bold", 22), ACCENT)
    canvas.text((image[0] + 120, image[3] - 52), "avg", font("regular", 12), MUTED)
    bits = (("Kept", view.kept), ("Regen", view.regenerated), ("Edit", view.edited))
    bx = image[0] + 230
    for label, value in bits:
        canvas.text((bx, image[3] - 78), f"{value:,}", font("semibold", 16), INK)
        canvas.text((bx, image[3] - 56), label, font("regular", 12), MUTED)
        bx += 70

    _card(canvas, voice)
    canvas.text((voice[0] + 16, voice[1] + 14), "Voice", font("semibold", 16), INK)
    canvas.text((voice[0] + 16, voice[1] + 42), f"{view.voice_minutes:,}", font("bold", 28), INK)
    canvas.text((voice[0] + 16 + 90, voice[1] + 54), "min", font("medium", 14), MUTED)
    canvas.text(
        (voice[2] - 16, voice[1] + 50),
        f"{view.voice_minutes / 60:.1f} h",
        font("medium", 14),
        MUTED,
        anchor="rt",
    )
    if view.voice_channels:
        _rows(canvas, (voice[0], voice[1] + 78, voice[2], voice[3] - 62), view.voice_channels, "Channels", pad=12, min_row=20)
    else:
        canvas.text(((voice[0] + voice[2]) / 2, voice[1] + 130), EMPTY, font("regular", 14), MUTED, anchor="mm")
    draw_hour_bars(
        canvas,
        (voice[0] + 16, voice[3] - 46, voice[2] - 16, voice[3] - 16),
        view.voice_by_hour,
        ACCENT,
        TRACK,
        colors=HEAT,
    )

    y += MID_H + G
    quiet, dead, reactions, rare = hsplit((M, y, WIDTH - M, y + LOW_H), [1, 1, 1.1, 1], G)
    scales = {
        "Quiet channels": row_bar_max(view.top_channels),
        "Least-used emojis": row_bar_max(view.reactions, "all_time"),
    }
    for box, rows, title in (
        (quiet, view.bottom_channels, "Quiet channels"),
        (dead, view.dead_channels, "Dead channels"),
        (reactions, view.reactions, "Reactions"),
        (rare, view.least_emojis, "Least-used emojis"),
    ):
        _card(canvas, box)
        extra = {}
        if title.startswith("Least"):
            extra["metric"] = "all_time"
        if title in scales:
            extra["bar_scale"] = scales[title]
        _rows(canvas, box, rows, title, **extra)

    canvas.text((M, HEIGHT - M), "Aggregate counts only. Message text and prompts are not stored.", font("regular", 12), MUTED, anchor="lb")
    return canvas.png()
