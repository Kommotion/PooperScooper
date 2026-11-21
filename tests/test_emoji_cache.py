"""Custom emoji cache: disk only on render, faked HTTP everywhere else."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

from PIL import Image

import cogs.utils.emoji_cache as emoji_cache
from cogs.utils.emoji_cache import fetch_to_cache, load_image, prefetch, set_cache_dir
from cogs.utils.stats_card import render_stats_card
from cogs.utils.stats_designs._common import fit, font, prepare
from cogs.utils.stats_schedule import LA
from cogs.utils.stats_store import StatsStore

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_bytes(color, size=(16, 8)) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _gif_bytes(first, second) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (10, 6), first).save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGBA", (10, 6), second)],
        loop=0,
        duration=80,
    )
    return buffer.getvalue()


def _use_cache(tmp_path):
    folder = tmp_path / "emoji_cache"
    folder.mkdir()
    set_cache_dir(folder)
    return folder


def _stats(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        when = datetime(2026, 11, 18, 15, tzinfo=LA)
        store.record_reaction(1, 7, "111", name="spark", is_custom=True, when=when)
        store.record_reaction(1, 7, "😂", name="😂", is_custom=False, when=when)
        stats = store.build_stats(1, when, known_custom_emojis=[("222", "quiet")])
    finally:
        store.close()
    return stats


def _red_pixels(payload: bytes) -> int:
    image = Image.open(BytesIO(payload))
    image.load()
    return sum(1 for red, green, blue in image.convert("RGB").getdata() if red > 240 and green < 20 and blue < 20)


def test_png_round_trip_and_load_returns_a_copy(tmp_path, monkeypatch):
    folder = _use_cache(tmp_path)
    calls = []

    def fake(url, timeout):
        calls.append((url, timeout))
        assert url == "https://cdn.discordapp.com/emojis/111.png?size=64"
        return 200, _png_bytes((10, 20, 30, 255))

    monkeypatch.setattr(emoji_cache, "_http_get", fake)
    try:
        saved = fetch_to_cache("111", timeout=5)
        assert saved is not None
        assert saved.getpixel((0, 0))[:3] == (10, 20, 30)
        assert (folder / "111.png").is_file()
        loaded = load_image("111")
        assert loaded is not None
        assert loaded.mode == "RGBA"
        loaded.putpixel((0, 0), (1, 2, 3, 255))
        again = load_image("111")
        assert again is not None
        assert again.getpixel((0, 0))[:3] == (10, 20, 30)
        (folder / "111.png").unlink()
        assert loaded.getpixel((1, 0))[:3] == (10, 20, 30)
        assert calls and calls[0][1] <= 5
    finally:
        set_cache_dir(None)


def test_gif_keeps_the_first_frame(tmp_path, monkeypatch):
    folder = _use_cache(tmp_path)

    def fake(url, timeout):
        if url.endswith(".png?size=64"):
            return 404, b"missing"
        if url.endswith(".gif?size=64"):
            return 200, _gif_bytes((255, 0, 0, 255), (0, 255, 0, 255))
        raise AssertionError(url)

    monkeypatch.setattr(emoji_cache, "_http_get", fake)
    try:
        saved = fetch_to_cache(222)
        assert saved is not None
        assert saved.getpixel((0, 0))[:3] == (255, 0, 0)
        assert (folder / "222.png").read_bytes().startswith(PNG_MAGIC)
        loaded = load_image("222")
        assert loaded is not None
        assert loaded.getpixel((0, 0))[1] < 30
    finally:
        set_cache_dir(None)


def test_failure_timeout_and_bad_id_leave_no_file(tmp_path, monkeypatch):
    folder = _use_cache(tmp_path)
    calls = []

    def fake(url, timeout):
        calls.append(url)
        if "404" in url:
            return 404, b"nope"
        if "timeout" in url or url.endswith("/505.png?size=64"):
            raise TimeoutError("slow")
        return 200, b"not an image"

    monkeypatch.setattr(emoji_cache, "_http_get", fake)
    try:
        assert fetch_to_cache("404") is None
        assert fetch_to_cache("505") is None
        assert fetch_to_cache("nope") is None
        assert fetch_to_cache("12ab") is None
        assert load_image("nope") is None
        assert calls
        assert not any("nope" in url or "12ab" in url for url in calls)
        assert list(folder.iterdir()) == []
    finally:
        set_cache_dir(None)


def test_prefetch_skips_cached_ids_and_rejects_nondigits(tmp_path, monkeypatch):
    folder = _use_cache(tmp_path)
    Image.new("RGBA", (4, 4), (0, 0, 0, 255)).save(folder / "111.png")
    calls = []

    def fake(url, timeout):
        calls.append(url)
        return 200, _png_bytes((9, 9, 9, 255), size=(4, 4))

    monkeypatch.setattr(emoji_cache, "_http_get", fake)

    async def run():
        await prefetch(["111", "111", "nope", "333"], timeout=5)

    try:
        import asyncio

        asyncio.run(run())
        assert calls
        assert all("333" in url for url in calls)
        assert (folder / "333.png").is_file()
        assert load_image("333") is not None
    finally:
        set_cache_dir(None)


def test_long_custom_name_ellipsizes_and_unicode_row_gets_a_short_name(tmp_path):
    long_name = "supercalifragilistic-custom-emoji-name"
    shown = fit(long_name, font("regular", 14), 48)
    assert shown.endswith("…")
    assert shown != long_name
    assert len(shown) < len(long_name)
    view = prepare(_stats(tmp_path), {"channels": {}, "users": {}, "emojis": {}}, "Menaces")
    unicode_rows = [row for row in view.reactions if row.emoji]
    assert unicode_rows[0].emoji == "😂"
    assert unicode_rows[0].label == "face with tears of joy"


def test_render_uses_cache_and_falls_back_without_network(tmp_path, monkeypatch):
    folder = _use_cache(tmp_path)
    stats = _stats(tmp_path)
    names = {"channels": {}, "users": {}, "emojis": {"111": "spark", "222": "quiet"}}
    view = prepare(stats, names, "Menaces")
    custom = [row for row in view.reactions if row.custom_id]
    assert custom and custom[0].custom_id == "111"
    assert custom[0].label == "spark"
    assert custom[0].emoji is None
    unicode_rows = [row for row in view.reactions if row.emoji]
    assert unicode_rows and unicode_rows[0].custom_id is None
    assert unicode_rows[0].label == "face with tears of joy"
    least = view.least_emojis[0]
    assert least.custom_id == "222"
    assert least.label == "quiet"

    def explode(url, timeout):
        raise AssertionError(f"render touched the network: {url}")

    monkeypatch.setattr(emoji_cache, "_http_get", explode)
    try:
        missing = render_stats_card(stats, names, "Menaces")
        assert missing.startswith(PNG_MAGIC)
        Image.open(BytesIO(missing)).load()
        assert _red_pixels(missing) == 0
        Image.new("RGBA", (32, 16), (255, 0, 0, 255)).save(folder / "111.png")
        seeded = render_stats_card(stats, names, "Menaces")
        assert seeded.startswith(PNG_MAGIC)
        Image.open(BytesIO(seeded)).load()
        assert seeded != missing
        assert _red_pixels(seeded) > 0
    finally:
        set_cache_dir(None)
