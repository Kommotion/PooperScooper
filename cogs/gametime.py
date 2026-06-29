from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Cog

from cogs.utils.constants import (
    GAMETIME_JSON,
    HOURS_IN_DAY,
    MINUTES_IN_HOUR,
    SECONDS_IN_HOUR,
)
from cogs.utils.json_store import LockedJsonFile

log = logging.getLogger(__name__)

UPDATE_FREQUENCY = 60
SAVE_FREQUENCY = 2000
DEFAULT_PLAYED_LIMIT = 10
MAX_PLAYED_LIMIT = 25


class GameData:
    """Global playtime per user: member_id -> game_name -> seconds."""

    def __init__(self):
        self._store = LockedJsonFile(GAMETIME_JSON, default=dict, indent=None)
        self.game_data: dict[str, dict[str, int]] | None = None
        self.load_json()

    def load_json(self) -> None:
        raw = self._store.read()
        self.game_data = self._migrate_if_needed(raw)
        if self.game_data is not raw:
            self.dump_json()

    async def aload_json(self) -> None:
        raw = await self._store.aread()
        self.game_data = self._migrate_if_needed(raw)
        if self.game_data is not raw:
            await self.adump_json()

    def dump_json(self) -> None:
        if self.game_data is None:
            return
        self._store.write(self.game_data)

    async def adump_json(self) -> None:
        if self.game_data is None:
            return
        await self._store.awrite(self.game_data)

    @staticmethod
    def _is_global_format(data: dict) -> bool:
        if not data:
            return True
        for value in data.values():
            if not isinstance(value, dict) or not value:
                continue
            for inner_value in value.values():
                if isinstance(inner_value, dict):
                    return False
                if isinstance(inner_value, (int, str)):
                    return True
        return False

    @staticmethod
    def _merge_member_games(
        target: dict[str, int],
        source: dict,
    ) -> None:
        for game, seconds in source.items():
            if not isinstance(seconds, (int, str)):
                continue
            game_key = str(game)
            target[game_key] = max(target.get(game_key, 0), int(seconds))

    def _migrate_if_needed(self, data: dict) -> dict[str, dict[str, int]]:
        if self._is_global_format(data):
            migrated: dict[str, dict[str, int]] = {}
            for member_id, games in data.items():
                if not isinstance(games, dict):
                    continue
                member_key = str(member_id)
                migrated[member_key] = {}
                self._merge_member_games(migrated[member_key], games)
            return migrated

        log.info("Migrating per-guild gametime.json to global per-user format")
        migrated: dict[str, dict[str, int]] = {}

        for guild_games in data.values():
            if not isinstance(guild_games, dict):
                continue
            for member_id, games in guild_games.items():
                if not isinstance(games, dict):
                    continue
                member_key = str(member_id)
                member_data = migrated.setdefault(member_key, {})
                self._merge_member_games(member_data, games)

        return migrated

    def increment_time_played(
        self,
        member_id: int,
        game: str,
        *,
        seconds: int = UPDATE_FREQUENCY,
    ) -> None:
        if self.game_data is None:
            log.critical("Tried incrementing game data before it was loaded from JSON!")
            return

        member_key = str(member_id)
        member_data = self.game_data.setdefault(member_key, {})
        member_data[game] = int(member_data.get(game, 0)) + seconds

    def get_member_data(self, member_id: int) -> dict[str, int]:
        if self.game_data is None:
            return {}
        return dict(self.game_data.get(str(member_id), {}))

    async def reset_member_data(self, member_id: int) -> bool:
        if self.game_data is None:
            return False
        member_key = str(member_id)
        if member_key not in self.game_data:
            return False
        del self.game_data[member_key]
        await self.adump_json()
        return True

    def get_guild_leaderboard(
        self,
        member_ids: set[int],
        *,
        limit: int = 10,
    ) -> list[tuple[int, int]]:
        if self.game_data is None:
            return []

        totals: list[tuple[int, int]] = []
        for member_id in member_ids:
            games = self.game_data.get(str(member_id))
            if not games:
                continue
            total = sum(int(seconds) for seconds in games.values())
            if total:
                totals.append((member_id, total))

        totals.sort(key=lambda item: item[1], reverse=True)
        return totals[:limit]

    def get_guild_top_games(
        self,
        member_ids: set[int],
        *,
        limit: int = 10,
    ) -> list[tuple[str, int]]:
        if self.game_data is None:
            return []

        totals: dict[str, int] = {}
        for member_id in member_ids:
            for game, seconds in self.game_data.get(str(member_id), {}).items():
                totals[game] = totals.get(game, 0) + int(seconds)

        ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
        return ranked[:limit]


class Gametime(Cog):
    """Tracks games played by members across all servers."""

    def __init__(self, bot):
        self.bot = bot
        self.game_data = GameData()
        self.last_save_time = time.time()
        self.last_update_time = time.time()
        self.update_gametime.start()

    gametime_group = app_commands.Group(name="gametime", description="Track time spent playing games.")

    @tasks.loop(seconds=UPDATE_FREQUENCY)
    async def update_gametime(self) -> None:
        now = time.time()
        elapsed = max(1, int(now - self.last_update_time))
        if elapsed < UPDATE_FREQUENCY - 1:
            return

        updated_members: set[int] = set()
        for guild in self.bot.guilds:
            for member in guild.members:
                if member.bot or member.id in updated_members:
                    continue

                for activity in member.activities:
                    if activity.type == discord.ActivityType.playing and activity.name:
                        self.game_data.increment_time_played(
                            member.id,
                            activity.name,
                            seconds=elapsed,
                        )
                        updated_members.add(member.id)
                        break

        self.last_update_time = now
        log.debug("Finished updating gametime data")
        await self.save_game_data()

    @update_gametime.before_loop
    async def before_gametime(self) -> None:
        await self.bot.wait_until_ready()

    @update_gametime.after_loop
    async def save_gametime(self) -> None:
        await self.save_game_data(force=True)

    async def save_game_data(self, force: bool = False) -> None:
        now = time.time()
        if now - self.last_save_time >= SAVE_FREQUENCY or force:
            log.debug("Saving gametime data to storage")
            await self.game_data.adump_json()
            self.last_save_time = now

    @staticmethod
    def convert_seconds_to_string(seconds: int) -> str:
        hours, remainder = divmod(seconds, SECONDS_IN_HOUR)
        minutes, seconds = divmod(remainder, MINUTES_IN_HOUR)
        days, hours = divmod(hours, HOURS_IN_DAY)
        if days:
            fmt = "{d} days, {h} hours, {m} minutes, and {s} seconds"
        else:
            fmt = "{h} hours, {m} minutes, and {s} seconds"
        return fmt.format(d=days, h=hours, m=minutes, s=seconds)

    @staticmethod
    def convert_seconds_to_compact(seconds: int) -> str:
        """Short duration for ranked lists (e.g. 482h 12m)."""
        hours, remainder = divmod(int(seconds), SECONDS_IN_HOUR)
        minutes, _ = divmod(remainder, MINUTES_IN_HOUR)
        days, hours = divmod(hours, HOURS_IN_DAY)
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m" if minutes else f"{hours}h"
        return f"{minutes}m"

    @staticmethod
    def _ranked_games(member_data: dict[str, int]) -> list[tuple[str, int]]:
        return sorted(member_data.items(), key=lambda item: int(item[1]), reverse=True)

    def _build_played_embed(
        self,
        member: discord.Member,
        member_data: dict[str, int],
        *,
        limit: int,
    ) -> discord.Embed:
        ranked = self._ranked_games(member_data)
        total_seconds = sum(int(seconds) for seconds in member_data.values())
        shown = ranked[:limit]

        lines = [
            f"**{rank}.** {game} — {self.convert_seconds_to_compact(seconds)}"
            for rank, (game, seconds) in enumerate(shown, start=1)
        ]
        embed = discord.Embed(
            title="Gametime 🎮",
            description="\n".join(lines),
            colour=discord.Colour.blue(),
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)

        game_count = len(ranked)
        footer = (
            f"Total: {self.convert_seconds_to_string(total_seconds)} across {game_count} game"
            f"{'' if game_count == 1 else 's'}"
        )
        if game_count > limit:
            footer += f" · showing top {limit}"
        footer += " · shared across all servers"
        embed.set_footer(text=footer)
        return embed

    async def _send_member_stats(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        *,
        limit: int,
        empty_message: str,
    ) -> None:
        member_data = self.game_data.get_member_data(member.id)
        if not member_data:
            await interaction.response.send_message(empty_message, ephemeral=True)
            return

        embed = self._build_played_embed(member, member_data, limit=limit)
        await interaction.response.send_message(embed=embed)

    @gametime_group.command(
        name="played",
        description="Show your top games by tracked Discord playtime.",
    )
    @app_commands.describe(limit="How many games to show (max 25).")
    async def gametime_played(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, MAX_PLAYED_LIMIT] = DEFAULT_PLAYED_LIMIT,
    ) -> None:
        await self._send_member_stats(
            interaction,
            interaction.user,
            limit=limit,
            empty_message="I scooped a lot but couldn't find any of your playtime yet!",
        )

    @gametime_group.command(
        name="stats",
        description="Show a member's top games by tracked Discord playtime.",
    )
    @app_commands.describe(
        user="Member to look up.",
        limit="How many games to show (max 25).",
    )
    async def gametime_stats(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        limit: app_commands.Range[int, 1, MAX_PLAYED_LIMIT] = DEFAULT_PLAYED_LIMIT,
    ) -> None:
        await self._send_member_stats(
            interaction,
            user,
            limit=limit,
            empty_message=f"No playtime tracked for {user.display_name} yet.",
        )

    @gametime_group.command(
        name="leaderboard",
        description="Top members in this server by total playtime across all games.",
    )
    @app_commands.describe(limit="How many members to show (max 25).")
    async def gametime_leaderboard(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        member_ids = {member.id for member in interaction.guild.members if not member.bot}
        leaderboard = self.game_data.get_guild_leaderboard(member_ids, limit=limit)
        if not leaderboard:
            await interaction.response.send_message(
                "No playtime tracked for members in this server yet.",
                ephemeral=True,
            )
            return

        lines = []
        for rank, (member_id, total_seconds) in enumerate(leaderboard, start=1):
            member = interaction.guild.get_member(member_id)
            name = member.display_name if member else f"User {member_id}"
            lines.append(f"**{rank}.** {name} — {self.convert_seconds_to_string(total_seconds)}")

        embed = discord.Embed(
            title="Gametime Leaderboard 🏆",
            description="\n".join(lines),
            colour=discord.Colour.gold(),
        )
        embed.set_footer(text="Playtime is shared across all servers the bot is in.")
        await interaction.response.send_message(embed=embed)

    @gametime_group.command(name="topgames", description="Most-played games among members in this server.")
    @app_commands.describe(limit="How many games to show (max 25).")
    async def gametime_topgames(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        member_ids = {member.id for member in interaction.guild.members if not member.bot}
        top_games = self.game_data.get_guild_top_games(member_ids, limit=limit)
        if not top_games:
            await interaction.response.send_message(
                "No games tracked for members in this server yet.",
                ephemeral=True,
            )
            return

        lines = [
            f"**{rank}.** {game} — {self.convert_seconds_to_string(seconds)}"
            for rank, (game, seconds) in enumerate(top_games, start=1)
        ]
        embed = discord.Embed(
            title="Top Games 🎮",
            description="\n".join(lines),
            colour=discord.Colour.green(),
        )
        embed.set_footer(text="Based on playtime shared across all servers the bot is in.")
        await interaction.response.send_message(embed=embed)

    @gametime_group.command(name="reset", description="Clear a member's playtime everywhere.")
    @app_commands.describe(user="Member whose stats will be cleared.")
    @app_commands.default_permissions(manage_guild=True)
    async def gametime_reset(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need Manage Server to reset gametime.", ephemeral=True)
            return

        removed = await self.game_data.reset_member_data(user.id)
        if not removed:
            await interaction.response.send_message(
                f"No gametime data found for {user.display_name}.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Cleared gametime for {user.mention} across all servers.",
            ephemeral=True,
        )

    @commands.command(name="played", hidden=True)
    async def played_legacy(self, ctx: commands.Context) -> None:
        await ctx.send("Use `/gametime played` instead.")

    @commands.command(name="save", hidden=True)
    @commands.is_owner()
    async def save_legacy(self, ctx: commands.Context) -> None:
        await self.save_game_data(force=True)
        await ctx.message.add_reaction("👍")


async def setup(bot):
    await bot.add_cog(Gametime(bot))