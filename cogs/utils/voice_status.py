"""Voice-channel "now playing" status that never overwrites someone else.

discord.py 2.7.1 does not store or parse voice channel status. The text is
read from gateway dispatches and written with ``VoiceChannel.edit(status=...)``.
The exact text this bot last wrote is saved under ``data/`` so a restart can
still recognize it and clear or replace it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import discord

from cogs.utils.constants import DATA_DIR

log = logging.getLogger(__name__)

VOICE_STATUS_LIMIT = 500
CHANNEL_INFO_TIMEOUT_SECONDS = 3.0
REQUEST_CHANNEL_STATUSES = 43
VOICE_CHANNEL_STATUS_UPDATE = "VOICE_CHANNEL_STATUS_UPDATE"
CHANNEL_INFO = "CHANNEL_INFO"
DEFAULT_VOICE_STATUS_PATH = DATA_DIR / "voice_status.json"

_MISSING = object()


def format_now_playing(track: object) -> str:
    """'🎵 {title} — {author}', without the author when the track has none.

    The result is at most 500 characters, which is Discord's voice-status limit.
    """
    title = getattr(track, "title", None)
    author = getattr(track, "author", None)
    title_text = "" if title is None else str(title)
    author_text = "" if author is None else str(author)
    note = "\N{MUSICAL NOTE}"
    if author_text:
        text = f"{note} {title_text} \N{EM DASH} {author_text}"
    else:
        text = f"{note} {title_text}"
    return text[:VOICE_STATUS_LIMIT]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _empty(status: str | None) -> bool:
    return status is None or status == ""


def _normalize_status(raw: Any) -> str | None:
    """Store ``None`` for a missing or blank status. Other text is kept as-is."""
    if raw is None:
        return None
    text = raw if isinstance(raw, str) else str(raw)
    if text == "":
        return None
    return text


class VoiceStatusManager:
    """Remember which voice statuses this bot set, and refuse to clobber the rest.

    ``known_status`` only contains channels whose text was observed via
    ``CHANNEL_INFO`` or ``VOICE_CHANNEL_STATUS_UPDATE``. ``None`` and ``""``
    both mean the channel has no status. ``bot_text`` is the exact string last
    written by the bot, and the same map is stored in ``path`` keyed by channel
    id. A channel id lands in ``permission_errors_logged`` after the first
    permission or edit failure for that channel.

    ``is_playing`` reports whether a channel currently has a track up. Startup
    cleanup leaves those channels alone.
    """

    def __init__(
        self,
        bot,
        *,
        info_timeout: float = CHANNEL_INFO_TIMEOUT_SECONDS,
        path: str | os.PathLike | None = None,
        is_playing: Callable[[int], bool] | None = None,
    ) -> None:
        self.bot = bot
        self.info_timeout = info_timeout
        self.path = Path(path) if path is not None else DEFAULT_VOICE_STATUS_PATH
        self.is_playing = is_playing
        self.known_status: dict[int, Optional[str]] = {}
        self.bot_text: dict[int, str] = {}
        self.permission_errors_logged: set[int] = set()
        self._info_events: dict[int, asyncio.Event] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._epoch: dict[int, int] = {}
        self._installed = False
        self._previous_parsers: dict[str, Any] = {}
        self._handlers: dict[str, Any] = {}
        self._load()

    def install(self) -> None:
        """Register gateway parsers. Unknown events are otherwise dropped."""
        parsers = self._parsers()
        if parsers is None or self._installed:
            return
        self._previous_parsers = {
            VOICE_CHANNEL_STATUS_UPDATE: parsers.get(VOICE_CHANNEL_STATUS_UPDATE),
            CHANNEL_INFO: parsers.get(CHANNEL_INFO),
        }
        # Bound methods are a new object on every lookup. Keep the instances
        # we stored so unload can tell they are still ours.
        self._handlers = {
            VOICE_CHANNEL_STATUS_UPDATE: self._on_voice_channel_status_update,
            CHANNEL_INFO: self._on_channel_info,
        }
        parsers.update(self._handlers)
        self._installed = True

    def uninstall(self) -> None:
        """Drop the parsers this manager installed."""
        parsers = self._parsers()
        installed = self._installed
        self._installed = False
        if parsers is None or not installed:
            return
        for name, previous in self._previous_parsers.items():
            if parsers.get(name) != self._handlers.get(name):
                continue
            if previous is None:
                parsers.pop(name, None)
            else:
                parsers[name] = previous
        self._previous_parsers = {}

    async def ensure_known(self, channel) -> None:
        """Fill ``known_status`` for ``channel`` when Discord answers within a few seconds.

        A missing answer leaves the channel unknown. Callers must not write then.
        """
        channel_id = _as_int(getattr(channel, "id", None))
        if channel_id is None or channel_id in self.known_status:
            return
        guild = getattr(channel, "guild", None)
        guild_id = _as_int(getattr(guild, "id", None))
        if guild_id is None:
            return

        event = self._info_events.get(guild_id)
        start_request = event is None or event.is_set()
        if start_request:
            event = asyncio.Event()
            self._info_events[guild_id] = event
            sent = await self._request_channel_statuses(guild)
            if channel_id in self.known_status:
                return
            if not sent:
                event.set()
                return

        try:
            await asyncio.wait_for(event.wait(), timeout=self.info_timeout)
        except asyncio.TimeoutError:
            return

    def begin_track(self, channel) -> int:
        """Start a now-playing generation and return the token for this track.

        Older tasks for the channel, and any task still running after ``clear``,
        hold a smaller token and will not write.
        """
        channel_id = _as_int(getattr(channel, "id", None))
        if channel_id is None:
            return 0
        generation = self._epoch.get(channel_id, 0) + 1
        self._epoch[channel_id] = generation
        return generation

    async def set_now_playing(self, channel, track, *, epoch: int | None = None) -> None:
        """Show ``track`` when the bot already owns the status, or the status is empty.

        Unknown status, or text this bot did not set, is left untouched. A
        remembered line that no longer matches is forgotten and not replaced.
        ``epoch`` is the value from ``begin_track`` at schedule time. Omit it to
        use whatever generation is current when this call starts.
        """
        channel_id = _as_int(getattr(channel, "id", None))
        if channel_id is None:
            return
        if epoch is None:
            epoch = self._epoch.get(channel_id, 0)
        if self._epoch.get(channel_id, 0) != epoch:
            return
        text = format_now_playing(track)
        await self.ensure_known(channel)
        async with self._lock(channel_id):
            if self._epoch.get(channel_id, 0) != epoch:
                return
            owned = self.bot_text.get(channel_id)
            if owned is not None:
                known = self.known_status.get(channel_id, _MISSING)
                if known is _MISSING or known != owned:
                    self._forget(channel_id)
                    log.debug(
                        "Leaving voice status in channel %s alone; it no longer matches the bot",
                        channel_id,
                    )
                    return
                if text == owned:
                    return
            else:
                if channel_id not in self.known_status:
                    log.debug("Skipping voice status in channel %s; current status is unknown", channel_id)
                    return
                if not _empty(self.known_status[channel_id]):
                    log.debug("Skipping voice status in channel %s; someone else set it", channel_id)
                    return
            if not await self._edit_status(channel, text):
                return
            self._remember(channel_id, text)
            self.known_status[channel_id] = text

    async def clear(self, channel) -> None:
        """Remove the status only when it is still the line this bot set.

        ``bot_text`` is forgotten either way, so a later track will not assume
        the channel is still ours. The channel's generation advances first, so
        a now-playing task scheduled earlier will not write again.
        """
        channel_id = _as_int(getattr(channel, "id", None))
        if channel_id is None:
            return
        self._epoch[channel_id] = self._epoch.get(channel_id, 0) + 1
        async with self._lock(channel_id):
            owned = self.bot_text.get(channel_id)
            if owned is None:
                return
            self._forget(channel_id)
            known = self.known_status.get(channel_id, _MISSING)
            if known is _MISSING or known != owned:
                return
            if not await self._edit_status(channel, None):
                return
            self.known_status[channel_id] = None

    async def clear_stale_after_restart(self) -> None:
        """Clear saved lines the bot is not playing, once each status is known.

        Best effort, after the bot is ready. An unknown status is skipped and
        kept. A status that no longer matches the saved text is forgotten and
        left untouched. A match is cleared, then dropped.
        """
        wait = getattr(self.bot, "wait_until_ready", None)
        if wait is not None:
            try:
                await wait()
            except Exception:
                log.warning("Voice status cleanup skipped; the bot is not ready", exc_info=True)
                return
        for channel_id in list(self.bot_text):
            if self._channel_is_playing(channel_id):
                continue
            try:
                await self._clear_saved_if_still_ours(channel_id)
            except Exception:
                log.warning(
                    "Could not clear the saved voice status in channel %s",
                    channel_id,
                    exc_info=True,
                )

    def _on_voice_channel_status_update(self, data: Any) -> None:
        """Sync parser. A status other than ours means the bot no longer owns it."""
        try:
            if not isinstance(data, dict) or "id" not in data:
                return
            channel_id = _as_int(data.get("id"))
            if channel_id is None:
                return
            raw = data.get("status") if "status" in data else None
            self._observe(channel_id, raw)
        except Exception:
            log.debug("Ignored malformed VOICE_CHANNEL_STATUS_UPDATE", exc_info=True)

    def _on_channel_info(self, data: Any) -> None:
        """Sync parser. Resolves whoever is waiting on this guild's snapshot."""
        guild_id = None
        try:
            if not isinstance(data, dict):
                return
            guild_id = _as_int(data.get("guild_id"))
            for entry in data.get("channels") or []:
                if not isinstance(entry, dict) or "id" not in entry:
                    continue
                channel_id = _as_int(entry.get("id"))
                if channel_id is None:
                    continue
                raw = entry.get("status") if "status" in entry else None
                self._observe(channel_id, raw)
        except Exception:
            log.debug("Ignored malformed CHANNEL_INFO", exc_info=True)
        finally:
            if guild_id is not None:
                event = self._info_events.get(guild_id)
                if event is not None and not event.is_set():
                    event.set()

    def _observe(self, channel_id: int, raw: Any) -> None:
        status = _normalize_status(raw)
        self.known_status[channel_id] = status
        owned = self.bot_text.get(channel_id)
        if owned is not None and owned != status:
            self._forget(channel_id)

    async def _edit_status(self, channel, status: str | None) -> bool:
        channel_id = int(channel.id)
        if not self._can_set_status(channel):
            self._log_once(
                channel_id,
                "Cannot set voice status in channel %s: missing Set Voice Channel Status",
            )
            return False
        try:
            await channel.edit(status=status)
        except (discord.Forbidden, discord.HTTPException) as exc:
            self._log_once(channel_id, "Could not edit voice status in channel %s: %s", exc)
            return False
        return True

    def _can_set_status(self, channel) -> bool:
        try:
            guild = channel.guild
            me = guild.me
            if me is None:
                return False
            perms = channel.permissions_for(me)
            return bool(perms.set_voice_channel_status)
        except Exception:
            log.debug("Could not read voice-status permission", exc_info=True)
            return False

    def _log_once(self, channel_id: int, message: str, *args: object) -> None:
        if channel_id in self.permission_errors_logged:
            return
        self.permission_errors_logged.add(channel_id)
        log.warning(message, channel_id, *args)

    async def _clear_saved_if_still_ours(self, channel_id: int) -> None:
        channel = self._get_channel(channel_id)
        if channel is None:
            log.debug("Leaving saved voice status in channel %s; the channel is not cached", channel_id)
            return
        await self.ensure_known(channel)
        async with self._lock(channel_id):
            if self._channel_is_playing(channel_id):
                return
            owned = self.bot_text.get(channel_id)
            if owned is None:
                return
            known = self.known_status.get(channel_id, _MISSING)
            if known is _MISSING:
                log.debug("Leaving saved voice status in channel %s; current status is unknown", channel_id)
                return
            if known != owned:
                self._forget(channel_id)
                log.debug(
                    "Leaving voice status in channel %s alone; it no longer matches the bot",
                    channel_id,
                )
                return
            if not await self._edit_status(channel, None):
                return
            self.known_status[channel_id] = None
            self._forget(channel_id)

    def _channel_is_playing(self, channel_id: int) -> bool:
        checker = self.is_playing
        if checker is None:
            return False
        try:
            return bool(checker(channel_id))
        except Exception:
            log.warning(
                "Could not tell whether channel %s is playing; leaving its voice status",
                channel_id,
                exc_info=True,
            )
            return True

    def _get_channel(self, channel_id: int):
        getter = getattr(self.bot, "get_channel", None)
        if getter is None:
            return None
        try:
            return getter(channel_id)
        except Exception:
            log.debug("Could not resolve voice channel %s", channel_id, exc_info=True)
            return None

    def _remember(self, channel_id: int, text: str) -> None:
        if self.bot_text.get(channel_id) == text:
            return
        self.bot_text[channel_id] = text
        self._persist()

    def _forget(self, channel_id: int) -> None:
        if channel_id not in self.bot_text:
            return
        self.bot_text.pop(channel_id, None)
        self._persist()

    def _load(self) -> None:
        """Fill ``bot_text`` from disk. A missing or corrupt file leaves it empty."""
        path = self.path
        try:
            if not path.is_file():
                return
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            log.warning(
                "Could not read voice statuses from %s; starting with none",
                path,
                exc_info=True,
            )
            self.bot_text.clear()
            return
        if not isinstance(data, dict):
            log.warning("Voice status file %s is not an object; starting with none", path)
            self.bot_text.clear()
            return
        loaded: dict[int, str] = {}
        for key, value in data.items():
            channel_id = _as_int(key)
            if channel_id is None or not isinstance(value, str) or value == "":
                continue
            loaded[channel_id] = value
        self.bot_text = loaded

    def _persist(self) -> None:
        """Write ``bot_text`` atomically. Disk errors are logged, never raised."""
        path = self.path
        temporary = path.parent / (path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {str(channel_id): text for channel_id, text in self.bot_text.items()}
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(temporary, path)
        except Exception:
            log.warning("Could not save voice statuses to %s", path, exc_info=True)

    def _lock(self, channel_id: int) -> asyncio.Lock:
        lock = self._locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[channel_id] = lock
        return lock

    def _parsers(self) -> dict | None:
        try:
            parsers = self.bot._connection.parsers
        except Exception:
            log.debug("Voice status parsers are unavailable", exc_info=True)
            return None
        if not isinstance(parsers, dict):
            return None
        return parsers

    async def _request_channel_statuses(self, guild) -> bool:
        """Opcode 43 asks Discord to dispatch CHANNEL_INFO for this guild."""
        payload = {
            "op": REQUEST_CHANNEL_STATUSES,
            "d": {"guild_id": str(getattr(guild, "id", "")), "fields": ["status"]},
        }
        try:
            ws = self._websocket_for(guild)
            if ws is None:
                return False
            await ws.send_as_json(payload)
            return True
        except Exception:
            log.debug(
                "Could not request voice channel statuses for guild %s",
                getattr(guild, "id", "?"),
                exc_info=True,
            )
            return False

    def _websocket_for(self, guild):
        """The guild shard's gateway socket, or the bot's only socket.

        AutoShardedBot.get_shard returns a ShardInfo. In discord.py 2.7.1 that
        object has no ``ws``; the socket lives on the parent shard. A plain
        Client exposes ``bot.ws`` instead.
        """
        bot = self.bot
        try:
            shard_id = getattr(guild, "shard_id", None)
            get_shard = getattr(bot, "get_shard", None)
            if get_shard is not None and shard_id is not None:
                ws = self._ws_from_shard(get_shard(shard_id))
                if ws is not None:
                    return ws
        except Exception:
            log.debug("Could not reach the guild shard websocket", exc_info=True)
        try:
            return getattr(bot, "ws", None)
        except Exception:
            log.debug("Could not reach the bot websocket", exc_info=True)
            return None

    @staticmethod
    def _ws_from_shard(shard):
        if shard is None:
            return None
        ws = getattr(shard, "ws", None)
        if ws is not None:
            return ws
        parent = getattr(shard, "_parent", None)
        if parent is None:
            return None
        return getattr(parent, "ws", None)
