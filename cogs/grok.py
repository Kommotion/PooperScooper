"""Grok commands that hand a prompt to a configured webhook.

/grok is a slash command. The owner can use it anywhere. In Menaces to
Sobriety, any Menace role gets five successful uses per rolling week.
Ask Grok is a message context menu and stays owner-only. The webhook is
expected to edit the deferred original response later. This cog only
acknowledges the interaction and posts the payload.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from cogs.utils.config import load_config
from cogs.utils.constants import DATA_DIR
from cogs.utils.grok_refs import GROK_REFS_PATH, GrokRefs
from cogs.utils.json_store import LockedJsonFile

log = logging.getLogger(__name__)

DEFAULT_KEY_HEADER = "Authorization"
WEBHOOK_TIMEOUT_SECONDS = 30
_TIMEOUT = aiohttp.ClientTimeout(total=WEBHOOK_TIMEOUT_SECONDS)
_PUBLIC_FAILURE = "Couldn't complete that Grok request."
_COMMAND_ERROR = "Something went wrong running that command."
_MISSING_CONFIG = (
    "Grok webhook is not configured. Set grok_webhook_url and grok_webhook_key in config.json."
)
# Menaces to Sobriety. Match any of these role ids; names are only labels.
MTS_GUILD_ID = 932057681307512922
MENACE_ROLE_ID = 1170245970634739762
MENACE_ROLE_IDS = frozenset({
    MENACE_ROLE_ID,  # Menace
    937926079291723816,  # Menace OG
    1142145259862757396,  # Menace Initiate
    1321648614472159302,  # Menace All
})
GROK_WEEKLY_LIMIT = 5
GROK_USAGE_WINDOW = timedelta(days=7)
GROK_USAGE_PATH = DATA_DIR / "grok_usage.json"
_OWNER_ONLY = "This command can only be used by the bot owner."
_MENACE_ROLE_REQUIRED = "You need a Menace role to use /grok here."
_ASK_GROK_NAME = "Ask Grok"
_ASK_PROMPT_LABEL = "What should I do with this?"
_DEFAULT_MESSAGE_PROMPT = "Help me with this message."
_RECENT_MESSAGE_LIMIT = 20
_RECENT_CONTENT_LIMIT = 1000


class GrokConfigError(Exception):
    """Raised when grok webhook settings are missing."""


class GrokCommandDenied(app_commands.CheckFailure):
    """A /grok check failure that already has the ephemeral text to send."""

    def __init__(self, message: str):
        super().__init__(message)
        self.public_message = message


def _limit_message(frees_at: int) -> str:
    return (
        "You've used all 5 /grok requests this week. "
        f"Your next one frees up <t:{int(frees_at)}:R>."
    )


def _usage_store(path: Path | None = None) -> LockedJsonFile:
    return LockedJsonFile(path or GROK_USAGE_PATH, default=dict, indent=2)


def _normalize_usage(data: object) -> dict[str, list[int]]:
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, list[int]] = {}
    for key, value in data.items():
        try:
            user_id = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, bool) or not isinstance(value, list):
            continue
        stamps: list[int] = []
        for item in value:
            if isinstance(item, bool):
                continue
            try:
                stamps.append(int(item))
            except (TypeError, ValueError):
                continue
        if stamps:
            cleaned[str(user_id)] = stamps
    return cleaned


def _prune_usage(users: dict[str, list[int]], now: datetime) -> dict[str, list[int]]:
    cutoff = _as_utc(now).timestamp() - GROK_USAGE_WINDOW.total_seconds()
    pruned: dict[str, list[int]] = {}
    for key, stamps in users.items():
        fresh = [stamp for stamp in stamps if stamp > cutoff]
        if fresh:
            pruned[key] = fresh
    return pruned


def _save_usage(path: Path, users: dict[str, list[int]]) -> None:
    """Atomic UTF-8 replace. No BOM. Used when the normal JSON reader cannot open the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(users, indent=2)
    if not payload.endswith("\n"):
        payload += "\n"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _mutate_usage(mutator, path: Path | None = None) -> dict[str, list[int]]:
    store = _usage_store(path)
    try:
        return store.update(lambda data: mutator(_normalize_usage(data)))
    except (UnicodeError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        log.warning("Grok usage file %s is unusable (%s); starting empty.", store.path, exc)
        users = mutator({})
        try:
            _save_usage(store.path, users)
        except OSError as write_exc:
            log.warning("Couldn't rewrite Grok usage file %s: %s", store.path, write_exc)
        return users


def grok_uses_and_block(
    user_id: int,
    now: datetime | None = None,
    *,
    path: Path | None = None,
) -> tuple[list[int], int | None]:
    """Prune stamps older than 7 days. Return (fresh stamps, unix the next slot frees).

    The second value is None when the user is under the weekly cap.
    """
    moment = _as_utc(now or datetime.now(timezone.utc))
    users = _mutate_usage(lambda data: _prune_usage(data, moment), path)
    stamps = list(users.get(str(int(user_id)), []))
    if len(stamps) < GROK_WEEKLY_LIMIT:
        return stamps, None
    frees_at = int(min(stamps) + GROK_USAGE_WINDOW.total_seconds())
    return stamps, frees_at


def record_grok_use(user_id: int, when: datetime, *, path: Path | None = None) -> None:
    """Append one successful /grok use and drop stamps older than 7 days."""
    stamp = int(_as_utc(when).timestamp())
    now = datetime.now(timezone.utc)

    def mutate(users: dict[str, list[int]]) -> dict[str, list[int]]:
        pruned = _prune_usage(users, now)
        pruned.setdefault(str(int(user_id)), []).append(stamp)
        return pruned

    _mutate_usage(mutate, path)


def _has_menace_role(user: object) -> bool:
    roles = getattr(user, "roles", None) or []
    for role in roles:
        role_id = getattr(role, "id", role)
        try:
            if int(role_id) in MENACE_ROLE_IDS:
                return True
        except (TypeError, ValueError):
            continue
    return False


async def app_command_is_owner(interaction: discord.Interaction) -> bool:
    """App-command owner gate. Uses the same client.is_owner check as the other cogs."""
    return await interaction.client.is_owner(interaction.user)


async def grok_slash_allowed(interaction: discord.Interaction) -> bool:
    """Owner anywhere. Any Menace role in Menaces to Sobriety, five successes per week. Else owner-only."""
    if await interaction.client.is_owner(interaction.user):
        return True
    if getattr(interaction, "guild_id", None) != MTS_GUILD_ID:
        raise GrokCommandDenied(_OWNER_ONLY)
    if not _has_menace_role(interaction.user):
        raise GrokCommandDenied(_MENACE_ROLE_REQUIRED)
    _stamps, frees_at = grok_uses_and_block(int(interaction.user.id))
    if frees_at is not None:
        raise GrokCommandDenied(_limit_message(frees_at))
    return True


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _to_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    return moment.isoformat()


def _created_at_iso(interaction: discord.Interaction) -> str:
    return _to_iso(interaction.created_at)


def _auth_headers(key: str, header_name: str) -> dict[str, str]:
    name = header_name.strip()
    if not name or name.lower() == DEFAULT_KEY_HEADER.lower():
        return {DEFAULT_KEY_HEADER: f"Bearer {key}"}
    return {name: key}


def _webhook_request() -> tuple[str, str, dict[str, str]]:
    config = load_config()
    url = config.grok_webhook_url.strip()
    key = config.grok_webhook_key.get_secret_value().strip()
    if not url or not key:
        raise GrokConfigError(_MISSING_CONFIG)
    return url, key, _auth_headers(key, config.grok_webhook_key_header)


def _guild_name(interaction: discord.Interaction) -> str | None:
    if getattr(interaction, "guild_id", None) is None:
        return None
    guild = getattr(interaction, "guild", None)
    if guild is None:
        return None
    name = getattr(guild, "name", None)
    if not isinstance(name, str):
        return None
    return name


def _channel_name(interaction: discord.Interaction) -> str | None:
    channel = getattr(interaction, "channel", None)
    if channel is None:
        return None
    # DM channels have no name attribute.
    name = getattr(channel, "name", None)
    if not isinstance(name, str):
        return None
    return name


def _attachment_payload(attachment: object, *, include_size: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "filename": attachment.filename,
        "url": attachment.url,
        "content_type": getattr(attachment, "content_type", None),
    }
    if include_size:
        payload["size"] = getattr(attachment, "size", None)
    return payload


def _command_attachments(attachment: discord.Attachment | None) -> list[dict[str, object]]:
    if attachment is None:
        return []
    return [_attachment_payload(attachment, include_size=True)]


def _truncate_content(content: object) -> str:
    text = content if isinstance(content, str) else ""
    if len(text) <= _RECENT_CONTENT_LIMIT:
        return text
    return text[:_RECENT_CONTENT_LIMIT]


def _reply_to_id(message: object) -> str | None:
    reference = getattr(message, "reference", None)
    if reference is None:
        return None
    message_id = getattr(reference, "message_id", None)
    if message_id is None:
        return None
    return str(message_id)


def _recent_message_payload(message: object) -> dict[str, object]:
    author = message.author
    attachments = getattr(message, "attachments", None) or []
    return {
        "id": str(message.id),
        "author_name": author.name,
        "author_id": str(author.id),
        "created_at": _to_iso(message.created_at),
        "content": _truncate_content(getattr(message, "content", "")),
        "attachments": [
            _attachment_payload(item, include_size=False) for item in attachments
        ],
        "reply_to_id": _reply_to_id(message),
    }


def _message_sort_key(message: object) -> tuple[datetime, int]:
    created = message.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    else:
        created = created.astimezone(timezone.utc)
    return (created, int(message.id))


async def _fetch_recent_messages(interaction: discord.Interaction) -> list[dict[str, object]]:
    """Last 20 messages before this interaction, oldest first.

    History is unavailable in some DMs and without Read Message History.
    Any failure becomes an empty list so the webhook post still runs.
    """
    channel = getattr(interaction, "channel", None)
    history = getattr(channel, "history", None)
    if not callable(history):
        return []
    try:
        # Omit oldest_first. True walks from the channel's oldest messages,
        # so the limit returns that oldest window instead of the recent one.
        messages = [
            message
            async for message in history(
                limit=_RECENT_MESSAGE_LIMIT,
                before=interaction,
            )
        ]
        messages.sort(key=_message_sort_key)
        return [_recent_message_payload(message) for message in messages]
    except Exception as exc:
        log.warning("Failed to fetch recent messages for Grok: %s", exc)
        return []


def _target_message_payload(message: discord.Message) -> dict[str, object]:
    author = message.author
    content = message.content if isinstance(message.content, str) else ""
    attachments = getattr(message, "attachments", None) or []
    embeds = getattr(message, "embeds", None) or []
    return {
        "id": str(message.id),
        "author_name": author.name,
        "author_id": str(author.id),
        "created_at": _to_iso(message.created_at),
        "content": content,
        "jump_url": message.jump_url,
        "attachments": [_attachment_payload(item, include_size=True) for item in attachments],
        "embeds": [
            {
                "title": getattr(embed, "title", None),
                "description": getattr(embed, "description", None),
            }
            for embed in embeds
        ],
    }


async def _build_payload(
    interaction: discord.Interaction,
    prompt: str,
    *,
    attachment: discord.Attachment | None = None,
    target_message: discord.Message | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "prompt": prompt,
        "user_id": str(interaction.user.id),
        "username": interaction.user.name,
        "channel_id": str(interaction.channel_id),
        "guild_id": None if interaction.guild_id is None else str(interaction.guild_id),
        "guild_name": _guild_name(interaction),
        "channel_name": _channel_name(interaction),
        "application_id": str(interaction.application_id),
        "interaction_id": str(interaction.id),
        "interaction_token": interaction.token,
        "created_at": _created_at_iso(interaction),
        "attachments": _command_attachments(attachment),
        "recent_messages": await _fetch_recent_messages(interaction),
    }
    if target_message is not None:
        payload["target_message"] = _target_message_payload(target_message)
    return payload


def _safe_error_text(exc: BaseException, secret: str) -> str:
    text = str(exc).strip() or "The Grok webhook request failed."
    if secret:
        text = text.replace(secret, "***")
    if len(text) > 1800:
        text = text[:1797] + "..."
    return text


async def _post_webhook(url: str, headers: dict[str, str], payload: dict[str, object]) -> None:
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if 200 <= response.status < 300:
                await response.read()
                return
            body = " ".join((await response.text()).split())
            if len(body) > 300:
                body = body[:300] + "..."
            detail = f"Grok webhook returned HTTP {response.status}"
            if body:
                detail = f"{detail}: {body}"
            raise RuntimeError(detail)


async def _dismiss_and_report(interaction: discord.Interaction, message: str) -> None:
    try:
        await interaction.delete_original_response()
    except Exception as exc:
        log.warning("Failed to delete the deferred /grok response: %s", exc)
        try:
            await interaction.edit_original_response(content=_PUBLIC_FAILURE)
        except Exception as edit_exc:
            log.warning("Failed to edit the deferred /grok response: %s", edit_exc)
    await interaction.followup.send(message, ephemeral=True)


async def _deliver(
    interaction: discord.Interaction,
    prompt: str,
    *,
    attachment: discord.Attachment | None = None,
    target_message: discord.Message | None = None,
) -> None:
    # thinking=True keeps a public message. A bare defer() on a modal submit
    # becomes deferred_message_update and leaves no @original to edit.
    await interaction.response.defer(thinking=True)
    secret = ""
    try:
        url, secret, headers = _webhook_request()
        payload = await _build_payload(
            interaction,
            prompt,
            attachment=attachment,
            target_message=target_message,
        )
        # Record before the POST. The routine can answer before that call returns,
        # and an unused ref expires in 24h if the post then fails.
        try:
            _record_grok_ref(interaction)
        except Exception as exc:
            log.warning("Failed to record Grok reminder ref: %s", exc)
        command = getattr(interaction, "command", None)
        command_name = getattr(command, "name", None)
        if not isinstance(command_name, str) or not command_name:
            command_name = _ASK_GROK_NAME if target_message is not None else "unknown"
        log.info(
            "Grok webhook post command=%s keys=%s target_message=%s recent_messages=%s",
            command_name,
            sorted(payload),
            target_message is not None,
            len(payload["recent_messages"]),
        )
        await _post_webhook(url, headers, payload)
        # Count only a successful /grok in Menaces to Sobriety. Ask Grok passes a
        # target message and stays owner-only, so it never spends the weekly cap.
        if target_message is None:
            try:
                await _maybe_record_grok_use(interaction)
            except Exception as exc:
                log.warning("Failed to record Grok usage: %s", exc)
    except Exception as exc:
        message = _safe_error_text(exc, secret)
        log.warning("Grok webhook request failed: %s", message)
        await _dismiss_and_report(interaction, message)


async def _maybe_record_grok_use(interaction: discord.Interaction) -> None:
    """Record a successful non-owner /grok in Menaces to Sobriety. Owners are unlimited."""
    if getattr(interaction, "guild_id", None) != MTS_GUILD_ID:
        return
    try:
        if await interaction.client.is_owner(interaction.user):
            return
    except Exception as exc:
        log.warning("Could not confirm Grok owner status: %s", exc)
        return
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    if user_id is None:
        return
    when = getattr(interaction, "created_at", None)
    if not isinstance(when, datetime):
        when = datetime.now(timezone.utc)
    record_grok_use(int(user_id), when)


def _record_grok_ref(interaction: discord.Interaction) -> None:
    """Remember this /grok so a later PS-REMIND block can be checked against it."""
    interaction_id = getattr(interaction, "id", None)
    user = getattr(interaction, "user", None)
    user_id = getattr(user, "id", None)
    channel_id = getattr(interaction, "channel_id", None)
    if interaction_id is None or user_id is None or channel_id is None:
        raise ValueError("interaction is missing id, user, or channel")
    created_at = getattr(interaction, "created_at", None)
    if not isinstance(created_at, datetime):
        created_at = datetime.now(timezone.utc)
    guild_id = getattr(interaction, "guild_id", None)
    refs = GrokRefs(GROK_REFS_PATH)
    refs.record(
        str(interaction_id),
        user_id=int(user_id),
        channel_id=int(channel_id),
        guild_id=None if guild_id is None else int(guild_id),
        created_at=created_at,
    )


class AskGrokModal(discord.ui.Modal, title=_ASK_GROK_NAME):
    """Optional note collected before the message is sent to Grok."""

    def __init__(self, message: discord.Message):
        super().__init__()
        self.message = message
        self.prompt_input = discord.ui.TextInput(
            label=_ASK_PROMPT_LABEL,
            style=discord.TextStyle.paragraph,
            required=False,
        )
        self.add_item(self.prompt_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.prompt_input.value.strip()
        prompt = text or _DEFAULT_MESSAGE_PROMPT
        await _deliver(interaction, prompt, target_message=self.message)


class Grok(commands.Cog):
    """Forward a prompt to the configured Grok webhook."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Context menus cannot be declared with a decorator on a Cog method.
        self.ask_grok_menu = app_commands.ContextMenu(
            name=_ASK_GROK_NAME,
            callback=self.ask_grok,
        )
        self.ask_grok_menu.add_check(app_command_is_owner)
        self.ask_grok_menu.error(self._on_ask_grok_error)
        tree = getattr(bot, "tree", None) if bot is not None else None
        if tree is not None:
            tree.add_command(self.ask_grok_menu)

    def cog_unload(self) -> None:
        tree = getattr(self.bot, "tree", None) if self.bot is not None else None
        if tree is None:
            return
        tree.remove_command(self.ask_grok_menu.name, type=self.ask_grok_menu.type)

    @app_commands.command(name="grok", description="Send a prompt to Grok.")
    @app_commands.describe(
        prompt="Prompt text to send.",
        attachment="Optional file to include with the prompt.",
    )
    @app_commands.check(grok_slash_allowed)
    async def grok(
        self,
        interaction: discord.Interaction,
        prompt: str,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        """Hand the prompt to the Grok webhook and leave the reply for it to edit."""
        await _deliver(interaction, prompt, attachment=attachment)

    async def ask_grok(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Open a modal, then send the selected message to the Grok webhook."""
        await interaction.response.send_modal(AskGrokModal(message))

    async def _on_ask_grok_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        await self.cog_app_command_error(interaction, error)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = getattr(error, "public_message", None) or _OWNER_ONLY
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return
        command = getattr(interaction, "command", None)
        command_name = getattr(command, "name", None) or "unknown"
        log.error("Unhandled %s error", command_name, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(_COMMAND_ERROR, ephemeral=True)
            else:
                await interaction.response.send_message(_COMMAND_ERROR, ephemeral=True)
        except Exception as exc:
            log.warning("Failed to send the %s error reply: %s", command_name, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Grok(bot))
