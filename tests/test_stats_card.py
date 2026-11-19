"""The weekly stats card is a PNG for both an empty week and a full one."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import cogs.utils.stats_card as stats_card
from cogs.utils.stats_card import (
    BODY_FONT_SIZE,
    CARD_WIDTH,
    EMOJI_FONT_SIZE,
    MARGIN,
    PANEL_PAD,
    CardNames,
    _sections,
    render_stats_card,
)
from cogs.utils.stats_schedule import LA
from cogs.utils.stats_store import (
    ChannelCount,
    CommandCount,
    DimCount,
    ReactionCount,
    StatsStore,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _open_png(payload: bytes) -> Image.Image:
    assert payload.startswith(PNG_MAGIC)
    image = Image.open(BytesIO(payload))
    image.load()
    assert image.size[0] == CARD_WIDTH
    assert image.size[1] > 0
    assert image.mode == "RGB"
    return image


def test_empty_card_is_a_png(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        stats = store.build_stats(1, datetime(2026, 11, 18, 12, tzinfo=LA))
        payload = render_stats_card(stats, None, "Menaces to Sobriety")
    finally:
        store.close()
    _open_png(payload)


def test_full_card_is_a_png(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        when = datetime(2026, 11, 18, 15, 5, tzinfo=LA)
        store.record_message(1, 10, 7, when)
        store.record_message(1, 11, 8, when)
        store.record_reaction(1, 7, "111", name="spark", is_custom=True, when=when)
        store.record_reaction(1, 7, "😂", name="😂", is_custom=False, when=when)
        store.record_command(1, "imagegen", "slash")
        store.record_command(1, "statscard", "prefix")
        store.record_imagegen(
            1,
            model="krea2_turbo",
            loras=("detail",),
            edit_mode="img2img",
            width=1024,
            height=1024,
            steps=8,
            sampler="euler",
            seconds=4.5,
            success=True,
        )
        store.record_imagegen_followup(1, "regenerate")
        store.record_voice(1, 50, 7, when.replace(hour=10), when.replace(hour=11, minute=30))
        stats = store.build_stats(1, when, known_custom_emojis=[("222", "quiet")])
        names = {
            "channels": {10: "general", 11: "quiet-room", 50: "voice-lounge"},
            "users": {7: "Ada"},
            "emojis": {"111": "spark"},
        }
        payload = render_stats_card(stats, names, "Menaces to Sobriety")
        card_names = CardNames(names["channels"], names["users"], names["emojis"])
        named = render_stats_card(stats, card_names, "Menaces to Sobriety")
        empty = render_stats_card(
            store.build_stats(99, when),
            {},
            "Empty guild",
        )
    finally:
        store.close()

    _open_png(payload)
    _open_png(named)
    _open_png(empty)
    assert payload != empty
    assert named == payload
    sections = dict(_sections(stats, card_names))
    assert "Leaderboard" not in sections
    rendered = "\n".join(line for lines in sections.values() for line in lines)
    assert "Ada" not in rendered


def test_card_sections_omit_member_names(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        when = datetime(2026, 11, 18, 15, 5, tzinfo=LA)
        for user_id in range(1, 13):
            store.record_message(1, 10, user_id, when)
        stats = store.build_stats(1, when)
        names = CardNames(
            {},
            {user_id: f"Member {user_id}" for user_id in range(3, 13)},
            {},
        )
        sections = dict(_sections(stats, names))
    finally:
        store.close()

    assert "Leaderboard" not in sections
    rendered = "\n".join(line for lines in sections.values() for line in lines)
    assert "Member" not in rendered
    assert "<@" not in rendered


def _sample_names() -> CardNames:
    return CardNames(
        {10: "general", 50: "lounge"},
        {7: "Ada"},
        {"111": "spark", "222": "quiet", "😂": "😂"},
    )


def _sample_stats(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        stats = store.build_stats(1, datetime(2026, 11, 18, 12, tzinfo=LA))
    finally:
        store.close()
    image = replace(
        stats.imagegen,
        avg_seconds=4.5,
        success_rate=0.83,
        error_rate=0.17,
        success_count=2000,
        error_count=400,
        regenerate_count=1200,
        edit_count=300,
        kept_first=1800,
        generation_count=2400,
        models=(DimCount("krea", 2400, 9000),),
    )
    return replace(
        stats,
        messages_weekly=1523,
        reactions_weekly=69,
        commands_weekly=1200,
        voice_minutes=491,
        voice_minutes_all_time=2010,
        active_members=1204,
        active_members_last_week=1124,
        top_channels=(ChannelCount(10, 1523, 9800),),
        top_reactions=(
            ReactionCount("😂", "😂", False, 1500, 3200),
            ReactionCount("111", "spark", True, 8, 40),
        ),
        least_custom_emojis=(ReactionCount("222", "quiet", True, 0, 1500),),
        top_commands=(CommandCount("imagegen", "slash", 1200, 4500),),
        top_voice_channels=(ChannelCount(50, 491, 2010),),
        imagegen=image,
    )


def _saturated_pixels(image: Image.Image) -> int:
    background = (33, 36, 49)
    count = 0
    for pixel in image.getdata():
        if pixel == background:
            continue
        red, green, blue = pixel[:3]
        if max(red, green, blue) - min(red, green, blue) >= 28:
            count += 1
    return count


def test_emoji_fallback_label_uses_unicode_names():
    assert stats_card._emoji_fallback_label("😂") == "face with tears of joy"
    assert stats_card._emoji_fallback_label("👍🏽") == "thumbs up sign"
    assert stats_card._emoji_fallback_label("❤️") == "heavy black heart"
    assert stats_card._emoji_fallback_label("1️⃣") == "digit one"
    assert stats_card._emoji_fallback_label("wave") == "wave"
    assert "😂" not in stats_card._emoji_fallback_label("😂")


def test_counts_and_emoji_fallback_when_no_emoji_font(tmp_path, monkeypatch):
    monkeypatch.setattr(
        stats_card,
        "_EMOJI_FONT_CANDIDATES",
        (Path(r"C:\Windows\Fonts\seguiemj-missing.ttf"), Path("no-emoji-font.ttf")),
    )
    assert stats_card._emoji_font(EMOJI_FONT_SIZE) is None
    stats = _sample_stats(tmp_path)
    names = _sample_names()
    sections = dict(_sections(stats, names))

    assert "Leaderboard" not in sections
    assert "Ada" not in "\n".join(line for lines in sections.values() for line in lines)
    assert sections["Reactions"] == [
        "face with tears of joy  1,500 this week  /  3,200 all time",
        "spark  8 this week  /  40 all time",
    ]
    assert "😂" not in "\n".join(sections["Reactions"])
    assert sections["Voice"] == [
        "491 minutes this week  /  2,010 all time",
        "lounge  491 this week  /  2,010 all time",
    ]
    assert sections["Channels"] == [
        "Busiest",
        "#general  1,523 this week  /  9,800 all time",
    ]
    assert sections["Commands"] == [
        "Most used",
        "/imagegen  1,200 this week  /  4,500 all time",
    ]
    assert sections["Least-used custom emojis"] == ["quiet  1,500 all time"]
    assert sections["Active members"] == [
        "1,204 active members this week. +80 versus last week's 1,124."
    ]
    assert "Success 83%    Errors 17%    Runs 2,400" in sections["Image generation"]
    assert "Kept the first result 1,800    Regenerated 1,200    Edited 300" in sections["Image generation"]
    assert "Models: krea (2,400)" in sections["Image generation"]
    assert stats_card._summary_line(stats) == (
        "1,523 messages   69 reactions   1,200 commands   491 voice min"
    )
    _open_png(render_stats_card(stats, names, "Menaces to Sobriety"))


def test_long_member_name_is_not_drawn_on_the_card(tmp_path):
    stats = _sample_stats(tmp_path)
    names = CardNames({}, {7: "W" * 48}, {})
    sections = dict(_sections(stats, names))
    assert "Leaderboard" not in sections
    rendered = "\n".join(line for lines in sections.values() for line in lines)
    assert "W" * 8 not in rendered


def test_emoji_font_draws_unicode_reactions_and_voice_prefix(tmp_path):
    if stats_card._emoji_font(EMOJI_FONT_SIZE) is None:
        pytest.skip("Segoe UI Emoji is not installed")
    stats = _sample_stats(tmp_path)
    names = _sample_names()
    sections = dict(_sections(stats, names))
    assert sections["Reactions"][0].startswith("😂 ")
    assert sections["Reactions"][1].startswith("spark ")
    assert sections["Voice"][1].startswith("🔊 lounge ")
    assert not sections["Voice"][1].startswith("#")
    payload = render_stats_card(stats, names, "Menaces to Sobriety")
    _open_png(payload)

    image = Image.new("RGB", (420, 36), (33, 36, 49))
    draw = ImageDraw.Draw(image)
    body = stats_card._font(BODY_FONT_SIZE)
    emoji = stats_card._emoji_font(EMOJI_FONT_SIZE)
    stats_card._draw_fitted_line(
        draw,
        4,
        6,
        sections["Reactions"][0],
        body,
        emoji,
        (244, 239, 230),
        400,
    )
    plain = Image.new("RGB", (420, 36), (33, 36, 49))
    ImageDraw.Draw(plain).text((4, 6), sections["Reactions"][0], font=body, fill=(244, 239, 230))
    assert _saturated_pixels(image) > _saturated_pixels(plain)


def test_emoji_draw_falls_back_when_the_emoji_font_fails(tmp_path, monkeypatch):
    real_text = ImageDraw.ImageDraw.text

    def boom(self, xy, text, *args, **kwargs):
        if kwargs.get("embedded_color"):
            raise OSError("emoji glyphs unavailable")
        return real_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", boom)
    stats = _sample_stats(tmp_path)
    _open_png(render_stats_card(stats, _sample_names(), "Menaces to Sobriety"))
