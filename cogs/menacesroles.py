import logging

import discord
from discord.ext import commands
from discord.ext.commands import Cog

from cogs.utils.server_config import get_guild_config

log = logging.getLogger(__name__)


class MenacesRoles(Cog):
    """Reaction-role assignment driven by per-guild server_configs."""

    def __init__(self, bot):
        self.bot = bot

    def _get_reaction_role_config(self, guild_id: int):
        config = get_guild_config(guild_id)
        if config is None or not config.enabled:
            return None, None
        channel_id = config.reaction_roles_channel_id
        role_map = config.reaction_role_map
        if channel_id is None or not role_map:
            return None, None
        return channel_id, role_map

    @commands.Cog.listener("on_raw_reaction_add")
    async def add_role(self, payload: discord.RawReactionActionEvent) -> None:
        channel_id, role_map = self._get_reaction_role_config(payload.guild_id)
        if channel_id is None or payload.channel_id != channel_id:
            return

        emoji_name = payload.emoji.name
        if emoji_name is None:
            return

        target_role_id = role_map.get(emoji_name)
        if target_role_id is None:
            return

        member = payload.member
        if member is None:
            guild = self.bot.get_guild(payload.guild_id)
            if guild is None:
                return
            member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return

        target_role = member.guild.get_role(target_role_id)
        if target_role is None:
            log.warning("Reaction role %s not found in guild %s", target_role_id, payload.guild_id)
            return

        try:
            await member.add_roles(target_role, reason=f"User reacted with {emoji_name}")
        except discord.Forbidden:
            log.warning("Missing permissions to add role %s in guild %s", target_role_id, payload.guild_id)
        except discord.HTTPException as e:
            log.error("Failed to add role %s for user %s: %s", target_role_id, payload.user_id, e)

    @commands.Cog.listener("on_raw_reaction_remove")
    async def remove_role(self, payload: discord.RawReactionActionEvent) -> None:
        channel_id, role_map = self._get_reaction_role_config(payload.guild_id)
        if channel_id is None or payload.channel_id != channel_id:
            return

        emoji_name = payload.emoji.name
        if emoji_name is None:
            return

        target_role_id = role_map.get(emoji_name)
        if target_role_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return

        target_role = guild.get_role(target_role_id)
        if target_role is None:
            log.warning("Reaction role %s not found in guild %s", target_role_id, payload.guild_id)
            return

        try:
            await member.remove_roles(target_role, reason=f"User removed reaction {emoji_name}")
        except discord.Forbidden:
            log.warning("Missing permissions to remove role %s in guild %s", target_role_id, payload.guild_id)
        except discord.HTTPException as e:
            log.error("Failed to remove role %s for user %s: %s", target_role_id, payload.user_id, e)


async def setup(bot):
    await bot.add_cog(MenacesRoles(bot))