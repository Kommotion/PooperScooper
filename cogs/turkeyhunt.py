"""Thanksgiving turkey hunt.

The cog is always loaded so owner admin commands work before the event.
``thanksgiving_enabled`` gates the live schedule: kickoff, drops, recaps, the
winner post, cleanup, and the avatar. Test drops and simulations still run
while that switch is off. A local inbox under ``data/turkey_hunt_ctl`` is
polled on the same loop, including while the live event is off.
This module does not publish slash commands.
"""

from __future__ import annotations

import logging
import random
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.candyhunt import MTS_GUILD_ID, NICK_ID, SUPPORT_GUILD_ID
from cogs.hunt.book import DeleteRef, HuntBook, due_summary, parse_dt
from cogs.hunt.copy import (
    drop_text,
    format_schedule_summary,
    is_deletable_hunt_message,
    leaderboard_text,
    status_text,
)
from cogs.hunt.ctl import (
    DEFAULT_TEST_CHANNEL_ID,
    REJECT_TEXT,
    ClaimedFile,
    claim_pending,
    default_ctl_root,
    discard_claimed,
    result_exists,
    write_result,
)
from cogs.hunt.engine import HuntEngine
from cogs.hunt.thanksgiving import ThanksgivingTheme, TurkeySettings, load_turkey_settings
from cogs.utils.config import thanksgiving_enabled_from_config
from cogs.utils.constants import DATA_DIR

log = logging.getLogger(__name__)

DEFAULT_PATH = DATA_DIR / "turkey_hunt.json"
CLAIM_TEMPLATE = r"turkey:claim:(?P<drop_id>[0-9a-f]{8})"
_NONE = discord.AllowedMentions.none()
_HARVEST = discord.Colour(0xC65D12)
_DRY_RUN_TRUE = {"true", "yes", "1", "dry_run=true", "preview"}
_DRY_RUN_FALSE = {"false", "no", "0", "dry_run=false"}

TURKEYADMIN_USAGE = (
    "Usage: !turkeyadmin status | schedule [save] | cleanup [true|false] | "
    "testdrop <channel> | test_session [minutes] [interval] [channel] | "
    "test_session_stop | simulate [iso] | power on|off"
)
TEST_DROP_REFUSED = "Test drops stay out of Menaces to Sobriety and the live drop channel."
TEST_SESSION_REFUSED = "Test sessions stay out of Menaces to Sobriety and the live drop channel."
TEST_SESSION_MTS = "Test sessions stay out of Menaces to Sobriety."
TEST_CHANNEL_UNRESOLVED = "That channel could not be resolved. Nothing was posted."
TEST_DROP_SEND_FAILED = "The test drop did not send. Live totals were left alone."
TEST_DROP_POSTED = "Test turkey posted in <#{channel_id}>. It does not count on the live board."
TEST_SESSION_STARTED = (
    "Test session started in <#{channel_id}> for {minutes} minutes, "
    "about every {interval} minutes. Claims go to the test tally."
)
TEST_SESSION_BUSY = "A test session is already running."
TEST_SESSION_RANGE = "Usage: minutes must be 1-240 and interval must be 1-60."
TEST_SESSION_STOPPED = "Test session stopped. Live totals were left alone."
TEST_SESSION_IDLE = "No test session is running."
TEST_SESSION_NEEDS_CHANNEL = "Give a channel outside Menaces to Sobriety."


def _parse_dry_run(value: object) -> bool | None:
    """Omitted means preview. Only a false token deletes or posts for real."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().casefold()
        if token in _DRY_RUN_TRUE:
            return True
        if token in _DRY_RUN_FALSE:
            return False
    return None


def _parse_power(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip().casefold()
    if token in {"on", "off"}:
        return token
    return None


def _parse_session_int(value: object, default: int) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    return int(text)


def _parse_channel_token(token: object) -> int | None:
    if isinstance(token, bool):
        return None
    if isinstance(token, int):
        return token if token > 0 else None
    text = str(token).strip()
    if text.startswith("<#") and text.endswith(">"):
        text = text[2:-1]
    if not text.isdigit():
        return None
    value = int(text)
    return value if value > 0 else None


def _parse_admin_moment(value: object, zone) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=zone)
    return moment


def _command_is_turkeyadmin(command) -> bool:
    seen: set[int] = set()
    current = command
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "parent", None) is None:
            return getattr(current, "name", None) == "turkeyadmin"
        current = getattr(current, "parent", None)
    return False


async def _turkeyadmin_allowed(ctx) -> bool:
    """Owner, and only in a turkey guild or a DM. Anyone else gets no reply."""
    author = getattr(ctx, "author", None)
    author_id = getattr(author, "id", None)
    cog = getattr(ctx, "cog", None)
    if cog is None:
        cog = getattr(getattr(ctx, "command", None), "cog", None)
    settings = getattr(cog, "settings", None)
    owner_id = getattr(settings, "owner_id", NICK_ID)
    if author_id != owner_id:
        is_owner = getattr(getattr(ctx, "bot", None), "is_owner", None)
        allowed = False
        if callable(is_owner):
            try:
                allowed = bool(await is_owner(author))
            except Exception:
                allowed = False
        if not allowed:
            return False
    guild = getattr(ctx, "guild", None)
    if guild is None:
        return True
    guild_id = getattr(guild, "id", None)
    if settings is None:
        return guild_id in {MTS_GUILD_ID, SUPPORT_GUILD_ID}
    return guild_id in {int(settings.guild_id), int(settings.support_guild_id)}


_turkeyadmin_check = commands.check(_turkeyadmin_allowed)


def _message_blob(message) -> str:
    parts: list[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, str) and content:
        parts.append(content)
    _walk_bits(getattr(message, "components", None), parts, set())
    _walk_bits(getattr(message, "embeds", None), parts, set())
    return "\n".join(parts)


def _walk_bits(node, parts: list[str], seen: set[int]) -> None:
    if node is None or isinstance(node, (int, float, bool)):
        return
    if isinstance(node, str):
        if node:
            parts.append(node)
        return
    identity = id(node)
    if identity in seen:
        return
    seen.add(identity)
    if isinstance(node, dict):
        for key in ("content", "description", "title", "label", "custom_id", "name", "value"):
            value = node.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        for value in node.values():
            if isinstance(value, (str, int, float, bool)) or value is None:
                continue
            _walk_bits(value, parts, seen)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _walk_bits(item, parts, seen)
        return
    for attr in ("content", "description", "title", "label", "custom_id"):
        value = getattr(node, attr, None)
        if isinstance(value, str) and value:
            parts.append(value)
    for attr in ("children", "components", "embeds", "fields"):
        child = getattr(node, attr, None)
        if child:
            _walk_bits(child, parts, seen)


def build_drop_view(drop_id: str, text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=_HARVEST)
    container.add_item(discord.ui.TextDisplay(text))
    row = discord.ui.ActionRow()
    row.add_item(TurkeyClaimButton(drop_id))
    container.add_item(row)
    view.add_item(container)
    return view


def build_caught_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=180)
    container = discord.ui.Container(accent_colour=_HARVEST)
    container.add_item(discord.ui.TextDisplay(text))
    view.add_item(container)
    return view


class TurkeyClaimButton(discord.ui.DynamicItem[discord.ui.Button], template=CLAIM_TEMPLATE):
    def __init__(self, drop_id: str):
        super().__init__(
            discord.ui.Button(
                label="Grab the turkey",
                emoji="🦃",
                style=discord.ButtonStyle.primary,
                custom_id=f"turkey:claim:{drop_id}",
            )
        )
        self.drop_id = drop_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match):
        return cls(match.group("drop_id"))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("TurkeyHunt")
        if not isinstance(cog, TurkeyHunt):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "The turkey hunt isn't available right now.",
                    ephemeral=True,
                    allowed_mentions=_NONE,
                )
            return
        await cog.handle_claim(interaction, self.drop_id)


class TurkeyHunt(commands.Cog):
    """Thanksgiving drops, claims, recaps, and the Monday winner post."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        path: str | Path | None = None,
        settings: TurkeySettings | None = None,
        rng: random.Random | None = None,
        enabled: bool | None = None,
        clock=None,
        ctl_dir: str | Path | None = None,
    ):
        self.bot = bot
        self.settings = settings if settings is not None else load_turkey_settings()
        self.rng = rng if rng is not None else random.Random()
        self._enabled = enabled
        self._clock = clock
        self._did_revert = False
        self.ctl_root = Path(ctl_dir) if ctl_dir is not None else default_ctl_root()
        state_path = Path(path) if path is not None else self.settings.state_path()
        year = self.settings.final_at.astimezone(self.settings.zone).year
        archive = state_path.with_name(f"turkey_hunt_archive_{year}.json")
        self.book = HuntBook(self.settings, state_path, rng=self.rng, archive_path=archive)
        self.theme = ThanksgivingTheme(self.settings)
        self.engine = HuntEngine(self.theme, book=self.book)

    def live_enabled(self) -> bool:
        """Bot-wide switch. ``None`` reads config each call. Missing or unreadable is off."""
        if self._enabled is None:
            return thanksgiving_enabled_from_config()
        return bool(self._enabled)

    def now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return discord.utils.utcnow()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(TurkeyClaimButton)
        if not self.turkey_loop.is_running():
            self.turkey_loop.start()

    async def cog_unload(self) -> None:
        self.turkey_loop.cancel()
        remove = getattr(self.bot, "remove_dynamic_items", None)
        if remove is not None:
            remove(TurkeyClaimButton)

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if not _command_is_turkeyadmin(getattr(ctx, "command", None)):
            log.error("turkey hunt command failed", exc_info=error)
            return
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, commands.UserInputError):
            if not await _turkeyadmin_allowed(ctx):
                return
            try:
                await ctx.reply(TURKEYADMIN_USAGE, mention_author=False, allowed_mentions=_NONE)
            except Exception:
                log.error("turkey admin usage reply failed", exc_info=True)
            return
        log.error("turkey admin command failed", exc_info=error)

    @tasks.loop(seconds=20)
    async def turkey_loop(self) -> None:
        await self.tick()

    @turkey_loop.before_loop
    async def _before_turkey_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def tick(self, now: datetime | None = None) -> None:
        moment = self.now() if now is None else now
        # The inbox is polled before the live-event return, and a failure here
        # does not skip the test-session poll below.
        try:
            await self._poll_ctl(moment)
        except Exception:
            log.exception("turkey ctl poll failed")
        try:
            await self._poll_tests(moment)
        except Exception:
            log.exception("turkey test poll failed")
        if not self.live_enabled():
            return
        if self.book.state.get("final_done") or self.book.state.get("event_disabled"):
            return
        try:
            exists = self.settings.avatar_file().is_file()
            plan = await self.engine.tick(moment, avatar_exists=exists)
            if plan is None:
                return
            await self._execute(plan, moment)
        except Exception:
            log.exception("turkey tick failed")

    async def _execute(self, plan, now: datetime) -> None:
        kicked = False
        if plan.kickoff_text:
            kicked = await self._send_kickoff(plan)
        if plan.avatar == "apply" and (kicked or not plan.kickoff_text):
            await self.apply_avatar()
        elif plan.avatar == "skip_missing":
            log.warning("turkey avatar file missing; kickoff continues without an avatar change")
        for post in plan.posts:
            await self._send_live_drop(post)
        for recap in plan.recaps:
            await self._send_recap(recap)
        if plan.final_text:
            await self._send_final(plan, now)
            return
        for ref in plan.deletes:
            await self.safe_delete(ref)

    async def _channel(self, channel_id: int | None):
        """Look up a channel by id only."""
        if channel_id is None:
            return None
        try:
            cid = int(channel_id)
        except (TypeError, ValueError):
            return None
        getter = getattr(self.bot, "get_channel", None)
        channel = getter(cid) if callable(getter) else None
        if channel is not None:
            return channel
        fetcher = getattr(self.bot, "fetch_channel", None)
        if not callable(fetcher):
            return None
        try:
            return await fetcher(cid)
        except Exception:
            log.info("turkey channel lookup failed channel=%s", cid)
            return None

    async def _send_kickoff(self, plan) -> bool:
        channel = await self._channel(plan.kickoff_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            log.info("turkey kickoff skipped reason=no_channel")
            self.book.undo_kickoff()
            return False
        try:
            message = await channel.send(plan.kickoff_text, allowed_mentions=_NONE)
        except Exception:
            log.exception("turkey kickoff send failed")
            self.book.undo_kickoff()
            return False
        self.book.note_kickoff(int(channel.id), int(message.id))
        return True

    async def _send_live_drop(self, post) -> None:
        channel = await self._channel(post.channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.info("turkey drop skipped reason=no_channel id=%s", post.drop_id)
            self.book.revert_post(post.drop_id)
            return
        view = build_drop_view(post.drop_id, post.text)
        try:
            message = await channel.send(view=view, allowed_mentions=_NONE)
        except Exception:
            log.exception("turkey drop send failed id=%s", post.drop_id)
            self.book.revert_post(post.drop_id)
            return
        self.book.note_message(post.drop_id, int(message.id))

    async def _send_recap(self, recap) -> None:
        channel = await self._channel(recap.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            log.info("turkey recap skipped reason=no_channel day=%s", recap.day)
            self.book.undo_recap(recap.day)
            return
        try:
            message = await channel.send(recap.text, allowed_mentions=_NONE)
        except Exception:
            log.exception("turkey recap send failed day=%s", recap.day)
            self.book.undo_recap(recap.day)
            return
        self.book.note_recap(recap.day, int(channel.id), int(message.id))

    async def _send_final(self, plan, now: datetime) -> None:
        channel = await self._channel(plan.final_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            log.info("turkey winner skipped reason=no_channel")
            return
        try:
            message = await channel.send(plan.final_text, allowed_mentions=_NONE)
        except Exception:
            log.exception("turkey winner send failed")
            return
        should_revert = self.book.commit_final(int(message.id), int(channel.id), now)
        for ref in plan.deletes:
            if ref.message_id == int(message.id):
                continue
            await self.safe_delete(ref)
        if should_revert:
            await self.revert_avatar()

    def _skip_channel(self, channel_id: int, name: str) -> bool:
        try:
            cid = int(channel_id)
        except (TypeError, ValueError):
            return True
        if cid in set(self.settings.cleanup_skip_channel_ids):
            return True
        return "announcement" in (name or "").casefold()

    async def safe_delete(self, ref: DeleteRef) -> bool:
        """Delete one of this bot's hunt messages. User messages and announcements stay."""
        channel = await self._channel(ref.channel_id)
        if channel is None:
            log.info(
                "turkey delete skipped reason=no_channel channel=%s message=%s",
                ref.channel_id,
                ref.message_id,
            )
            return False
        name = str(getattr(channel, "name", "") or "")
        if self._skip_channel(ref.channel_id, name):
            log.info("turkey delete skipped reason=announcement channel=%s", ref.channel_id)
            return False
        fetcher = getattr(channel, "fetch_message", None)
        if not callable(fetcher):
            return False
        try:
            message = await fetcher(int(ref.message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.info(
                "turkey delete fetch failed channel=%s message=%s error=%s",
                ref.channel_id,
                ref.message_id,
                type(exc).__name__,
            )
            return False
        except Exception:
            log.exception("turkey delete fetch failed channel=%s message=%s", ref.channel_id, ref.message_id)
            return False
        author_id = getattr(getattr(message, "author", None), "id", None)
        bot_id = getattr(getattr(self.bot, "user", None), "id", None)
        if not isinstance(bot_id, int) or author_id != bot_id:
            log.info("turkey delete skipped reason=not_bot message=%s", ref.message_id)
            return False
        if not is_deletable_hunt_message(_message_blob(message)):
            log.info("turkey delete skipped reason=not_hunt message=%s", ref.message_id)
            return False
        try:
            await message.delete()
        except discord.NotFound:
            return True
        except Exception as exc:
            log.info(
                "turkey delete failed channel=%s message=%s error=%s",
                ref.channel_id,
                ref.message_id,
                type(exc).__name__,
            )
            return False
        return True

    async def apply_avatar(self) -> None:
        """Snapshot the global avatar, then set the pilgrim. At most once. Off-switch is a no-op."""
        if not self.live_enabled():
            return
        if self.book.state.get("avatar_apply_attempted") or self.book.state.get("avatar_applied"):
            return
        path = self.settings.avatar_file()
        if not path.is_file():
            if not self.book.state.get("avatar_missing_logged"):
                self.book.state["avatar_missing_logged"] = True
                self.book.save()
                log.warning("turkey avatar missing path=%s; skipping the avatar change", path)
            return
        user = getattr(self.bot, "user", None)
        edit = getattr(user, "edit", None)
        if not callable(edit):
            log.info("turkey avatar skipped reason=no_user")
            return
        avatar = getattr(user, "avatar", None)
        had = avatar is not None
        backup_path = None
        if had:
            try:
                raw = await avatar.read()
            except Exception:
                log.exception("turkey avatar snapshot failed")
                return
            dest = self.book.path.parent / self.settings.avatar_backup_name
            try:
                dest.write_bytes(raw)
            except Exception:
                log.exception("turkey avatar backup write failed")
                return
            backup_path = str(dest)
        try:
            png = path.read_bytes()
        except Exception:
            log.exception("turkey avatar read failed")
            return
        self.book.note_avatar_attempted()
        try:
            await edit(avatar=png)
        except Exception:
            log.exception("turkey avatar apply failed")
            return
        self.book.note_avatar_applied(had_global_avatar=had, backup=backup_path)

    async def revert_avatar(self) -> None:
        """Put the snapshotted global avatar back. At most once. Off-switch is a no-op."""
        if not self.live_enabled():
            return
        if self._did_revert or self.book.state.get("avatar_revert_attempted"):
            return
        if not self.book.state.get("avatar_applied"):
            return
        user = getattr(self.bot, "user", None)
        edit = getattr(user, "edit", None)
        if not callable(edit):
            return
        self._did_revert = True
        self.book.note_avatar_reverted()
        try:
            if self.book.state.get("had_global_avatar") and self.book.state.get("avatar_backup"):
                data = Path(self.book.state["avatar_backup"]).read_bytes()
                await edit(avatar=data)
            else:
                await edit(avatar=None)
        except Exception:
            log.exception("turkey avatar revert failed")

    async def handle_claim(self, interaction: discord.Interaction, drop_id: str) -> None:
        user = interaction.user
        name = getattr(user, "display_name", None) or getattr(user, "name", None) or getattr(user, "id", "someone")
        result = await self.book.claim(
            drop_id,
            int(user.id),
            self.now(),
            self.rng,
            name=name,
            is_bot=bool(getattr(user, "bot", False)),
        )
        if result.status == "caught":
            view = build_caught_view(result.text)
            try:
                await interaction.response.edit_message(view=view, allowed_mentions=_NONE)
            except Exception:
                log.exception("turkey catch edit failed drop=%s", drop_id)
            try:
                await user.send(result.dm_text, allowed_mentions=_NONE)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.info("turkey dm failed user=%s error=%s", getattr(user, "id", None), type(exc).__name__)
            except Exception:
                log.exception("turkey dm failed user=%s", getattr(user, "id", None))
            return
        if not interaction.response.is_done():
            await interaction.response.send_message(result.text, ephemeral=True, allowed_mentions=_NONE)
        if result.delete is not None:
            await self.safe_delete(result.delete)

    def _test_channel_block(self, channel) -> str | None:
        gid = getattr(getattr(channel, "guild", None), "id", None)
        try:
            cid = int(getattr(channel, "id", None))
        except (TypeError, ValueError):
            return "channel"
        if gid == int(self.settings.guild_id):
            return "mts"
        if cid == int(self.settings.drop_channel_id):
            return "channel"
        if cid in set(self.settings.cleanup_skip_channel_ids):
            return "channel"
        return None

    async def _send_test_drop(self, channel, drop: dict) -> bool:
        item = self.settings.item_by_key(str(drop.get("type_key")))
        if item is None:
            self.book.forget_test_drop(str(drop.get("id")))
            return False
        text = drop_text(item, parse_dt(drop["expires_at"]), test=True, drops_end=None)
        view = build_drop_view(str(drop["id"]), text)
        try:
            message = await channel.send(view=view, allowed_mentions=_NONE)
        except Exception:
            log.exception("turkey test drop send failed")
            self.book.forget_test_drop(str(drop["id"]))
            return False
        self.book.note_message(str(drop["id"]), int(message.id), test=True)
        return True

    async def _poll_tests(self, now: datetime) -> None:
        for ref in self.book.expire_tests(now):
            await self.safe_delete(ref)
        drop = self.book.poll_test_session(now)
        if drop is None:
            return
        channel = await self._channel(int(drop["channel_id"]))
        block = None if channel is None else self._test_channel_block(channel)
        if channel is None or block:
            self.book.forget_test_drop(str(drop["id"]))
            if block == "mts":
                self.book.stop_test_session()
            return
        await self._send_test_drop(channel, drop)

    def _test_channel_id_block(self, channel_id: int) -> str | None:
        """Refuse the live drop channel and cleanup-skip channels by id alone."""
        try:
            cid = int(channel_id)
        except (TypeError, ValueError):
            return "channel"
        if cid == int(self.settings.drop_channel_id):
            return "channel"
        if cid in {int(item) for item in self.settings.cleanup_skip_channel_ids}:
            return "channel"
        return None

    async def _lookup_test_channel(self, channel_id: int):
        """Resolve one channel by id. Never posts, and never substitutes another channel."""
        blocked = self._test_channel_id_block(channel_id)
        if blocked:
            return None, blocked
        channel = await self._channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return None, "unresolved"
        try:
            found = int(channel.id)
        except (TypeError, ValueError):
            return None, "unresolved"
        if found != int(channel_id):
            return None, "unresolved"
        block = self._test_channel_block(channel)
        if block:
            return None, block
        return channel, None

    def _ctl_refuse_text(self, action: str, reason: str) -> str:
        if reason == "unresolved":
            return TEST_CHANNEL_UNRESOLVED
        if action == "testdrop":
            return TEST_DROP_REFUSED
        if reason == "mts":
            return TEST_SESSION_MTS
        return TEST_SESSION_REFUSED

    def _ctl_finish(
        self,
        claimed: ClaimedFile,
        *,
        ok: bool,
        action: str,
        text: str,
        channel_id: int | None = None,
        error: str | None = None,
    ) -> None:
        if claimed.request_id:
            try:
                write_result(
                    self.ctl_root,
                    {
                        "ok": ok,
                        "action": action,
                        "id": claimed.request_id,
                        "text": text,
                        "channel_id": channel_id,
                        "error": error,
                    },
                )
            except Exception:
                log.exception("turkey ctl result write failed id=%s", claimed.request_id)
        discard_claimed(claimed.path)

    async def _poll_ctl(self, now: datetime) -> None:
        try:
            batch = claim_pending(self.ctl_root)
        except Exception:
            log.exception("turkey ctl inbox failed")
            return
        for claimed in batch:
            try:
                await self._dispatch_ctl(claimed, now)
            except Exception:
                log.exception("turkey ctl command failed id=%s", claimed.request_id)
                try:
                    self._ctl_finish(
                        claimed,
                        ok=False,
                        action=claimed.raw_action,
                        text="The control command failed.",
                        error="internal",
                    )
                except Exception:
                    log.exception("turkey ctl result write failed id=%s", claimed.request_id)
                    discard_claimed(claimed.path)

    async def _dispatch_ctl(self, claimed: ClaimedFile, now: datetime) -> None:
        if claimed.request_id and result_exists(self.ctl_root, claimed.request_id):
            discard_claimed(claimed.path)
            return
        if claimed.command is None:
            if claimed.request_id is None:
                log.info("turkey ctl discarded a file name=%s", claimed.path.name)
                discard_claimed(claimed.path)
                return
            reason = claimed.error or "rejected"
            self._ctl_finish(
                claimed,
                ok=False,
                action=claimed.raw_action,
                text=REJECT_TEXT.get(reason, REJECT_TEXT["rejected"]),
                error=reason,
            )
            return
        action = claimed.command.action
        if action == "testdrop":
            await self._ctl_testdrop(claimed, now)
        elif action == "test_session_start":
            await self._ctl_test_session_start(claimed, now)
        elif action == "test_session_stop":
            self._ctl_test_session_stop(claimed)
        elif action == "status":
            self._ctl_status(claimed)
        elif action == "schedule":
            self._ctl_schedule(claimed)
        else:
            self._ctl_finish(
                claimed,
                ok=False,
                action=action,
                text=REJECT_TEXT["unknown_action"],
                error="unknown_action",
            )

    def _ctl_channel_id(self, claimed: ClaimedFile) -> int:
        command = claimed.command
        if command is not None and command.channel_id is not None:
            return int(command.channel_id)
        return int(DEFAULT_TEST_CHANNEL_ID)

    async def _ctl_testdrop(self, claimed: ClaimedFile, now: datetime) -> None:
        channel_id = self._ctl_channel_id(claimed)
        channel, reason = await self._lookup_test_channel(channel_id)
        if channel is None:
            self._ctl_finish(
                claimed,
                ok=False,
                action="testdrop",
                text=self._ctl_refuse_text("testdrop", reason or "unresolved"),
                channel_id=channel_id,
                error="refused" if reason in {"mts", "channel"} else "unresolved",
            )
            return
        drop = self.book.open_test_drop(int(channel.id), now, self.rng)
        if drop is None:
            self._ctl_finish(
                claimed,
                ok=False,
                action="testdrop",
                text=TEST_DROP_REFUSED,
                channel_id=int(channel.id),
                error="refused",
            )
            return
        posted = await self._send_test_drop(channel, drop)
        if not posted:
            self._ctl_finish(
                claimed,
                ok=False,
                action="testdrop",
                text=TEST_DROP_SEND_FAILED,
                channel_id=int(channel.id),
                error="send_failed",
            )
            return
        self._ctl_finish(
            claimed,
            ok=True,
            action="testdrop",
            text=TEST_DROP_POSTED.format(channel_id=channel.id),
            channel_id=int(channel.id),
        )

    async def _ctl_test_session_start(self, claimed: ClaimedFile, now: datetime) -> None:
        command = claimed.command
        assert command is not None
        minutes = 15 if command.minutes is None else int(command.minutes)
        interval = 2 if command.interval is None else int(command.interval)
        channel_id = self._ctl_channel_id(claimed)
        channel, reason = await self._lookup_test_channel(channel_id)
        if channel is None:
            self._ctl_finish(
                claimed,
                ok=False,
                action="test_session_start",
                text=self._ctl_refuse_text("test_session_start", reason or "unresolved"),
                channel_id=channel_id,
                error="refused" if reason in {"mts", "channel"} else "unresolved",
            )
            return
        guild_id = getattr(getattr(channel, "guild", None), "id", None)
        if not isinstance(guild_id, int) or isinstance(guild_id, bool):
            self._ctl_finish(
                claimed,
                ok=False,
                action="test_session_start",
                text=TEST_SESSION_NEEDS_CHANNEL,
                channel_id=int(channel.id),
                error="unresolved",
            )
            return
        status = self.book.start_test_session(
            guild_id=int(guild_id),
            channel_id=int(channel.id),
            minutes=minutes,
            interval=interval,
            now=now,
        )
        if status == "busy":
            self._ctl_finish(
                claimed,
                ok=False,
                action="test_session_start",
                text=TEST_SESSION_BUSY,
                channel_id=int(channel.id),
                error="busy",
            )
            return
        if status != "started":
            self._ctl_finish(
                claimed,
                ok=False,
                action="test_session_start",
                text=TEST_SESSION_RANGE if status == "invalid" else TEST_SESSION_REFUSED,
                channel_id=int(channel.id),
                error="invalid" if status == "invalid" else "refused",
            )
            return
        try:
            await self._poll_tests(now)
        except Exception:
            log.exception("turkey test poll failed")
        self._ctl_finish(
            claimed,
            ok=True,
            action="test_session_start",
            text=TEST_SESSION_STARTED.format(channel_id=channel.id, minutes=minutes, interval=interval),
            channel_id=int(channel.id),
        )

    def _ctl_test_session_stop(self, claimed: ClaimedFile) -> None:
        if not self.book.stop_test_session():
            self._ctl_finish(claimed, ok=True, action="test_session_stop", text=TEST_SESSION_IDLE)
            return
        self._ctl_finish(claimed, ok=True, action="test_session_stop", text=TEST_SESSION_STOPPED)

    def _ctl_status(self, claimed: ClaimedFile) -> None:
        text = status_text(self.book.state, self.settings, self.now(), live_enabled=self.live_enabled())
        self._ctl_finish(claimed, ok=True, action="status", text=text)

    def _ctl_schedule(self, claimed: ClaimedFile) -> None:
        # Preview only. This path must not save or replace the live schedule.
        slots = self.book.preview_schedule(random.Random())
        minutes = int(self.settings.cluster_gap.total_seconds() // 60)
        text = "Unsaved preview.\n" + format_schedule_summary(slots, self.settings.zone, cluster_minutes=minutes)
        self._ctl_finish(claimed, ok=True, action="schedule", text=text)

    def simulate(self, now: datetime) -> str:
        """Preview a tick on a copy. Does not save and does not touch Discord."""
        clone = HuntBook(
            self.settings,
            self.book.path,
            rng=random.Random(0),
            archive_path=self.book.archive_path,
        )
        clone.state = deepcopy(self.book.state)
        clone.persist = False
        plan, _mutated = clone._prepare(now, avatar_exists=False)
        lines = [
            "Simulation only. Nothing was saved.",
            due_summary(self.book.state, self.settings, now),
            f"Kickoff in the preview: {'yes' if plan.kickoff_text else 'no'}.",
            f"Drops in the preview: {len(plan.posts)}.",
            f"Recaps in the preview: {len(plan.recaps)}.",
            f"Winner in the preview: {'yes' if plan.final_text else 'no'}.",
            f"Avatar in the preview: {plan.avatar}.",
        ]
        return "\n".join(lines)

    async def cleanup_report(self, *, dry_run: bool) -> str:
        refs = self.book.leftover_deletes()
        if dry_run:
            lines = [f"Dry run. {len(refs)} hunt messages would be deleted."]
            for ref in refs:
                lines.append(f"would delete {ref.kind} {ref.channel_id}/{ref.message_id}")
            lines.append("The winner post and the kickoff stay.")
            return "\n".join(lines)
        deleted = 0
        for ref in refs:
            if await self.safe_delete(ref):
                deleted += 1
        return (
            f"Cleanup finished. Deleted {deleted} of {len(refs)} hunt messages. "
            "The winner post and the kickoff stay."
        )

    async def _reply(self, ctx, content: str) -> None:
        await ctx.reply(content, mention_author=False, allowed_mentions=_NONE)

    async def _admin_channel(self, ctx, token):
        if isinstance(token, discord.TextChannel):
            return token, None
        if token is None:
            if getattr(ctx, "guild", None) is None:
                return None, "dm"
            current = getattr(ctx, "channel", None)
            if isinstance(current, discord.TextChannel):
                return current, None
            return None, "not_text"
        channel_id = _parse_channel_token(token)
        if channel_id is None:
            return None, "usage"
        found = await self._channel(channel_id)
        if not isinstance(found, discord.TextChannel):
            return None, "not_text"
        return found, None

    @app_commands.command(name="turkeys", description="Your turkey hunt score and the leaderboard.")
    @app_commands.guilds(MTS_GUILD_ID, SUPPORT_GUILD_ID)
    @app_commands.guild_only()
    async def turkeys(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        if interaction.guild_id == self.settings.guild_id:
            text = leaderboard_text(self.book.state["scores"], self.book.state["names"], user_id, test=False)
        elif interaction.guild_id == self.settings.support_guild_id:
            text = leaderboard_text(
                self.book.state["test_scores"],
                self.book.state["test_names"],
                user_id,
                test=True,
            )
        else:
            await interaction.response.send_message(
                "The turkey hunt is only running in Menaces to Sobriety.",
                ephemeral=True,
                allowed_mentions=_NONE,
            )
            return
        await interaction.response.send_message(text, allowed_mentions=_NONE)

    @commands.group(name="turkeyadmin", invoke_without_command=True, hidden=True, case_insensitive=True)
    @_turkeyadmin_check
    async def turkeyadmin(self, ctx: commands.Context) -> None:
        """Owner controls for the turkey hunt. Hidden from help."""
        if not await _turkeyadmin_allowed(ctx):
            return
        await self._reply(ctx, TURKEYADMIN_USAGE)

    @turkeyadmin.command(name="status", hidden=True)
    @_turkeyadmin_check
    async def admin_status(self, ctx: commands.Context) -> None:
        if not await _turkeyadmin_allowed(ctx):
            return
        await self._reply(
            ctx,
            status_text(self.book.state, self.settings, self.now(), live_enabled=self.live_enabled()),
        )

    @turkeyadmin.command(name="schedule", hidden=True)
    @_turkeyadmin_check
    async def admin_schedule(self, ctx: commands.Context, mode: str | None = None) -> None:
        if not await _turkeyadmin_allowed(ctx):
            return
        if mode is None:
            if self.book.state.get("schedule_built"):
                slots = self.book.state["schedule"]
                label = "Saved schedule."
            else:
                slots = self.book.preview_schedule(random.Random())
                label = "Unsaved preview."
            minutes = int(self.settings.cluster_gap.total_seconds() // 60)
            text = label + "\n" + format_schedule_summary(slots, self.settings.zone, cluster_minutes=minutes)
            await self._reply(ctx, text)
            return
        if mode.strip().casefold() != "save":
            await self._reply(ctx, TURKEYADMIN_USAGE)
            return
        result = self.book.save_generated_schedule(self.rng)
        if result == "posted":
            await self._reply(ctx, "A drop has already posted. The saved schedule was left as it is.")
            return
        await self._reply(ctx, "Schedule saved. The live hunt uses it when the event is on.")

    @turkeyadmin.command(name="cleanup", hidden=True)
    @_turkeyadmin_check
    async def admin_cleanup(self, ctx: commands.Context, dry_run: str | None = None) -> None:
        if not await _turkeyadmin_allowed(ctx):
            return
        preview = _parse_dry_run(dry_run)
        if preview is None:
            await self._reply(ctx, "Usage: !turkeyadmin cleanup [true|false]  (omitted means dry-run)")
            return
        guild_id = getattr(getattr(ctx, "guild", None), "id", None)
        if guild_id != self.settings.guild_id:
            await self._reply(ctx, "Cleanup only runs in Menaces to Sobriety.")
            return
        await self._reply(ctx, await self.cleanup_report(dry_run=preview))

    @turkeyadmin.command(name="testdrop", hidden=True)
    @_turkeyadmin_check
    async def admin_testdrop(self, ctx: commands.Context, channel: str | None = None) -> None:
        if not await _turkeyadmin_allowed(ctx):
            return
        if channel is None:
            await self._reply(ctx, "Usage: !turkeyadmin testdrop <channel>")
            return
        target, problem = await self._admin_channel(ctx, channel)
        if problem or target is None:
            await self._reply(ctx, "Usage: !turkeyadmin testdrop <channel id>")
            return
        if self._test_channel_block(target):
            await self._reply(ctx, TEST_DROP_REFUSED)
            return
        drop = self.book.open_test_drop(int(target.id), self.now(), self.rng)
        if drop is None:
            await self._reply(ctx, TEST_DROP_REFUSED)
            return
        posted = await self._send_test_drop(target, drop)
        if not posted:
            await self._reply(ctx, TEST_DROP_SEND_FAILED)
            return
        await self._reply(ctx, TEST_DROP_POSTED.format(channel_id=target.id))

    @turkeyadmin.command(name="test_session", hidden=True)
    @_turkeyadmin_check
    async def admin_test_session(
        self,
        ctx: commands.Context,
        minutes: str | None = None,
        interval: str | None = None,
        channel: str | None = None,
    ) -> None:
        if not await _turkeyadmin_allowed(ctx):
            return
        minutes_i = _parse_session_int(minutes, 15)
        interval_i = _parse_session_int(interval, 2)
        if minutes_i is None or interval_i is None:
            await self._reply(ctx, "Usage: !turkeyadmin test_session [minutes] [interval] [channel]")
            return
        guild_id = getattr(getattr(ctx, "guild", None), "id", None)
        if guild_id == self.settings.guild_id:
            await self._reply(ctx, TEST_SESSION_MTS)
            return
        target, problem = await self._admin_channel(ctx, channel)
        if problem == "dm" or target is None:
            await self._reply(ctx, TEST_SESSION_NEEDS_CHANNEL)
            return
        if self._test_channel_block(target):
            await self._reply(ctx, TEST_SESSION_REFUSED)
            return
        if guild_id is None:
            guild_id = getattr(getattr(target, "guild", None), "id", None)
        if guild_id is None:
            await self._reply(ctx, TEST_SESSION_NEEDS_CHANNEL)
            return
        status = self.book.start_test_session(
            guild_id=int(guild_id),
            channel_id=int(target.id),
            minutes=int(minutes_i),
            interval=int(interval_i),
            now=self.now(),
        )
        if status == "busy":
            await self._reply(ctx, TEST_SESSION_BUSY)
            return
        if status != "started":
            await self._reply(ctx, TEST_SESSION_REFUSED)
            return
        await self._reply(
            ctx,
            TEST_SESSION_STARTED.format(channel_id=target.id, minutes=minutes_i, interval=interval_i),
        )
        await self._poll_tests(self.now())

    @turkeyadmin.command(name="test_session_stop", hidden=True)
    @_turkeyadmin_check
    async def admin_test_session_stop(self, ctx: commands.Context) -> None:
        if not await _turkeyadmin_allowed(ctx):
            return
        if not self.book.stop_test_session():
            await self._reply(ctx, TEST_SESSION_IDLE)
            return
        await self._reply(ctx, TEST_SESSION_STOPPED)

    @turkeyadmin.command(name="simulate", hidden=True)
    @_turkeyadmin_check
    async def admin_simulate(self, ctx: commands.Context, when: str | None = None) -> None:
        if not await _turkeyadmin_allowed(ctx):
            return
        moment = self.now() if when is None else _parse_admin_moment(when, self.settings.zone)
        if moment is None:
            await self._reply(ctx, "Usage: !turkeyadmin simulate [ISO timestamp]")
            return
        before = self.book.path.read_bytes() if self.book.path.is_file() else None
        text = self.simulate(moment)
        after = self.book.path.read_bytes() if self.book.path.is_file() else None
        if before != after:
            log.error("turkey simulate wrote the live hunt file")
        await self._reply(ctx, text)

    @turkeyadmin.command(name="power", hidden=True)
    @_turkeyadmin_check
    async def admin_power(self, ctx: commands.Context, state: str | None = None) -> None:
        if not await _turkeyadmin_allowed(ctx):
            return
        mode = _parse_power(state)
        if mode is None:
            await self._reply(ctx, "Usage: !turkeyadmin power on|off")
            return
        if mode == "on":
            self.book.state["event_disabled"] = False
            self.book.save()
            if self.book.state.get("final_done"):
                await self._reply(ctx, "Local pause cleared. The hunt already finished, so scheduled posts stay off.")
                return
            if not self.live_enabled():
                await self._reply(
                    ctx,
                    "Local pause cleared. The live hunt stays off until thanksgiving_enabled is true.",
                )
                return
            await self._reply(ctx, "Turkey hunt local pause cleared.")
            return
        self.book.state["event_disabled"] = True
        self.book.save()
        await self._reply(
            ctx,
            "Turkey hunt paused. Scheduled drops, recaps, the winner post, and the avatar stay off.",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TurkeyHunt(bot, settings=load_turkey_settings()))
