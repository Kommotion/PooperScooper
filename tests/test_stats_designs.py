"""Dashboard designs render for a full fake week and for an empty one."""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from cogs.utils.stats_card import CARD_WIDTH, DEFAULT_CARD_DESIGN, CardNames, render_stats_card
from cogs.utils.stats_designs import DESIGN_SIZES, render_design
from cogs.utils.stats_designs._common import (
    Canvas,
    Row,
    bar_fill_width,
    draw_heatmap,
    font,
    heat_color,
    heat_geometry,
    metric_peak,
)
from cogs.utils.stats_designs.design_a import WIDTH as DESIGN_A_WIDTH
from cogs.utils.stats_designs.design_a import render_alltime
from cogs.utils.stats_card import render_alltime_card
from scripts.render_stats_designs import GUILD_NAME, build_preview

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class _HiddenNames:
    """Any read of ``users`` fails. Renderers may use channels and emojis only."""

    def __init__(self, channels, emojis):
        self.channels = channels
        self.emojis = emojis

    @property
    def users(self):
        raise AssertionError("renderer read names.users")


class _Guard(dict):
    def get(self, key, default=None):
        if str(key) == "users":
            raise AssertionError("renderer read names.users")
        return dict.get(self, key, default)

    def __getitem__(self, key):
        if str(key) == "users":
            raise AssertionError("renderer read names.users")
        return dict.__getitem__(self, key)


def _open(payload: bytes) -> Image.Image:
    assert payload.startswith(PNG_MAGIC)
    image = Image.open(BytesIO(payload))
    image.load()
    assert image.mode == "RGB"
    assert image.size[0] > 0 and image.size[1] > 0
    return image


def test_default_design_constant_matches_card_width():
    assert DEFAULT_CARD_DESIGN == "A"
    assert CARD_WIDTH == DESIGN_A_WIDTH == DESIGN_SIZES["A"][0]
    assert DESIGN_SIZES["D"] == (1600, 900)


def test_each_design_renders_full_and_empty_pngs(tmp_path_factory):
    stats, names, empty = build_preview(tmp_path_factory.mktemp("designs") / "weekly_stats.sqlite3")
    assert stats.messages_last_week is not None
    assert stats.reactions_last_week is not None
    assert stats.voice_minutes_last_week is not None
    assert stats.imagegen_last_week is not None
    assert stats.active_members_last_week is not None
    assert stats.messages_weekly != stats.messages_last_week
    hidden = _HiddenNames(names.channels, names.emojis)
    guarded = _Guard(channels=names.channels, emojis=names.emojis, users={7: "LeakUser7"})
    for key, size in DESIGN_SIZES.items():
        full = render_design(key, stats, hidden, GUILD_NAME, watermark="FAKE DATA")
        blank = render_design(key, empty, guarded, "Empty guild")
        full_image = _open(full)
        blank_image = _open(blank)
        assert full_image.size == size
        assert blank_image.size == size
        assert len(full) > 8000
        assert len(blank) > 8000
        assert full != blank
        if key == "D":
            assert full_image.size == (1600, 900)
            assert full_image.size[0] > full_image.size[1]
        else:
            assert full_image.size[0] >= 1280
            assert full_image.size[1] >= 1400


def test_renderers_do_not_read_user_names(tmp_path_factory):
    stats, names, empty = build_preview(tmp_path_factory.mktemp("hidden") / "weekly_stats.sqlite3")
    hidden = _HiddenNames(dict(names.channels), dict(names.emojis))
    for key in DESIGN_SIZES:
        render_design(key, stats, hidden, GUILD_NAME)
        render_design(key, empty, hidden, GUILD_NAME)
    assert render_stats_card(empty, hidden, GUILD_NAME) == render_design("A", empty, hidden, GUILD_NAME)


def test_preview_week_follows_a_server_day(tmp_path_factory):
    stats, _names, _empty = build_preview(tmp_path_factory.mktemp("shape") / "weekly_stats.sqlite3")
    heat = stats.heatmap
    quiet = sum(heat[day][hour] for day in range(7) for hour in range(3, 9))
    peak = sum(heat[day][hour] for day in range(7) for hour in range(20, 23))
    assert peak > quiet * 3
    columns = [tuple(heat[day][hour] for day in range(7)) for hour in range(24)]
    assert len(set(columns)) >= 20
    weekend_afternoon = sum(heat[day][hour] for day in (5, 6) for hour in range(12, 18))
    weekday_afternoon = sum(heat[day][hour] for day in range(5) for hour in range(12, 18))
    assert weekend_afternoon / 2 > weekday_afternoon / 5
    late_weekend = sum(heat[day][hour] for day in (4, 5) for hour in (21, 22, 23))
    late_week = sum(heat[day][hour] for day in range(4) for hour in (21, 22, 23))
    assert late_weekend / 2 > late_week / 4

    voice = stats.voice_heatmap
    evening = sum(voice[day][hour] for day in range(7) for hour in (19, 20, 21, 22, 23, 0))
    morning = sum(voice[day][hour] for day in range(7) for hour in range(3, 12))
    afternoon = sum(voice[day][hour] for day in (5, 6) for hour in range(13, 18))
    assert evening > max(1, morning) * 3
    assert afternoon > 0

    image = stats.imagegen
    assert image.avg_seconds is not None and 25 <= image.avg_seconds <= 30
    assert image.success_rate is not None and 0.92 <= image.success_rate <= 0.96
    assert "krea2_identity_edit" in {row.key for row in image.models}
    assert any(row.weekly > 0 for row in image.resolutions)
    assert any(row.weekly > 0 for row in image.steps)


def test_heatmap_cells_stay_discrete_and_zero_is_darker():
    stops = [(20, 28, 48), (16, 90, 110), (20, 170, 160), (80, 220, 190), (190, 240, 100)]
    assert heat_color(0, 40, stops) == stops[0]
    assert heat_color(1, 40, stops) != stops[0]
    grid = [[0] * 24 for _ in range(7)]
    grid[0][2] = 1
    grid[0][4] = 40
    grid[3][8] = 12
    box = (8, 8, 892, 312)
    canvas = Canvas((900, 320), (9, 11, 16))
    draw_heatmap(canvas, box, grid, stops, font("medium", 14), (160, 168, 184), gutter_left=52, gutter_bottom=28, gap=3)
    origin_x, origin_y, cell_w, cell_h, gap = heat_geometry(box, gutter_left=52, gutter_bottom=28, gap=3)

    def pixel(day: int, hour: int):
        left = origin_x + hour * (cell_w + gap)
        top = origin_y + day * (cell_h + gap)
        return canvas.image.getpixel((left + cell_w // 2, top + cell_h // 2))[:3]

    assert pixel(0, 0) != pixel(0, 2)
    assert pixel(0, 2) != pixel(0, 4)
    gap_x = origin_x + cell_w + gap // 2
    gap_y = origin_y + cell_h // 2
    assert canvas.image.getpixel((gap_x, gap_y))[:3] == (9, 11, 16)


def test_short_lists_share_the_long_list_scale():
    top = [Row("/imagegen", 92, 140), Row("/play", 71, 137)]
    bottom = [Row("!sync", 1, 4), Row("!statscard", 2, 8)]
    peak = metric_peak(bottom, "weekly", metric_peak(top, "weekly"))
    assert peak == 92
    assert bar_fill_width(2, peak, 200) < 20
    assert bar_fill_width(92, peak, 200) == 200
    reactions = [Row("joy", 180, 440)]
    rare = [Row(":kekw:", 14, 62)]
    emoji_peak = metric_peak(rare, "all_time", metric_peak(reactions, "all_time"))
    assert emoji_peak == 440
    assert bar_fill_width(62, emoji_peak, 200) < 40


def test_weekly_and_alltime_cards_are_distinct_pngs(tmp_path_factory):
    stats, names, empty = build_preview(tmp_path_factory.mktemp("split") / "weekly_stats.sqlite3")
    hidden = _HiddenNames(names.channels, names.emojis)
    weekly = _open(render_stats_card(stats, hidden, GUILD_NAME))
    same = _open(render_design("A", stats, hidden, GUILD_NAME))
    assert list(weekly.getdata()) == list(same.getdata())
    alltime = _open(render_alltime_card(stats, hidden, GUILD_NAME))
    blank = _open(render_alltime(empty, hidden, "Empty guild"))
    assert weekly.size == alltime.size == blank.size
    assert list(weekly.getdata()) != list(alltime.getdata())
    # Header rule: indigo border on the weekly card, gold on the all-time card.
    assert weekly.getpixel((40, 114))[:3] != alltime.getpixel((40, 114))[:3]
    assert alltime.getpixel((40, 114))[0] > alltime.getpixel((40, 114))[2]
    render_alltime(empty, _Guard(channels=names.channels, emojis=names.emojis, users={7: "LeakUser7"}), "Empty guild")


def test_card_names_mapping_is_not_required_for_users(tmp_path_factory):
    _stats, names, empty = build_preview(tmp_path_factory.mktemp("guard") / "weekly_stats.sqlite3")
    guarded = _Guard({"channels": names.channels, "emojis": names.emojis, "users": {7: "LeakUser7"}})
    for key in DESIGN_SIZES:
        _open(render_design(key, empty, guarded, "Empty guild"))
        _open(render_design(key, empty, CardNames(names.channels, {7: "LeakUser7"}, names.emojis), "Empty guild"))
