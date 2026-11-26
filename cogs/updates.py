"""Owner-only PooperScooper changelog announcements.

``!announce`` posts into the configured target channel and pings the
Pooperscooper Updates role. With no settings file, that target is MTS
#announcements. ``!announce preview`` posts the same body into the
support-server test channel with no ping. Both messages carry a
persistent Notify me button that toggles the role in the guild the
message is in. ``!announce_setup`` gets or creates the role in the
configured target guild. There are no slash commands.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import discord
from discord.ext import commands

from cogs.utils.constants import DATA_DIR

log = logging.getLogger(__name__)

GUILD_ID = 932057681307512922
ANNOUNCEMENTS_CHANNEL_ID = 1019352642977267743
TEST_CHANNEL_ID = 1045149015756521493
ROLE_NAME = "Pooperscooper Updates"
SETTINGS_PATH = DATA_DIR / "updates_settings.json"
NOTIFY_CUSTOM_ID = "updates:notify"
CONTENT_LIMIT = 2000
EMBED_LIMIT = 4096

_OWNER_COMMANDS = {"announce_setup", "announce", "announce preview"}
_missing_logged: set[str] = set()


def default_settings() -> dict:
    return {"guild_id": GUILD_ID, "channel_id": ANNOUNCEMENTS_CHANNEL_ID}


@dataclass(frozen=True)
class JsonRead:
    data: dict
    status: str


def read_json(path: str | Path, default_factory: Callable[[], dict]) -> JsonRead:
    """Load a JSON object. BOM is accepted. Missing and corrupt files do not raise."""
    target = Path(path)
    if not target.is_file():
        key = str(target)
        if key not in _missing_logged:
            _missing_logged.add(key)
            log.warning("Updates file %s is missing; using defaults.", target)
        return JsonRead(default_factory(), "missing")
    try:
        parsed = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        log.error("Updates file %s is corrupt: %s", target, exc)
        return JsonRead(default_factory(), "corrupt")
    if not isinstance(parsed, dict):
        log.error("Updates file %s is corrupt: expected an object.", target)
        return JsonRead(default_factory(), "corrupt")
    return JsonRead(parsed, "ok")


def write_json(path: str | Path, data: dict) -> None:
    """Atomic UTF-8 write with no BOM. A temp file is replaced into place."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    if not payload.endswith("\n"):
        payload += "\n"
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _settings_role_id(settings: object) -> int | None:
    if not isinstance(settings, dict) or "role_id" not in settings:
        return None
    value = settings.get("role_id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _valid_snowflake(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _valid_channel_id(value: object) -> int | None:
    return _valid_snowflake(value)


def _settings_guild_id(settings: object) -> int:
    """Saved target guild when it is a non-bool int above 0, otherwise MTS."""
    if isinstance(settings, dict):
        found = _valid_snowflake(settings.get("guild_id"))
        if found is not None:
            return found
    return GUILD_ID


def _settings_channel_id(settings: object) -> int:
    """Saved channel when it is a non-bool int above 0, otherwise the MTS announcements channel."""
    if isinstance(settings, dict):
        found = _valid_snowflake(settings.get("channel_id"))
        if found is not None:
            return found
    return ANNOUNCEMENTS_CHANNEL_ID


def find_role(guild: object, settings: object) -> object | None:
    """Role from settings.role_id, otherwise the first role named Pooperscooper Updates."""
    if guild is None:
        return None
    role_id = _settings_role_id(settings)
    if role_id is not None:
        getter = getattr(guild, "get_role", None)
        role = getter(role_id) if callable(getter) else None
        if role is not None:
            return role
    roles = getattr(guild, "roles", None) or []
    for role in roles:
        if getattr(role, "name", None) == ROLE_NAME:
            return role
    return None


def _role_id(role: object) -> int | None:
    value = getattr(role, "id", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _member_has_role(member: object, role: object) -> bool:
    role_id = _role_id(role)
    for item in getattr(member, "roles", None) or []:
        if item is role or _role_id(item) == role_id:
            return True
    return False


def _command_is_updates(command: object) -> bool:
    qualified = getattr(command, "qualified_name", None)
    return isinstance(qualified, str) and qualified in _OWNER_COMMANDS


class UpdatesNotifyView(discord.ui.View):
    """Persistent Notify me button. The callback delegates to the loaded cog."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Notify me", emoji="🔔", custom_id=NOTIFY_CUSTOM_ID)
    async def notify(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        cog = interaction.client.get_cog("Updates")
        if not isinstance(cog, Updates):
            await interaction.response.send_message("Updates aren't available right now.", ephemeral=True)
            return
        await cog.handle_notify(interaction)


def make_view() -> UpdatesNotifyView:
    return UpdatesNotifyView()


class Updates(commands.Cog):
    """Posts changelog announcements and toggles the updates role."""

    def __init__(self, bot: commands.Bot, *, settings_path: str | Path | None = None):
        self.bot = bot
        self.settings_path = Path(SETTINGS_PATH if settings_path is None else settings_path)

    def _target_guild_id(self) -> int:
        return _settings_guild_id(self._settings())

    def _guild(self, source: object | None = None):
        target_id = self._target_guild_id()
        if source is not None:
            guild = getattr(source, "guild", None)
            if getattr(guild, "id", None) == target_id:
                return guild
        getter = getattr(self.bot, "get_guild", None)
        if callable(getter):
            return getter(target_id)
        return None

    def _settings(self) -> dict:
        loaded = read_json(self.settings_path, default_settings)
        if loaded.status == "ok":
            return loaded.data
        return default_settings()

    async def _user_is_owner(self, user: object) -> bool:
        if user is None:
            return False
        checker = getattr(self.bot, "is_owner", None)
        if not callable(checker):
            return False
        try:
            result = checker(user)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            log.exception("Updates owner check failed")
            return False

    async def _ephemeral(self, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.response.send_message(content, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Updates reply failed")

    async def cog_load(self) -> None:
        self.bot.add_view(make_view())

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        # CheckFailure on these commands is someone who is not the owner.
        # Swallow it so Discord and the error logger stay quiet.
        if not _command_is_updates(getattr(ctx, "command", None)):
            log.error("Updates command failed", exc_info=error)
            return
        if isinstance(error, commands.CheckFailure):
            return
        log.error("Updates command failed", exc_info=error)

    def _interaction_guild(self, interaction: object):
        """Guild the button was clicked in, not necessarily the configured target."""
        guild = getattr(interaction, "guild", None)
        if guild is not None:
            return guild
        guild_id = getattr(interaction, "guild_id", None)
        getter = getattr(self.bot, "get_guild", None)
        if callable(getter) and guild_id is not None:
            return getter(guild_id)
        return None

    async def handle_notify(self, interaction: discord.Interaction) -> None:
        guild = self._interaction_guild(interaction)
        role = find_role(guild, self._settings())
        if guild is None or role is None:
            await self._ephemeral(interaction, "Signups aren't set up yet.")
            return
        member = await self._member_for(guild, interaction.user)
        if member is None:
            log.error("Updates notify could not find member %s", getattr(interaction.user, "id", None))
            await self._ephemeral(interaction, "Couldn't update your notify role.")
            return
        removing = _member_has_role(member, role)
        try:
            if removing:
                await member.remove_roles(role, reason="Updates notify off")
            else:
                await member.add_roles(role, reason="Updates notify on")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Updates notify failed for user %s", getattr(interaction.user, "id", None))
            await self._ephemeral(interaction, "Couldn't update your notify role.")
            return
        if removing:
            await self._ephemeral(interaction, "You won't be pinged anymore.")
        else:
            await self._ephemeral(interaction, "You'll be pinged for PooperScooper updates 🐶💩")

    async def _member_for(self, guild: object, user: object):
        user_guild = getattr(getattr(user, "guild", None), "id", None)
        if user_guild == getattr(guild, "id", None) and callable(getattr(user, "add_roles", None)):
            return user
        user_id = getattr(user, "id", None)
        getter = getattr(guild, "get_member", None)
        member = getter(user_id) if callable(getter) else None
        if member is not None:
            return member
        fetcher = getattr(guild, "fetch_member", None)
        if not callable(fetcher) or user_id is None:
            return None
        try:
            return await fetcher(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.exception("Updates could not fetch member %s", user_id)
            return None

    async def _resolve_channel(self, channel_id: int):
        getter = getattr(self.bot, "get_channel", None)
        channel = getter(channel_id) if callable(getter) else None
        if channel is not None:
            return channel
        fetcher = getattr(self.bot, "fetch_channel", None)
        if not callable(fetcher):
            return None
        try:
            return await fetcher(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.exception("Updates could not fetch channel %s", channel_id)
            return None

    async def _read_body(self, ctx: commands.Context, text: str) -> tuple[str | None, str | None]:
        """Return ``(body, error)``. A ``.txt`` attachment wins over command text."""
        message = getattr(ctx, "message", None)
        attachments = getattr(message, "attachments", None) or []
        for attachment in attachments:
            filename = getattr(attachment, "filename", None)
            if not isinstance(filename, str) or not filename.lower().endswith(".txt"):
                continue
            try:
                raw = await attachment.read()
            except (OSError, discord.HTTPException):
                log.exception("Updates could not read the announcement file")
                return None, "Couldn't read that .txt file."
            try:
                if isinstance(raw, str):
                    body = raw
                else:
                    body = bytes(raw).decode("utf-8-sig")
            except (UnicodeError, TypeError, ValueError):
                log.exception("Updates could not read the announcement file")
                return None, "Couldn't read that .txt file."
            if body == "":
                return None, "Need some announcement text, or a .txt file."
            return body, None
        stripped = text.strip() if isinstance(text, str) else ""
        if stripped == "":
            return None, "Need some announcement text, or a .txt file."
        return stripped, None

    async def _send_one(
        self,
        ctx: commands.Context,
        channel: object,
        content: str | None,
        *,
        embed: discord.Embed | None,
        view: discord.ui.View | None,
        allowed_mentions: discord.AllowedMentions,
    ):
        kwargs: dict = {"allowed_mentions": allowed_mentions}
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        try:
            return await channel.send(content, **kwargs)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Updates post could not be completed")
            try:
                await ctx.reply("Couldn't complete that post.", mention_author=False)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Updates reply failed")
            return None

    async def _send_parts(self, ctx: commands.Context, channel: object, prefix: str, body: str, role: object | None):
        """Split a live or preview post. The role mention and the button stay on the first message."""
        if role is not None:
            first_mentions = discord.AllowedMentions(everyone=False, users=False, roles=[role])
        else:
            first_mentions = discord.AllowedMentions.none()
        if len(prefix + body) <= CONTENT_LIMIT:
            first_content: str | None = prefix + body
            embed = None
            followups: list[str] = []
        else:
            first_content = prefix.rstrip("\n") if prefix else None
            if len(body) <= EMBED_LIMIT:
                embed = discord.Embed(description=body)
                followups = []
            else:
                embed = discord.Embed(description=body[:EMBED_LIMIT])
                rest = body[EMBED_LIMIT:]
                followups = [rest[index:index + CONTENT_LIMIT] for index in range(0, len(rest), CONTENT_LIMIT)]
        first = await self._send_one(
            ctx,
            channel,
            first_content,
            embed=embed,
            view=make_view(),
            allowed_mentions=first_mentions,
        )
        if first is None:
            return None
        silent = discord.AllowedMentions.none()
        for chunk in followups:
            sent = await self._send_one(
                ctx,
                channel,
                chunk,
                embed=None,
                view=None,
                allowed_mentions=silent,
            )
            if sent is None:
                return None
        return first

    async def _post(self, ctx: commands.Context, text: str, *, preview: bool) -> None:
        body, problem = await self._read_body(ctx, text)
        if problem is not None:
            await ctx.reply(problem, mention_author=False)
            return
        if preview:
            channel = await self._resolve_channel(TEST_CHANNEL_ID)
            if channel is None:
                await ctx.reply("Couldn't find the test channel.", mention_author=False)
                return
            role = None
            prefix = ""
            kind = "preview"
        else:
            channel = await self._resolve_channel(_settings_channel_id(self._settings()))
            if channel is None:
                await ctx.reply("Couldn't find the announcements channel.", mention_author=False)
                return
            role = find_role(self._guild(), self._settings())
            role_id = _role_id(role) if role is not None else None
            if role is None or role_id is None:
                await ctx.reply("Run !announce_setup first.", mention_author=False)
                return
            prefix = f"<@&{role_id}>\n"
            kind = "announcement"
        first = await self._send_parts(ctx, channel, prefix, body or "", role)
        if first is None:
            return
        await ctx.reply(f"Posted the {kind} ({first.id}).", mention_author=False)

    @commands.command(name="announce_setup", hidden=True)
    @commands.is_owner()
    async def announce_setup(self, ctx: commands.Context) -> None:
        """Owner-only. Get or create the Pooperscooper Updates role and save its id."""
        if not await self._user_is_owner(ctx.author):
            return
        guild = self._guild(ctx)
        if guild is None:
            fetcher = getattr(self.bot, "fetch_guild", None)
            if callable(fetcher):
                try:
                    guild = await fetcher(self._target_guild_id())
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Updates setup could not fetch guild %s", self._target_guild_id())
                    guild = None
        if guild is None:
            await ctx.reply("Couldn't find the guild.", mention_author=False)
            return
        role = find_role(guild, self._settings())
        if role is None:
            try:
                role = await guild.create_role(
                    name=ROLE_NAME,
                    mentionable=True,
                    permissions=discord.Permissions.none(),
                    reason="PooperScooper updates signups",
                )
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Updates setup could not create the role")
                await ctx.reply("Couldn't create the Pooperscooper Updates role.", mention_author=False)
                return
        loaded = read_json(self.settings_path, default_settings)
        data = dict(loaded.data) if loaded.status == "ok" else default_settings()
        data["role_id"] = int(role.id)
        data["guild_id"] = int(guild.id)
        if _valid_channel_id(data.get("channel_id")) is None:
            data["channel_id"] = ANNOUNCEMENTS_CHANNEL_ID
        write_json(self.settings_path, data)
        await ctx.reply(str(role.id), mention_author=False)

    @commands.group(name="announce", hidden=True, invoke_without_command=True)
    @commands.is_owner()
    async def announce(self, ctx: commands.Context, *, text: str = "") -> None:
        """Owner-only. Post a live announcement that pings Pooperscooper Updates."""
        if not await self._user_is_owner(ctx.author):
            return
        await self._post(ctx, text, preview=False)

    @announce.command(name="preview", hidden=True)
    @commands.is_owner()
    async def announce_preview(self, ctx: commands.Context, *, text: str = "") -> None:
        """Owner-only. Post the announcement to the test channel without a ping."""
        if not await self._user_is_owner(ctx.author):
            return
        await self._post(ctx, text, preview=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Updates(bot))
