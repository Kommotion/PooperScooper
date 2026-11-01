"""Malfoyd of the day.

An external job writes ``data/malfoyd_queue.json``. Once a minute this cog
posts today's pick into the Malfoyd thread when that file is for today and
has not been posted yet. The message is plain text plus the vxtwitter link
so Discord unfurls the video. No embeds.

``index`` counts valid candidates only. A candidate is valid when ``vx_url``
is a non-empty string starting with ``https://vxtwitter.com/``. Invalid
entries are skipped and do not take a slot. Extra JSON fields are kept.

Notify toggles the Malfoyd Ping role. Reroll moves to the next valid
candidate. The whole server shares two rerolls a day. The bot owner can
reroll without using one. ``!malfoyd_setup`` and ``!malfoyd_post`` are
owner-only prefix commands; anyone else gets no reply.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from cogs.utils.constants import DATA_DIR

log = logging.getLogger(__name__)

GUILD_ID = 932057681307512922
THREAD_ID = 1565459865134960811
ROLE_NAME = "Malfoyd Ping"
QUEUE_PATH = DATA_DIR / "malfoyd_queue.json"
SETTINGS_PATH = DATA_DIR / "malfoyd_settings.json"
MAX_REROLLS_PER_DAY = 2
POLL_SECONDS = 60
TZ = ZoneInfo("America/Los_Angeles")

VX_PREFIX = "https://vxtwitter.com/"
HEADING = "**Malfoyd of the day** 🐍"
NOTIFY_CUSTOM_ID = "malfoyd:notify"
REROLL_CUSTOM_ID = "malfoyd:reroll"

_missing_logged: set[str] = set()


def today_pt(now: datetime | None = None) -> str:
    """YYYY-MM-DD in America/Los_Angeles. Naive datetimes are read as Pacific."""
    moment = datetime.now(TZ) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TZ)
    return moment.astimezone(TZ).date().isoformat()


def default_queue() -> dict:
    return {
        "date": None,
        "posted": False,
        "message_id": None,
        "index": 0,
        "rerolls_used": 0,
        "candidates": [],
    }


def default_settings() -> dict:
    return {}


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
            log.warning("Malfoyd file %s is missing; using defaults.", target)
        return JsonRead(default_factory(), "missing")
    try:
        parsed = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        log.error("Malfoyd file %s is corrupt: %s", target, exc)
        return JsonRead(default_factory(), "corrupt")
    if not isinstance(parsed, dict):
        log.error("Malfoyd file %s is corrupt: expected an object.", target)
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


def is_valid_candidate(candidate: object) -> bool:
    if not isinstance(candidate, dict):
        return False
    url = candidate.get("vx_url")
    return isinstance(url, str) and url.startswith(VX_PREFIX)


def valid_candidates(candidates: object) -> list[dict]:
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if is_valid_candidate(item)]


def _non_negative_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _candidate_at(queue: dict, index: int) -> dict | None:
    chosen = valid_candidates(queue.get("candidates"))
    if index < 0 or index >= len(chosen):
        return None
    return chosen[index]


def current_candidate(queue: dict) -> dict | None:
    if not isinstance(queue, dict):
        return None
    return _candidate_at(queue, _non_negative_int(queue.get("index")))


def _settings_role_id(settings: object) -> int | None:
    if not isinstance(settings, dict) or "role_id" not in settings:
        return None
    value = settings.get("role_id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def find_role(guild: object, settings: object) -> object | None:
    """Role from settings.role_id, otherwise the first role named Malfoyd Ping."""
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


def build_content(candidate: dict, role_id: int | None, mention: bool) -> str:
    """Plain-text Malfoyd post. The role mention is only the first line when requested."""
    url = candidate["vx_url"]
    lines: list[str] = []
    if mention and role_id is not None:
        lines.append(f"<@&{role_id}>")
    lines.append(HEADING)
    lines.append(url)
    return "\n".join(lines)


def _same_id(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    return str(left) == str(right)


def _member_has_role(member: object, role: object) -> bool:
    role_id = _role_id(role)
    for item in getattr(member, "roles", None) or []:
        if item is role or _role_id(item) == role_id:
            return True
    return False


def _malfoyd_cog(interaction: discord.Interaction) -> Malfoyd | None:
    client = getattr(interaction, "client", None)
    getter = getattr(client, "get_cog", None)
    if not callable(getter):
        return None
    cog = getter("Malfoyd")
    return cog if isinstance(cog, Malfoyd) else None


class MalfoydView(discord.ui.View):
    """Persistent notify and reroll buttons. Callbacks read the queue, not the label."""

    def __init__(self, remaining: int):
        super().__init__(timeout=None)
        left = remaining if isinstance(remaining, int) and not isinstance(remaining, bool) else 0
        self.reroll.label = f"Reroll ({left} left)"
        self.reroll.disabled = left <= 0

    @discord.ui.button(label="Notify me", emoji="🔔", custom_id=NOTIFY_CUSTOM_ID)
    async def notify(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        cog = _malfoyd_cog(interaction)
        if cog is None:
            await interaction.response.send_message("Malfoyd isn't available right now.", ephemeral=True)
            return
        await cog.handle_notify(interaction)

    @discord.ui.button(label="Reroll (2 left)", emoji="🎲", custom_id=REROLL_CUSTOM_ID)
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        cog = _malfoyd_cog(interaction)
        if cog is None:
            await interaction.response.send_message("Malfoyd isn't available right now.", ephemeral=True)
            return
        await cog.handle_reroll(interaction)


def make_view(remaining: int) -> MalfoydView:
    return MalfoydView(remaining)


def _command_is_malfoyd(command: object) -> bool:
    return getattr(command, "name", None) in {"malfoyd_post", "malfoyd_setup"}


class Malfoyd(commands.Cog):
    """Posts the daily Malfoyd pick and handles notify / reroll."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        queue_path: str | Path | None = None,
        settings_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.bot = bot
        self.queue_path = Path(QUEUE_PATH if queue_path is None else queue_path)
        self.settings_path = Path(SETTINGS_PATH if settings_path is None else settings_path)
        self._clock = clock
        self._lock = asyncio.Lock()

    def _today(self) -> str:
        if self._clock is None:
            return today_pt()
        return today_pt(self._clock())

    def _guild(self, source: object | None = None):
        if source is not None:
            guild = getattr(source, "guild", None)
            if getattr(guild, "id", None) == GUILD_ID:
                return guild
        getter = getattr(self.bot, "get_guild", None)
        if callable(getter):
            return getter(GUILD_ID)
        return None

    def _settings(self) -> dict:
        loaded = read_json(self.settings_path, default_settings)
        if loaded.status != "ok":
            return {}
        return loaded.data

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
            log.exception("Malfoyd owner check failed")
            return False

    async def _ephemeral(self, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.response.send_message(content, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Malfoyd reply failed")

    async def cog_load(self) -> None:
        self.bot.add_view(make_view(MAX_REROLLS_PER_DAY))
        if not self.poll_loop.is_running():
            self.poll_loop.start()
        log.info("Malfoyd loaded guild=%s thread=%s", GUILD_ID, THREAD_ID)

    async def cog_unload(self) -> None:
        self.poll_loop.cancel()

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        # CheckFailure on these commands is someone who is not the owner.
        # Swallow it so Discord and the error logger stay quiet.
        if not _command_is_malfoyd(getattr(ctx, "command", None)):
            log.error("Malfoyd command failed", exc_info=error)
            return
        if isinstance(error, commands.CheckFailure):
            return
        log.error("Malfoyd command failed", exc_info=error)

    @tasks.loop(seconds=POLL_SECONDS)
    async def poll_loop(self) -> None:
        try:
            await self.poll_once()
        except Exception:
            log.exception("Malfoyd poll failed")

    @poll_loop.before_loop
    async def before_poll_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def poll_once(self) -> str | None:
        async with self._lock:
            return await self._post_locked(force=False)

    async def _open_thread(self) -> tuple[object | None, str | None]:
        thread = None
        guild = self._guild()
        if guild is not None:
            getter = getattr(guild, "get_thread", None)
            if callable(getter):
                thread = getter(THREAD_ID)
        if thread is None:
            getter = getattr(self.bot, "get_channel", None)
            if callable(getter):
                thread = getter(THREAD_ID)
        if thread is None:
            fetcher = getattr(self.bot, "fetch_channel", None)
            if callable(fetcher):
                try:
                    thread = await fetcher(THREAD_ID)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    log.error("Malfoyd thread %s could not be fetched: %s", THREAD_ID, exc)
                    return None, "missing"
        if thread is None:
            log.error("Malfoyd thread %s was not found.", THREAD_ID)
            return None, "missing"
        if getattr(thread, "archived", False):
            try:
                await thread.edit(archived=False)
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Failed to unarchive Malfoyd thread %s", THREAD_ID)
                return None, "unarchive"
        return thread, None

    async def _post_locked(self, *, force: bool) -> str | None:
        loaded = read_json(self.queue_path, default_queue)
        if loaded.status == "corrupt":
            return "The queue file couldn't be read." if force else None
        if loaded.status == "missing":
            return "No valid pick in the queue." if force else None
        queue = loaded.data
        if queue.get("date") != self._today():
            return "The queue is not for today." if force else None
        if not force and queue.get("posted") is True:
            return None
        candidate = current_candidate(queue)
        if candidate is None:
            return "No valid pick in the queue." if force else None
        thread, problem = await self._open_thread()
        if thread is None:
            if not force:
                return None
            if problem == "unarchive":
                return "Couldn't unarchive the Malfoyd thread."
            return "Couldn't find the Malfoyd thread."
        role = find_role(self._guild(), self._settings())
        role_id = _role_id(role)
        content = build_content(candidate, role_id, role is not None)
        if role is not None:
            allowed = discord.AllowedMentions(everyone=False, users=False, roles=[role])
        else:
            allowed = discord.AllowedMentions.none()
        remaining = MAX_REROLLS_PER_DAY - _non_negative_int(queue.get("rerolls_used"))
        try:
            message = await thread.send(
                content,
                view=make_view(remaining),
                allowed_mentions=allowed,
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Malfoyd post to thread %s failed", THREAD_ID)
            return "Couldn't post Malfoyd of the day." if force else None
        queue["posted"] = True
        queue["message_id"] = int(message.id)
        write_json(self.queue_path, queue)
        log.info("Posted Malfoyd of the day message_id=%s", message.id)
        return f"Posted Malfoyd of the day ({message.id})."

    async def handle_notify(self, interaction: discord.Interaction) -> None:
        guild = self._guild(interaction)
        role = find_role(guild, self._settings())
        if guild is None or role is None:
            await self._ephemeral(interaction, "Signups aren't set up yet.")
            return
        member = await self._member_for(guild, interaction.user)
        if member is None:
            log.error("Malfoyd notify could not find member %s", getattr(interaction.user, "id", None))
            await self._ephemeral(interaction, "Couldn't update your notify role.")
            return
        removing = _member_has_role(member, role)
        try:
            if removing:
                await member.remove_roles(role, reason="Malfoyd notify off")
            else:
                await member.add_roles(role, reason="Malfoyd notify on")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Malfoyd notify failed for user %s", getattr(interaction.user, "id", None))
            await self._ephemeral(interaction, "Couldn't update your notify role.")
            return
        if removing:
            await self._ephemeral(interaction, "You won't be pinged anymore.")
        else:
            await self._ephemeral(interaction, "You'll be pinged for Malfoyd of the day 🐍")

    async def _member_for(self, guild: object, user: object):
        user_guild = getattr(getattr(user, "guild", None), "id", None)
        if user_guild == GUILD_ID and callable(getattr(user, "add_roles", None)):
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
            log.exception("Malfoyd could not fetch member %s", user_id)
            return None

    async def handle_reroll(self, interaction: discord.Interaction) -> None:
        # Held across the owner lookup. That await would otherwise let two
        # clicks read the same rerolls_used and both write it back.
        async with self._lock:
            await self._reroll_locked(interaction)

    async def _reroll_locked(self, interaction: discord.Interaction) -> None:
        loaded = read_json(self.queue_path, default_queue)
        queue = loaded.data
        message = getattr(interaction, "message", None)
        message_id = getattr(message, "id", None)
        if (
            loaded.status != "ok"
            or not _same_id(message_id, queue.get("message_id"))
            or queue.get("date") != self._today()
        ):
            await self._ephemeral(interaction, "That pick has expired.")
            return

        owner = await self._user_is_owner(getattr(interaction, "user", None))
        index = _non_negative_int(queue.get("index"))
        used = _non_negative_int(queue.get("rerolls_used"))
        nxt = _candidate_at(queue, index + 1)
        if not owner and used >= MAX_REROLLS_PER_DAY:
            await self._ephemeral(interaction, "No rerolls left today")
            return
        if nxt is None:
            await self._ephemeral(interaction, "No more picks today")
            return

        previous_index = queue.get("index", 0)
        previous_used = queue.get("rerolls_used", 0)
        queue["index"] = index + 1
        if not owner:
            queue["rerolls_used"] = used + 1
        try:
            write_json(self.queue_path, queue)
        except OSError:
            log.exception("Malfoyd queue save failed")
            await self._ephemeral(interaction, "Couldn't update that pick.")
            return

        role_id = _role_id(find_role(self._guild(interaction), self._settings()))
        remaining = MAX_REROLLS_PER_DAY - _non_negative_int(queue.get("rerolls_used"))
        try:
            await interaction.response.edit_message(
                content=build_content(nxt, role_id, False),
                view=make_view(remaining),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Malfoyd reroll edit failed")
            queue["index"] = previous_index
            queue["rerolls_used"] = previous_used
            try:
                write_json(self.queue_path, queue)
            except OSError:
                log.exception("Malfoyd queue revert failed")
            done = getattr(getattr(interaction, "response", None), "is_done", None)
            if not callable(done) or not done():
                await self._ephemeral(interaction, "Couldn't update that pick.")

    @commands.command(name="malfoyd_post", hidden=True)
    @commands.is_owner()
    async def malfoyd_post(self, ctx: commands.Context) -> None:
        """Owner-only. Post the current Malfoyd queue now."""
        if not await self._user_is_owner(getattr(ctx, "author", None)):
            return
        async with self._lock:
            result = await self._post_locked(force=True)
        await ctx.reply(result or "Couldn't post Malfoyd of the day.", mention_author=False)

    @commands.command(name="malfoyd_setup", hidden=True)
    @commands.is_owner()
    async def malfoyd_setup(self, ctx: commands.Context) -> None:
        """Owner-only. Get or create the Malfoyd Ping role and save its id."""
        if not await self._user_is_owner(getattr(ctx, "author", None)):
            return
        guild = self._guild(ctx)
        if guild is None:
            fetcher = getattr(self.bot, "fetch_guild", None)
            if callable(fetcher):
                try:
                    guild = await fetcher(GUILD_ID)
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("Malfoyd setup could not fetch guild %s", GUILD_ID)
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
                    reason="Malfoyd of the day signups",
                )
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Malfoyd setup could not create the role")
                await ctx.reply("Couldn't create the Malfoyd Ping role.", mention_author=False)
                return
        loaded = read_json(self.settings_path, default_settings)
        data = dict(loaded.data) if loaded.status == "ok" else {}
        data["role_id"] = int(role.id)
        write_json(self.settings_path, data)
        await ctx.reply(str(role.id), mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Malfoyd(bot))
