from __future__ import annotations

from typing import TYPE_CHECKING, Callable, TypeVar

from discord import app_commands
from discord.ext import commands

from cogs.utils.constants import MTS_ILLUMINAUGHTY_ROLE_ID
from cogs.utils.server_config import get_guild_config

if TYPE_CHECKING:
    from .context import GuildContext

T = TypeVar('T')


async def check_permissions(ctx: GuildContext, perms: dict[str, bool], *, check=all):
    is_owner = await ctx.bot.is_owner(ctx.author)
    if is_owner:
        return True

    resolved = ctx.channel.permissions_for(ctx.author)
    return check(getattr(resolved, name, None) == value for name, value in perms.items())


def has_permissions(*, check=all, **perms: bool):
    async def pred(ctx: GuildContext):
        return await check_permissions(ctx, perms, check=check)

    return commands.check(pred)


async def check_guild_permissions(ctx: GuildContext, perms: dict[str, bool], *, check=all):
    is_owner = await ctx.bot.is_owner(ctx.author)
    if is_owner:
        return True

    if ctx.guild is None:
        return False

    resolved = ctx.author.guild_permissions
    return check(getattr(resolved, name, None) == value for name, value in perms.items())


def has_guild_permissions(*, check=all, **perms: bool):
    async def pred(ctx: GuildContext):
        return await check_guild_permissions(ctx, perms, check=check)

    return commands.check(pred)


def hybrid_permissions_check(**perms: bool) -> Callable[[T], T]:
    async def pred(ctx: GuildContext):
        return await check_guild_permissions(ctx, perms)

    def decorator(func: T) -> T:
        commands.check(pred)(func)
        app_commands.default_permissions(**perms)(func)
        return func

    return decorator


def is_manager():
    return hybrid_permissions_check(manage_guild=True)


def is_mod():
    return hybrid_permissions_check(ban_members=True, manage_messages=True)


def is_admin():
    return hybrid_permissions_check(administrator=True)


async def _caller_is_bot_owner(ctx: commands.Context) -> bool:
    bot = getattr(ctx, "bot", None)
    checker = getattr(bot, "is_owner", None)
    if not callable(checker):
        return False
    try:
        result = checker(getattr(ctx, "author", None))
        if hasattr(result, "__await__"):
            result = await result
    except Exception:
        return False
    return bool(result)


async def check_admin_or_illuminaughty(ctx: commands.Context) -> bool:
    """True for the bot owner, a guild administrator, or the illuminaughty role.

    With no guild, only the owner check can pass. A missing or broken
    ``is_owner`` is denied.
    """
    if await _caller_is_bot_owner(ctx):
        return True
    if getattr(ctx, "guild", None) is None:
        return False

    author = getattr(ctx, "author", None)
    if author is None:
        return False

    permissions = getattr(author, "guild_permissions", None)
    if getattr(permissions, "administrator", False):
        return True

    roles = getattr(author, "roles", None) or ()
    for role in roles:
        if getattr(role, "id", None) == MTS_ILLUMINAUGHTY_ROLE_ID:
            return True
        role_name = getattr(role, "name", None)
        if isinstance(role_name, str) and role_name.casefold() == "illuminaughty":
            return True
    return False


def is_admin_or_illuminaughty():
    """Prefix check: bot owner, guild administrator, or the illuminaughty role."""

    async def predicate(ctx: commands.Context) -> bool:
        return await check_admin_or_illuminaughty(ctx)

    return commands.check(predicate)


def is_in_guilds(*guild_ids: int):
    def predicate(ctx: GuildContext) -> bool:
        guild = ctx.guild
        if guild is None:
            return False
        return guild.id in guild_ids

    return commands.check(predicate)


def _guild_has_palworld_enabled(guild_id: int | None) -> bool:
    if guild_id is None:
        return False
    config = get_guild_config(guild_id)
    return config is not None and config.enabled and config.palworld_enabled


def is_palworld_guild():
    """Restrict prefix commands to guilds with palworld.enabled in server_configs."""

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        if await ctx.bot.is_owner(ctx.author):
            return True
        return _guild_has_palworld_enabled(ctx.guild.id)

    return commands.check(predicate)


async def ensure_palworld_guild(interaction) -> bool:
    """Return True when the interaction guild has Palworld enabled in server_configs."""
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return False

    if await interaction.client.is_owner(interaction.user):
        return True

    if not _guild_has_palworld_enabled(interaction.guild.id):
        await interaction.response.send_message(
            "Palworld is not enabled for this server. Set `palworld.enabled` in server_configs.",
            ephemeral=True,
        )
        return False

    return True