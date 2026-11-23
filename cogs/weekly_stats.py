"""Weekly stats card for enabled servers.

Tracks aggregate counters, this week's active members, and an opt-out list.
Message text, prompts, and per-person totals are not stored. The card posts
once a week. The first time an enabled guild has no posted week, the latest
week that is already due is recorded as a baseline and nothing is posted.
The week still in progress posts at the next scheduled time. After that, a
missed Sunday is caught up once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.utils.stats_card import CardNames, render_alltime_card, render_stats_card
from cogs.utils.stats_commands import (
    alias_map_from_bot,
    alias_map_from_source,
    canonical_command_name,
    is_owner_only_name,
)
from cogs.utils.stats_schedule import LA, LA_NAME, PostSchedule, due_week
from cogs.utils.stats_store import StatsStore

log = logging.getLogger(__name__)

# Menaces to Sobriety. Same id as cogs.candyhunt.MTS_GUILD_ID.
MTS_GUILD_ID = 932057681307512922

# MTS #illuminaughty. The card still counts MTS only.
MTS_ILLUMINAUGHTY_CHANNEL_ID = 1082770598981283880
MTS_REPORT_CHANNEL_ID = MTS_ILLUMINAUGHTY_CHANNEL_ID


@dataclass(frozen=True)
class GuildStatsConfig:
    enabled: bool = True
    report_channel_id: int | None = None
    weekday: int = 6
    hour: int = 21
    timezone: str = LA_NAME

    def schedule(self) -> PostSchedule:
        return PostSchedule(self.weekday, self.hour, self.timezone)


DEFAULT_GUILDS: dict[int, GuildStatsConfig] = {
    MTS_GUILD_ID: GuildStatsConfig(
        enabled=True,
        report_channel_id=MTS_REPORT_CHANNEL_ID,
        weekday=6,
        hour=21,
        timezone=LA_NAME,
    ),
}


def _pick(data: dict, key: str, default):
    if key not in data:
        return default
    value = data[key]
    # An explicit null channel id clears the in-code channel. Other nulls keep defaults.
    if value is None and key != "report_channel_id":
        return default
    return value


def apply_config_overrides(base: dict[int, GuildStatsConfig], section) -> dict[int, GuildStatsConfig]:
    """Merge an optional config section onto the in-code guild defaults."""
    merged = dict(base)
    if section is None:
        return merged
    guilds = getattr(section, "guilds", None)
    if guilds is None and isinstance(section, dict):
        guilds = section.get("guilds") or {}
    if not guilds:
        return merged
    for key, value in guilds.items():
        guild_id = int(key)
        current = merged.get(guild_id, GuildStatsConfig())
        # Missing keys keep the in-code defaults. A dumped model includes every
        # field, so only keys the config actually set are allowed to override.
        if hasattr(value, "model_dump"):
            data = value.model_dump(exclude_unset=True)
        else:
            data = dict(value)
        zone = _pick(data, "timezone", current.timezone) or current.timezone
        merged[guild_id] = GuildStatsConfig(
            enabled=bool(_pick(data, "enabled", current.enabled)),
            report_channel_id=_pick(data, "report_channel_id", current.report_channel_id),
            weekday=int(_pick(data, "weekday", current.weekday)),
            hour=int(_pick(data, "hour", current.hour)),
            timezone=str(zone),
        )
    return merged


def _tracked_channel_id(channel) -> int | None:
    """Channel id for a message. Threads, including forum posts, use the parent."""
    if isinstance(channel, discord.Thread):
        parent_id = getattr(channel, "parent_id", None)
        if parent_id is not None:
            return int(parent_id)
    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return None
    return int(channel_id)


def load_guild_configs() -> dict[int, GuildStatsConfig]:
    """In-code defaults, overridden by config weekly_stats when that section loads."""
    try:
        from cogs.utils.config import load_config

        cfg = load_config()
    except Exception:
        log.warning("weekly stats config could not be read; using in-code defaults")
        return dict(DEFAULT_GUILDS)
    return apply_config_overrides(dict(DEFAULT_GUILDS), getattr(cfg, "weekly_stats", None))


class WeeklyStats(commands.Cog):
    """Per-guild weekly activity card."""

    def __init__(self, bot, *, store: StatsStore | None = None, guilds: dict | None = None) -> None:
        self.bot = bot
        self._owns_store = store is None
        self.store = store if store is not None else StatsStore()
        if guilds is None:
            self.guilds = load_guild_configs()
        else:
            self.guilds = {int(guild_id): cfg for guild_id, cfg in guilds.items()}
        # (guild_id, user_id) -> (channel_id, joined_at). Join time is not persisted.
        self._voice: dict[tuple[int, int], tuple[int, datetime]] = {}
        self._voice_seeded = False
        # (guild_id, week_start iso) already warned about a missing report channel.
        self._missing_channel_warned: set[tuple[int, str]] = set()
        self._source_command_map: dict[str, str] | None = None

    async def cog_load(self) -> None:
        self._start_loops()

    def cog_unload(self) -> None:
        self.flush_loop.cancel()
        self.scheduler_loop.cancel()
        try:
            self._flush_open_voice()
        finally:
            self.store.flush()
            if self._owns_store:
                self.store.close()

    def _start_loops(self) -> None:
        try:
            import asyncio

            asyncio.get_running_loop()
        except RuntimeError:
            return
        if not self.flush_loop.is_running():
            self.flush_loop.start()
        if not self.scheduler_loop.is_running():
            self.scheduler_loop.start()

    def _enabled(self, guild_id: int | None) -> bool:
        if guild_id is None:
            return False
        cfg = self.guilds.get(int(guild_id))
        return cfg is not None and cfg.enabled

    def _now(self) -> datetime:
        return datetime.now(LA)

    def record_imagegen(
        self,
        guild_id: int | None,
        *,
        model: str,
        loras=(),
        edit_mode: str | None = None,
        width: int | None = None,
        height: int | None = None,
        steps: int | None = None,
        sampler: str | None = None,
        seconds: float | None = None,
        success: bool = True,
    ) -> None:
        if not self._enabled(guild_id):
            return
        self.store.record_imagegen(
            int(guild_id),
            model=model,
            loras=loras,
            edit_mode=edit_mode,
            width=width,
            height=height,
            steps=steps,
            sampler=sampler,
            seconds=seconds,
            success=success,
        )

    def record_imagegen_followup(self, guild_id: int | None, kind: str) -> None:
        if kind not in {"regenerate", "edit", "kept"}:
            return
        if not self._enabled(guild_id):
            return
        self.store.record_imagegen_followup(int(guild_id), kind)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = getattr(message, "guild", None)
        if guild is None or not self._enabled(getattr(guild, "id", None)):
            return
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return
        channel = getattr(message, "channel", None)
        channel_id = _tracked_channel_id(channel)
        user_id = getattr(author, "id", None)
        if channel_id is None or user_id is None:
            return
        when = getattr(message, "created_at", None) or self._now()
        self.store.record_message(guild.id, channel_id, user_id, when)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        guild_id = getattr(payload, "guild_id", None)
        if not self._enabled(guild_id):
            return
        member = getattr(payload, "member", None)
        if member is not None and getattr(member, "bot", False):
            return
        emoji = getattr(payload, "emoji", None)
        if emoji is None:
            return
        emoji_id = getattr(emoji, "id", None)
        if emoji_id:
            key = str(emoji_id)
            name = getattr(emoji, "name", None) or key
            is_custom = True
        else:
            key = str(emoji)
            name = key
            is_custom = False
        when = self._now()
        self.store.record_reaction(
            int(guild_id),
            getattr(payload, "user_id", None),
            key,
            name=name,
            is_custom=is_custom,
            when=when,
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        if getattr(member, "bot", False):
            return
        guild = getattr(member, "guild", None)
        guild_id = getattr(guild, "id", None)
        if not self._enabled(guild_id):
            return
        before_id = getattr(getattr(before, "channel", None), "id", None)
        after_id = getattr(getattr(after, "channel", None), "id", None)
        if before_id == after_id:
            return
        now = self._now()
        key = (int(guild_id), int(member.id))
        if key in self._voice:
            channel_id, joined = self._voice.pop(key)
            self.store.record_voice(key[0], channel_id, key[1], joined, now)
        if after_id is not None:
            self._voice[key] = (int(after_id), now)

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction, command) -> None:
        guild_id = getattr(interaction, "guild_id", None)
        if not self._enabled(guild_id):
            return
        user = getattr(interaction, "user", None)
        if user is not None and getattr(user, "bot", False):
            return
        name = getattr(command, "qualified_name", None) or getattr(command, "name", None)
        if not name or not str(name).strip():
            return
        if is_owner_only_name(str(name)):
            return
        self.store.record_command(int(guild_id), str(name), "slash")

    @commands.Cog.listener()
    async def on_command_completion(self, ctx) -> None:
        guild = getattr(ctx, "guild", None)
        guild_id = getattr(guild, "id", None)
        if not self._enabled(guild_id):
            return
        author = getattr(ctx, "author", None)
        if author is not None and getattr(author, "bot", False):
            return
        command = getattr(ctx, "command", None)
        name = getattr(command, "qualified_name", None) if command is not None else None
        if not name or not str(name).strip():
            return
        raw = str(name).strip()
        mapping = self._prefix_command_map()
        canonical = mapping.get(raw.casefold())
        if canonical is None:
            canonical = canonical_command_name(raw, mapping)
        if not canonical or is_owner_only_name(canonical):
            return
        self.store.record_command(int(guild_id), canonical, "prefix")

    def _prefix_command_map(self) -> dict[str, str]:
        """Bot command tree when it is loaded. Otherwise the source scan."""
        if getattr(self.bot, "commands", None) is not None:
            return alias_map_from_bot(self.bot)
        if self._source_command_map is None:
            root = Path(__file__).resolve().parents[1]
            from PooperScooper import extensions_to_load

            try:
                from cogs.utils.config import load_config

                cfg = load_config()
            except Exception:
                cfg = None
            self._source_command_map = alias_map_from_source(root, modules=extensions_to_load(cfg))
        return self._source_command_map

    def _cut_open_voice(self, guild_id: int, now: datetime) -> None:
        """Close open voice sessions so their minutes land in the week being posted."""
        for key in list(self._voice):
            if key[0] != int(guild_id):
                continue
            channel_id, joined = self._voice[key]
            self.store.record_voice(key[0], channel_id, key[1], joined, now)
            self._voice[key] = (channel_id, now)

    def _flush_open_voice(self, now: datetime | None = None) -> None:
        """Store minutes for sessions that are still open, then drop them."""
        moment = now if now is not None else self._now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo(LA_NAME))
        for key in list(self._voice):
            channel_id, joined = self._voice.pop(key)
            self.store.record_voice(key[0], channel_id, key[1], joined, moment)

    def _seed_open_voice(self, now: datetime | None = None) -> None:
        """Start sessions for people already in voice when the cog comes up.

        Runs once. Bots are skipped. Opted-out users stay in the session map
        so their minutes still land on the channel totals.
        """
        if self._voice_seeded:
            return
        self._voice_seeded = True
        moment = now if now is not None else self._now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo(LA_NAME))
        for guild_id, cfg in list(self.guilds.items()):
            if not cfg.enabled:
                continue
            guild = self._guild(int(guild_id))
            if guild is None:
                continue
            for user_id, member, channel in _voice_occupants(guild):
                if user_id is None or channel is None:
                    continue
                if member is not None and getattr(member, "bot", False):
                    continue
                channel_id = getattr(channel, "id", None)
                if channel_id is None:
                    continue
                key = (int(guild_id), int(user_id))
                if key in self._voice:
                    continue
                self._voice[key] = (int(channel_id), moment)

    def _guild(self, guild_id: int):
        getter = getattr(self.bot, "get_guild", None)
        if not callable(getter):
            return None
        return getter(int(guild_id))

    def _known_custom_emojis(self, guild_id: int) -> list[tuple[str, str]] | None:
        guild = self._guild(guild_id)
        if guild is None:
            return None
        found = []
        for emoji in getattr(guild, "emojis", None) or []:
            emoji_id = getattr(emoji, "id", None)
            if emoji_id is None:
                continue
            found.append((str(emoji_id), str(getattr(emoji, "name", None) or emoji_id)))
        return found

    def _known_channel_ids(self, guild) -> list[int] | None:
        """Text channels and forums currently in the guild. None if the guild is missing."""
        if guild is None:
            return None
        found: list[int] = []
        seen: set[int] = set()

        def add(collection) -> None:
            for channel in collection or []:
                channel_id = getattr(channel, "id", None)
                if channel_id is None:
                    continue
                channel_id = int(channel_id)
                if channel_id in seen:
                    continue
                seen.add(channel_id)
                found.append(channel_id)

        add(getattr(guild, "text_channels", None))
        forums = getattr(guild, "forums", None)
        if forums is None:
            forums = getattr(guild, "forum_channels", None)
        add(forums)
        return found

    def _warn_missing_channel(self, guild_id: int, due, cfg: GuildStatsConfig) -> None:
        key = (int(guild_id), due.isoformat())
        if key in self._missing_channel_warned:
            return
        self._missing_channel_warned.add(key)
        log.warning(
            "weekly stats skipped guild %s: report channel %s was not found",
            guild_id,
            cfg.report_channel_id,
        )

    def _report_channel(self, guild, cfg: GuildStatsConfig):
        """Resolve the card channel by id. It may live in another guild."""
        channel_id = cfg.report_channel_id
        if not channel_id:
            return None
        channel = None
        bot_getter = getattr(self.bot, "get_channel", None)
        if callable(bot_getter):
            channel = bot_getter(int(channel_id))
        if not hasattr(channel, "send") and guild is not None:
            getter = getattr(guild, "get_channel", None)
            if callable(getter):
                channel = getter(int(channel_id))
        if channel is not None and hasattr(channel, "send"):
            return channel
        return None

    def _card_names(self, guild, stats) -> CardNames:
        channels: dict[int, str] = {}
        emojis: dict[str, str] = {}

        def remember(channel) -> None:
            if channel is None:
                return
            channel_id = getattr(channel, "id", None)
            name = getattr(channel, "name", None)
            if isinstance(channel_id, bool) or not isinstance(channel_id, int) or not name:
                return
            channels.setdefault(channel_id, str(name))

        if guild is not None:
            for collection_name in ("text_channels", "forums", "forum_channels", "voice_channels"):
                for channel in getattr(guild, collection_name, None) or []:
                    remember(channel)
            getter = getattr(guild, "get_channel", None)
            rows = []
            for attr in (
                "top_channels",
                "bottom_channels",
                "dead_channels",
                "top_voice_channels",
                "top_channels_all_time",
                "bottom_channels_all_time",
                "top_voice_channels_all_time",
            ):
                rows.extend(getattr(stats, attr, ()) or ())
            if callable(getter):
                for row in rows:
                    if row.channel_id in channels:
                        continue
                    remember(getter(row.channel_id))
        for row in list(stats.top_reactions) + list(stats.least_custom_emojis):
            emojis[row.emoji_key] = row.name
        return CardNames(channels, {}, emojis)

    async def _send_card(self, destination, stats, names: CardNames, guild_name: str) -> None:
        import asyncio

        from cogs.utils.emoji_cache import prefetch

        custom_rows = []
        for attr in (
            "top_reactions",
            "least_custom_emojis",
            "least_custom_emojis_weekly",
            "top_reactions_all_time",
        ):
            custom_rows.extend(getattr(stats, attr, ()) or ())
        custom_ids = [row.emoji_key for row in custom_rows if getattr(row, "is_custom", False)]
        await prefetch(custom_ids)

        def _render_pair():
            weekly = render_stats_card(stats, names, guild_name)
            alltime = render_alltime_card(stats, names, guild_name)
            return weekly, alltime

        weekly_png, alltime_png = await asyncio.to_thread(_render_pair)
        weekly_image = discord.File(fp=BytesIO(weekly_png), filename="weekly-stats.png")
        alltime_image = discord.File(fp=BytesIO(alltime_png), filename="alltime-stats.png")
        await destination.send(
            content=(
                f"This week and all time for {guild_name}"
                f" · week of {stats.week_start.isoformat()}"
            ),
            files=[weekly_image, alltime_image],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def post_due_guilds(self, now: datetime | None = None) -> None:
        moment = now if now is not None else self._now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo(LA_NAME))
        for guild_id, cfg in list(self.guilds.items()):
            if not cfg.enabled:
                continue
            try:
                await self._post_if_due(int(guild_id), cfg, moment)
            except Exception:
                log.exception("weekly stats failed for guild %s", guild_id)

    async def _post_if_due(self, guild_id: int, cfg: GuildStatsConfig, now: datetime) -> bool:
        last = self.store.last_posted_week_start(guild_id)
        due = due_week(now, last, cfg.schedule())
        if due is None:
            return False
        if last is None:
            # `due` is the latest week whose post time has already passed.
            # Record it and do not send it. The week still in progress posts
            # at its own Sunday 21:00 and includes everything tracked since
            # deploy. Baselining that open week would skip the first card.
            self.store.ensure_baseline(guild_id, due)
            return False
        self._cut_open_voice(guild_id, now)
        guild = self._guild(guild_id)
        known_emojis = self._known_custom_emojis(guild_id)
        known_channels = self._known_channel_ids(guild)
        command_map = self._prefix_command_map()
        stats = self.store.build_stats(
            guild_id,
            now,
            week_start=due,
            known_custom_emojis=known_emojis,
            known_channel_ids=known_channels,
            command_map=command_map,
        )
        channel = self._report_channel(guild, cfg)
        if channel is None:
            self._warn_missing_channel(guild_id, due, cfg)
            return False
        names = self._card_names(guild, stats)
        guild_name = str(getattr(guild, "name", None) or guild_id)
        try:
            await self._send_card(channel, stats, names, guild_name)
        except Exception:
            log.exception("weekly stats post failed for guild %s week %s", guild_id, due.isoformat())
            return False
        self.store.close_week(
            guild_id,
            due,
            now,
            known_custom_emojis=known_emojis,
            known_channel_ids=known_channels,
            command_map=command_map,
        )
        log.info("posted weekly stats guild=%s week=%s", guild_id, due.isoformat())
        return True

    @tasks.loop(seconds=30)
    async def flush_loop(self) -> None:
        self.store.flush()

    @flush_loop.before_loop
    async def before_flush_loop(self) -> None:
        waiter = getattr(self.bot, "wait_until_ready", None)
        if callable(waiter):
            await waiter()

    @tasks.loop(seconds=60)
    async def scheduler_loop(self) -> None:
        await self.post_due_guilds()

    @scheduler_loop.before_loop
    async def before_scheduler_loop(self) -> None:
        waiter = getattr(self.bot, "wait_until_ready", None)
        if callable(waiter):
            await waiter()
        self._seed_open_voice()

    stats = app_commands.Group(
        name="stats",
        description="Weekly stats privacy for this server.",
        guild_only=True,
    )

    @stats.command(name="optout", description="Stop counting you as an active member in this server.")
    @app_commands.guild_only()
    async def stats_optout(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        user_id = interaction.user.id
        self.store.opt_out(guild_id, user_id)
        session = self._voice.pop((int(guild_id), int(user_id)), None)
        if session is not None:
            channel_id, joined = session
            self.store.record_voice(guild_id, channel_id, user_id, joined, self._now())
        await interaction.response.send_message(
            "You're opted out in this server. You won't be counted as an active member from now on.",
            ephemeral=True,
        )

    @stats.command(name="optin", description="Resume weekly stats tracking for you in this server.")
    @app_commands.guild_only()
    async def stats_optin(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        user_id = interaction.user.id
        self.store.opt_in(guild_id, user_id)
        voice = getattr(interaction.user, "voice", None)
        channel = getattr(voice, "channel", None) if voice is not None else None
        channel_id = getattr(channel, "id", None)
        if channel_id is not None:
            self._voice[(int(guild_id), int(user_id))] = (int(channel_id), self._now())
        await interaction.response.send_message(
            "You're opted back in. New activity in this server counts toward the weekly card.",
            ephemeral=True,
        )

    @commands.command(name="statscard")
    @commands.is_owner()
    async def statscard(self, ctx: commands.Context, guild_id: int | None = None) -> None:
        """Post the current weekly stats card here without closing the week."""
        target = int(guild_id) if guild_id is not None else getattr(getattr(ctx, "guild", None), "id", None)
        if target is None:
            await ctx.send("Pass a guild id.", allowed_mentions=discord.AllowedMentions.none())
            return
        now = self._now()
        self._cut_open_voice(target, now)
        guild = self._guild(target)
        known = self._known_custom_emojis(target)
        known_channels = self._known_channel_ids(guild)
        stats = self.store.build_stats(
            target,
            now,
            known_custom_emojis=known,
            known_channel_ids=known_channels,
            command_map=self._prefix_command_map(),
        )
        names = self._card_names(guild, stats)
        guild_name = str(getattr(guild, "name", None) or target)
        await self._send_card(ctx, stats, names, guild_name)


def _voice_occupants(guild):
    """(user id, member or None, channel) for everyone currently in voice."""
    states = getattr(guild, "voice_states", None)
    if not isinstance(states, dict):
        states = getattr(guild, "_voice_states", None)
    if isinstance(states, dict):
        getter = getattr(guild, "get_member", None)
        for user_id, state in states.items():
            member = getattr(state, "member", None)
            if member is None and callable(getter):
                try:
                    member = getter(int(user_id))
                except (TypeError, ValueError):
                    member = None
            yield user_id, member, getattr(state, "channel", None)
        return
    for attr in ("voice_channels", "stage_channels"):
        for channel in getattr(guild, attr, None) or []:
            for member in getattr(channel, "members", None) or []:
                yield getattr(member, "id", None), member, channel


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WeeklyStats(bot))
