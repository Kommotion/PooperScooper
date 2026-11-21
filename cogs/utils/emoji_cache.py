"""Disk cache of Discord custom emoji images.

Render reads PNG files already on disk. The live card prefetches missing ids
before that, through the public emoji CDN only. No bot token is used.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image

from cogs.utils.constants import DATA_DIR

log = logging.getLogger(__name__)

_EXTENSIONS = ("png", "gif", "webp")
_CONCURRENCY = 4
_CACHE_DIR: Path | None = None


def set_cache_dir(path: Path | str | None) -> None:
    """Point the cache at ``path``. ``None`` restores ``DATA_DIR / emoji_cache``."""
    global _CACHE_DIR
    _CACHE_DIR = None if path is None else Path(path)


def cache_dir() -> Path:
    if _CACHE_DIR is not None:
        return _CACHE_DIR
    return DATA_DIR / "emoji_cache"


def _emoji_id(emoji_id) -> str | None:
    text = str(emoji_id or "").strip()
    if text.isdigit():
        return text
    return None


def _png_path(emoji_id: str) -> Path:
    return cache_dir() / f"{emoji_id}.png"


def cdn_url(emoji_id: str, extension: str) -> str:
    return f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}?size=64"


def load_image(emoji_id) -> Image.Image | None:
    """Return a copy of the cached PNG, or None. Never touches the network."""
    key = _emoji_id(emoji_id)
    if key is None:
        return None
    path = _png_path(key)
    if not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            image.load()
            return image.convert("RGBA").copy()
    except Exception:
        log.warning("emoji cache could not be read for %s", key)
        return None


def _http_get(url: str, timeout: float) -> tuple[int, bytes] | None:
    """GET one CDN url. Returns None on timeout, HTTP error, or any other failure."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "PooperScooper"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            return status, response.read()
    except Exception:
        return None


def _decode_image(payload: bytes) -> Image.Image | None:
    try:
        with Image.open(BytesIO(payload)) as image:
            image.seek(0)
            frame = image.convert("RGBA")
            frame.load()
            return frame.copy()
    except Exception:
        return None


def _write_png(path: Path, image: Image.Image) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    try:
        image.save(temporary, format="PNG")
        temporary.replace(path)
        return True
    except Exception:
        log.warning("emoji cache could not be written for %s", path.stem)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def fetch_to_cache(emoji_id, timeout: float = 5) -> Image.Image | None:
    """Download one emoji and store ``{id}.png``. None on rejection or failure.

    PNG, GIF, then WebP. A GIF keeps its first frame. Nothing is left behind
    when the download or decode fails.
    """
    key = _emoji_id(emoji_id)
    if key is None:
        return None
    destination = _png_path(key)
    if destination.is_file():
        return load_image(key)
    deadline = time.monotonic() + max(0.1, float(timeout))
    image = None
    for extension in _EXTENSIONS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            fetched = _http_get(cdn_url(key, extension), remaining)
        except Exception:
            continue
        if not fetched:
            continue
        status, payload = fetched
        if status != 200 or not payload:
            continue
        image = _decode_image(payload)
        if image is not None:
            break
    if image is None:
        return None
    if not _write_png(destination, image):
        return None
    return image


def _cached(emoji_id: str) -> bool:
    return _png_path(emoji_id).is_file()


async def prefetch(ids, timeout: float = 5) -> None:
    """Fetch custom emoji ids that are not already cached. Never raises."""
    pending: list[str] = []
    seen: set[str] = set()
    try:
        source = list(ids or [])
    except Exception:
        log.warning("emoji prefetch was given ids that could not be read")
        return
    for raw in source:
        key = _emoji_id(raw)
        if key is None or key in seen:
            continue
        seen.add(key)
        try:
            if _cached(key):
                continue
        except Exception:
            log.warning("emoji cache lookup failed for %s", key)
            continue
        pending.append(key)
    if not pending:
        return
    limit = asyncio.Semaphore(_CONCURRENCY)

    async def one(key: str) -> None:
        async with limit:
            try:
                await asyncio.to_thread(fetch_to_cache, key, timeout)
            except Exception:
                log.warning("emoji prefetch failed for %s", key)

    try:
        await asyncio.gather(*(one(key) for key in pending))
    except Exception:
        log.warning("emoji prefetch failed")
