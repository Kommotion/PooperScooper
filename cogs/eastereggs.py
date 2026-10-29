"""Small Menaces-to-Sobriety reactions driven by server_configs."""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands
from discord.ext.commands import Cog

from cogs.utils.server_config import ServerConfig, get_guild_config

log = logging.getLogger(__name__)

DEFAULT_PARTY_TIMEZONE = "America/Chicago"

HOOTY_PATTERN = re.compile(
    r"\bhooty\s+hoo+s?\b|\bhooty\s+fucking\s+hoot\b",
    re.IGNORECASE,
)
ABOUT_THAT_TIME_PATTERN = re.compile(
    r"\bit'?s about that time\b",
    re.IGNORECASE,
)
PARTY_REMINDER_PATTERN = re.compile(
    r"\b(?:reminder|tonight|headset|charge)\b|\bget ready\b",
    re.IGNORECASE,
)

HELP_PATTERN = re.compile(
    r"\?|"
    r"\b(?:help|broken|error|errors|crash|crashed|wont|won't|cant|can't|issue|issues|fix|bug|bugs|stuck|problem|problems)\b|"
    r"\bnot working\b|"
    r"\bdoesn'?t work\b|"
    r"\bdoes not work\b",
    re.IGNORECASE,
)


def mentions_hooty(content: str) -> bool:
    return HOOTY_PATTERN.search(content or "") is not None


def is_help_request(content: str, title: str = "") -> bool:
    return HELP_PATTERN.search(f"{title}\n{content or ''}") is not None


def channel_or_parent_in(channel_id: int, parent_id: int | None, channel_ids: set[int]) -> bool:
    if channel_id in channel_ids:
        return True
    return parent_id is not None and parent_id in channel_ids


def is_about_that_time(content: str) -> bool:
    return ABOUT_THAT_TIME_PATTERN.search(content or "") is not None


def party_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_PARTY_TIMEZONE)
    except ZoneInfoNotFoundError:
        log.warning("Unknown announcement party timezone %s", name)
        return ZoneInfo(DEFAULT_PARTY_TIMEZONE)


def is_saturday_party_reminder(content: str, created_at: datetime, zone: ZoneInfo) -> bool:
    """Saturday announcement that looks like the drinking-night heads-up.

    The wording changes. Any one of reminder, tonight, headset, charge, or
    "get ready" is enough.
    """
    if PARTY_REMINDER_PATTERN.search(content or "") is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    local = created_at.astimezone(zone)
    return local.weekday() == 5


def pick_party_emojis(pool: list[str], count: int, rng: random.Random) -> list[str]:
    unique = list(dict.fromkeys(emoji for emoji in pool if emoji))
    if count < 1 or not unique:
        return []
    return rng.sample(unique, min(count, len(unique)))


def is_thread_opener(message_id: int, channel_id: int, parent_id: int | None, forum_id: int | None) -> bool:
    """A forum post's starter message uses the thread id, which equals the message id."""
    if forum_id is None or parent_id != forum_id:
        return False
    return message_id == channel_id


class EasterEggs(Cog):
    """Phrase reactions configured per guild. Other servers stay quiet."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._redeemed_posts: set[int] = set()
        self._party_rng = random.Random()

    @commands.Cog.listener("on_message")
    async def on_easter_egg(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if (message.content or "").lstrip().startswith("!"):
            return

        config = get_guild_config(message.guild.id)
        if config is None or not config.enabled:
            return
        if message.author.bot and not (getattr(message, "webhook_id", None) and config.easter_eggs_allow_webhooks):
            return

        if self._should_salute(message, config):
            await self._salute(message, config)
        if await self._should_redeem(message, config):
            await self._redeem(message, config)
        if self._should_party(message, config):
            await self._party(message, config)

    def _parent_id(self, message: discord.Message) -> int | None:
        return getattr(message.channel, "parent_id", None)

    def _thread_name(self, message: discord.Message) -> str:
        if self._parent_id(message) is None:
            return ""
        return getattr(message.channel, "name", "") or ""

    def _should_salute(self, message: discord.Message, config: ServerConfig) -> bool:
        if not config.hooty_hoo_enabled or not config.hooty_hoo_emoji:
            return False
        if not mentions_hooty(message.content or ""):
            return False
        parent_id = self._parent_id(message)
        allowed = config.hooty_hoo_channel_ids
        if allowed and not channel_or_parent_in(message.channel.id, parent_id, allowed):
            return False
        return not channel_or_parent_in(message.channel.id, parent_id, config.hooty_hoo_skip_channel_ids)

    def _should_party(self, message: discord.Message, config: ServerConfig) -> bool:
        if not config.announcement_party_enabled or not config.announcement_party_emojis:
            return False
        if not channel_or_parent_in(
            message.channel.id,
            self._parent_id(message),
            config.announcement_party_channel_ids,
        ):
            return False
        content = message.content or ""
        if is_about_that_time(content):
            return True
        created_at = getattr(message, "created_at", None)
        if created_at is None:
            return False
        return is_saturday_party_reminder(content, created_at, party_timezone(config.announcement_party_timezone))

    async def _thread_parent_id(self, message: discord.Message) -> int | None:
        parent_id = self._parent_id(message)
        if parent_id is not None or message.guild is None or message.id != message.channel.id:
            return parent_id
        channel = message.guild.get_thread(message.channel.id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(message.channel.id)
            except (discord.HTTPException, discord.Forbidden) as exc:
                log.warning("Could not resolve thread %s: %s", message.channel.id, exc)
                return None
        return getattr(channel, "parent_id", None)

    async def _should_redeem(self, message: discord.Message, config: ServerConfig) -> bool:
        if not config.do_not_redeem_enabled or not config.do_not_redeem_reply:
            return False
        if message.id in self._redeemed_posts:
            return False
        parent_id = await self._thread_parent_id(message)
        if not is_thread_opener(
            message.id,
            message.channel.id,
            parent_id,
            config.do_not_redeem_channel_id,
        ):
            return False
        title = self._thread_name(message)
        if not title and parent_id is not None:
            thread = message.guild.get_thread(message.channel.id) if message.guild is not None else None
            title = getattr(thread, "name", "") or ""
        return is_help_request(message.content or "", title)

    async def _salute(self, message: discord.Message, config: ServerConfig) -> None:
        try:
            emoji = discord.PartialEmoji.from_str(config.hooty_hoo_emoji)
            await message.add_reaction(emoji)
        except (discord.HTTPException, discord.Forbidden) as exc:
            log.warning("Hooty salute failed in channel %s: %s", message.channel.id, exc)

    async def _party(self, message: discord.Message, config: ServerConfig) -> None:
        chosen = pick_party_emojis(
            config.announcement_party_emojis,
            config.announcement_party_reaction_count,
            self._party_rng,
        )
        for raw in chosen:
            try:
                await message.add_reaction(discord.PartialEmoji.from_str(raw))
            except (discord.HTTPException, discord.Forbidden, ValueError) as exc:
                log.warning("Party reaction %s failed in channel %s: %s", raw, message.channel.id, exc)

    async def _redeem(self, message: discord.Message, config: ServerConfig) -> None:
        self._redeemed_posts.add(message.id)
        try:
            await message.reply(config.do_not_redeem_reply, mention_author=False)
        except (discord.HTTPException, discord.Forbidden) as exc:
            self._redeemed_posts.discard(message.id)
            log.warning("DO NOT REDEEM reply failed in channel %s: %s", message.channel.id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EasterEggs(bot))
