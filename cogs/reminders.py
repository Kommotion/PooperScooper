"""Personal reminders: /remind set, list, cancel, and timezone."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Cog

from cogs.utils.constants import DATA_DIR
from cogs.utils.grok_refs import (
    BLOCK_FIELD_NAME,
    GROK_REFS_PATH,
    BlockError,
    GrokRefs,
    RemindBlock,
    parse_remind_block,
)
from cogs.utils.remind_parse import (
    DEFAULT_TIMEZONE,
    EXAMPLES,
    ParsedWhen,
    ParseError,
    RepeatRule,
    as_utc,
    describe_repeat,
    next_occurrence,
    parse_when,
    resolve_timezone,
    zone_or_default,
)

log = logging.getLogger(__name__)

DEFAULT_PATH = DATA_DIR / "reminders.json"
MAX_ACTIVE = 25
MAX_TEXT = 500
LOOP_SECONDS = 45
LATE_GRACE = timedelta(seconds=60)
FIRED_KEEP = timedelta(days=30)
MISSED_KEEP = timedelta(hours=24)
PRIVATE_FALLBACK = (
    "I couldn't DM you your private reminder. Turn on DMs, or use `/remind list` to see it."
)
JOIN_TEMPLATE = r"remind:join:(?P<id>[0-9a-f]{8})"
SNOOZE_TEMPLATE = r"remind:snooze:(?P<id>[0-9a-f]{8}):(?P<delay>10m|1h|tomorrow)"
CANCEL_TEMPLATE = r"remind:cancel:(?P<id>[0-9a-f]{8})"
GROK_REMIND_CREATORS = {87324314084331520}
MAX_GROK_MENTIONS = 10
# A failed fetch or application-id mismatch must not stick until process restart.
WEBHOOK_UNTRUSTED_SECONDS = 10 * 60
REF_PROBLEM = "This reminder request expired or was already used."
PRIVATE_MENTION_REASON = "private reminders can't include other people"
SNOOZE_LABELS = {
    "10m": "Snooze 10 min",
    "1h": "Snooze 1 hour",
    "tomorrow": "Snooze tomorrow",
}
_ID_RE = re.compile(r"^[0-9a-f]{8}$")


class CapError(Exception):
    message = (
        "You already have 25 active reminders. "
        "Cancel one with `/remind cancel` before adding another."
    )


class TextTooLong(Exception):
    message = "Reminders can be at most 500 characters."


class EmptyReminder(Exception):
    message = "Tell me what to remind you about."


class ReminderNotFound(Exception):
    message = "No active reminder with that id. Use `/remind list` to see yours."


class NotYourReminder(Exception):
    message = "You can only cancel your own reminders."


class NotOnReminder(Exception):
    message = "Only people on this reminder can snooze it."


@dataclass
class Reminder:
    id: str
    creator_id: int
    participants: list[int]
    channel_id: int
    guild_id: int | None
    text: str
    next_fire_at: datetime
    timezone: str
    repeat: RepeatRule | None
    created_at: datetime
    status: str = "active"
    snooze_of: str | None = None
    private: bool = False
    missed_ids: list[int] = field(default_factory=list)
    missed_at: datetime | None = None

    def copy(self) -> Reminder:
        return Reminder(
            id=self.id,
            creator_id=self.creator_id,
            participants=list(self.participants),
            channel_id=self.channel_id,
            guild_id=self.guild_id,
            text=self.text,
            next_fire_at=self.next_fire_at,
            timezone=self.timezone,
            repeat=self.repeat,
            created_at=self.created_at,
            status=self.status,
            snooze_of=self.snooze_of,
            private=self.private,
            missed_ids=list(self.missed_ids),
            missed_at=self.missed_at,
        )

    def to_dict(self) -> dict:
        payload = {
            "id": self.id,
            "creator_id": self.creator_id,
            "participants": list(self.participants),
            "channel_id": self.channel_id,
            "guild_id": self.guild_id,
            "text": self.text,
            "next_fire_at": as_utc(self.next_fire_at).isoformat(),
            "timezone": self.timezone,
            "repeat": None if self.repeat is None else self.repeat.to_dict(),
            "created_at": as_utc(self.created_at).isoformat(),
            "status": self.status,
            "snooze_of": self.snooze_of,
            "private": self.private,
        }
        if self.missed_ids:
            payload["missed_ids"] = list(self.missed_ids)
        if self.missed_at is not None:
            payload["missed_at"] = as_utc(self.missed_at).isoformat()
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> Reminder:
        if not isinstance(data, dict):
            raise ValueError("reminder must be an object")
        reminder_id = str(data["id"])
        if not _ID_RE.fullmatch(reminder_id):
            raise ValueError("bad reminder id")
        status = data.get("status", "active")
        if status not in {"active", "fired"}:
            raise ValueError("bad reminder status")
        repeat_raw = data.get("repeat")
        repeat = None if repeat_raw is None else RepeatRule.from_dict(repeat_raw)
        participants = _clean_participants(int(data["creator_id"]), data.get("participants") or [])
        guild_id = data.get("guild_id")
        snooze_of = data.get("snooze_of")
        raw_private = data.get("private", False)
        missed_at = data.get("missed_at")
        return cls(
            id=reminder_id,
            creator_id=int(data["creator_id"]),
            participants=participants,
            channel_id=int(data["channel_id"]),
            guild_id=None if guild_id is None else int(guild_id),
            text=str(data["text"]),
            next_fire_at=_load_dt(data["next_fire_at"]),
            timezone=str(data.get("timezone") or DEFAULT_TIMEZONE),
            repeat=repeat,
            created_at=_load_dt(data.get("created_at") or data["next_fire_at"]),
            status=status,
            snooze_of=None if snooze_of is None else str(snooze_of),
            private=raw_private if isinstance(raw_private, bool) else False,
            missed_ids=_clean_id_list(data.get("missed_ids")),
            missed_at=None if missed_at is None else _load_dt(missed_at),
        )


@dataclass(frozen=True)
class FiredReminder:
    reminder: Reminder
    scheduled_for: datetime
    late: bool


class ReminderStore:
    """JSON reminder book. The path is injectable so tests don't touch data/."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.timezones: dict[str, str] = {}
        self.reminders: list[Reminder] = []
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            log.warning("Reminder file %s is missing; starting empty.", self.path)
            self.timezones = {}
            self.reminders = []
            self._log_loaded()
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Reminder file %s is corrupt (%s); starting empty.", self.path, exc)
            self.timezones = {}
            self.reminders = []
            self._log_loaded()
            return
        if not isinstance(raw, dict):
            log.warning("Reminder file %s is corrupt; starting empty.", self.path)
            self.timezones = {}
            self.reminders = []
            self._log_loaded()
            return
        self.timezones = _read_timezones(raw.get("timezones", {}), self.path)
        self.reminders = _read_reminders(raw.get("reminders", []), self.path)
        self._log_loaded()

    def _log_loaded(self) -> None:
        now = datetime.now(timezone.utc)
        overdue = sum(
            1
            for reminder in self.reminders
            if reminder.status == "active" and reminder.next_fire_at <= now
        )
        _log_info(f"Reminders loaded count={len(self.reminders)} overdue={overdue}")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timezones": self.timezones,
            "reminders": [reminder.to_dict() for reminder in self.reminders],
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def timezone_for(self, user_id: int) -> str:
        return self.timezones.get(str(user_id), DEFAULT_TIMEZONE)

    def set_timezone(self, user_id: int, name: str) -> str:
        try:
            canonical = resolve_timezone(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(name) from exc
        self.timezones[str(user_id)] = canonical
        self.save()
        return canonical

    def get(self, reminder_id: str) -> Reminder | None:
        for reminder in self.reminders:
            if reminder.id == reminder_id:
                return reminder
        return None

    def active_count(self, user_id: int) -> int:
        return sum(
            1 for reminder in self.reminders if reminder.status == "active" and reminder.creator_id == user_id
        )

    def active_for(self, user_id: int) -> list[Reminder]:
        rows = [
            reminder
            for reminder in self.reminders
            if reminder.status == "active" and reminder.creator_id == user_id
        ]
        rows.sort(key=lambda reminder: reminder.next_fire_at)
        return rows

    def create(
        self,
        *,
        creator_id: int,
        channel_id: int,
        guild_id: int | None,
        text: str,
        parsed: ParsedWhen,
        snooze_of: str | None = None,
        participants: list[int] | None = None,
        reminder_id: str | None = None,
        created_at: datetime | None = None,
        private: bool = False,
    ) -> Reminder:
        cleaned = _validate_text(text)
        if self.active_count(creator_id) >= MAX_ACTIVE:
            raise CapError()
        if reminder_id is None:
            reminder_id = self._new_id()
        elif not _ID_RE.fullmatch(reminder_id) or self.get(reminder_id) is not None:
            raise ValueError("reminder id must be a new 8-character hex id")
        reminder = Reminder(
            id=reminder_id,
            creator_id=creator_id,
            participants=_clean_participants(creator_id, participants or []),
            channel_id=channel_id,
            guild_id=guild_id,
            text=cleaned,
            next_fire_at=as_utc(parsed.next_fire_at),
            timezone=parsed.timezone,
            repeat=parsed.repeat,
            created_at=as_utc(created_at or datetime.now(timezone.utc)),
            status="active",
            snooze_of=snooze_of,
            private=private,
        )
        self.reminders.append(reminder)
        self.save()
        return reminder

    def cancel(self, reminder_id: str, user_id: int) -> Reminder:
        reminder = self.get(reminder_id)
        if reminder is None or reminder.status != "active":
            raise ReminderNotFound()
        if reminder.creator_id != user_id:
            raise NotYourReminder()
        self.reminders = [item for item in self.reminders if item.id != reminder_id]
        self.save()
        return reminder

    def drop(self, reminder_id: str) -> None:
        before = len(self.reminders)
        self.reminders = [item for item in self.reminders if item.id != reminder_id]
        if len(self.reminders) != before:
            self.save()

    def toggle_participant(self, reminder_id: str, user_id: int) -> str:
        reminder = self.get(reminder_id)
        if reminder is None:
            return "missing"
        if reminder.status != "active":
            return "inactive"
        if user_id == reminder.creator_id:
            return "creator"
        if user_id in reminder.participants:
            reminder.participants.remove(user_id)
            self.save()
            return "removed"
        reminder.participants.append(user_id)
        self.save()
        return "added"

    def snooze(self, source_id: str, user_id: int, delay: str, now: datetime) -> tuple[str, Reminder]:
        source = self.get(source_id)
        if source is None:
            raise ReminderNotFound()
        if user_id != source.creator_id and user_id not in source.participants:
            raise NotOnReminder()
        fire_at = _snooze_deadline(self.timezone_for(user_id), delay, now)
        if source.creator_id == user_id and source.repeat is None:
            self._reactivate(source, fire_at, user_id)
            return "rescheduled", source

        existing = next(
            (
                reminder
                for reminder in self.reminders
                if reminder.creator_id == user_id
                and reminder.snooze_of == source.id
                and reminder.repeat is None
            ),
            None,
        )
        if existing is not None:
            if existing.status != "active" and self.active_count(user_id) >= MAX_ACTIVE:
                raise CapError()
            existing.text = source.text
            existing.channel_id = source.channel_id
            existing.guild_id = source.guild_id
            existing.private = source.private
            self._reactivate(existing, fire_at, user_id)
            return "rescheduled", existing

        if self.active_count(user_id) >= MAX_ACTIVE:
            raise CapError()
        created = self.create(
            creator_id=user_id,
            channel_id=source.channel_id,
            guild_id=source.guild_id,
            text=source.text,
            parsed=ParsedWhen(next_fire_at=fire_at, repeat=None, timezone=self.timezone_for(user_id)),
            snooze_of=source.id,
            created_at=as_utc(now),
            private=source.private,
        )
        return "created", created

    def claim_due(self, now: datetime) -> list[FiredReminder]:
        now_utc = as_utc(now)
        fired: list[FiredReminder] = []
        for reminder in self.reminders:
            if reminder.status != "active" or reminder.next_fire_at > now_utc:
                continue
            snapshot = reminder.copy()
            late = reminder.next_fire_at <= now_utc - LATE_GRACE
            scheduled = reminder.next_fire_at
            reminder.missed_ids = []
            reminder.missed_at = None
            if reminder.repeat is None:
                reminder.status = "fired"
            else:
                reminder.next_fire_at = _future_repeat(reminder, now_utc, scheduled)
            fired.append(FiredReminder(reminder=snapshot, scheduled_for=scheduled, late=late))
        before = len(self.reminders)
        self._prune_fired(now_utc)
        if fired or len(self.reminders) != before:
            self.save()
        return fired

    def mark_missed(self, reminder_id: str, user_ids: list[int], when: datetime) -> None:
        """Keep a one-off whose private DM failed so /remind list can show the text."""
        reminder = self.get(reminder_id)
        if reminder is None or reminder.repeat is not None or not reminder.private:
            return
        missed = _clean_id_list(user_ids)
        if not missed:
            return
        reminder.missed_ids = missed
        reminder.missed_at = as_utc(when)
        self.save()

    def visible_for(self, user_id: int, now: datetime) -> list[Reminder]:
        now_utc = as_utc(now)
        before = len(self.reminders)
        self._prune_fired(now_utc)
        if len(self.reminders) != before:
            self.save()
        rows: list[Reminder] = []
        for reminder in self.reminders:
            if reminder.status == "active" and reminder.creator_id == user_id:
                rows.append(reminder)
            elif _is_open_miss(reminder, now_utc) and user_id in reminder.missed_ids:
                rows.append(reminder)
        rows.sort(key=lambda reminder: reminder.next_fire_at)
        return rows

    def _reactivate(self, reminder: Reminder, fire_at: datetime, user_id: int) -> None:
        if reminder.status != "active" and self.active_count(user_id) >= MAX_ACTIVE:
            raise CapError()
        reminder.next_fire_at = fire_at
        reminder.status = "active"
        reminder.repeat = None
        reminder.missed_ids = []
        reminder.missed_at = None
        self.save()

    def _prune_fired(self, now_utc: datetime) -> None:
        cutoff = now_utc - FIRED_KEEP
        kept: list[Reminder] = []
        for reminder in self.reminders:
            if reminder.status != "fired":
                kept.append(reminder)
                continue
            if reminder.private and reminder.missed_ids and reminder.repeat is None:
                if not _miss_expired(reminder, now_utc):
                    kept.append(reminder)
                else:
                    missed = ",".join(str(user_id) for user_id in reminder.missed_ids)
                    place = f"user={reminder.creator_id} channel={reminder.channel_id}"
                    if reminder.guild_id is not None:
                        place = f"user={reminder.creator_id} guild={reminder.guild_id} channel={reminder.channel_id}"
                    _log_info(f"Missed-private purged id={reminder.id} {place} missed={missed}")
                continue
            if reminder.next_fire_at >= cutoff:
                kept.append(reminder)
        self.reminders = kept

    def _new_id(self) -> str:
        existing = {reminder.id for reminder in self.reminders}
        for _ in range(8):
            candidate = secrets.token_hex(4)
            if candidate not in existing:
                return candidate
        raise RuntimeError("could not allocate a reminder id")


class RemindMeButton(discord.ui.DynamicItem[discord.ui.Button], template=JOIN_TEMPLATE):
    def __init__(self, reminder_id: str):
        super().__init__(
            discord.ui.Button(
                label="Remind me too",
                style=discord.ButtonStyle.primary,
                custom_id=f"remind:join:{reminder_id}",
            )
        )
        self.reminder_id = reminder_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
    ) -> RemindMeButton:
        return cls(match.group("id"))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _reminders_cog(interaction)
        if cog is None:
            _log_info(
                "remind join rejected "
                f"{_interaction_context(interaction, reminder_id=self.reminder_id)} reason=unavailable"
            )
            await interaction.response.send_message("Reminders aren't available right now.", ephemeral=True)
            return
        await cog.handle_join(interaction, self.reminder_id)


class SnoozeButton(discord.ui.DynamicItem[discord.ui.Button], template=SNOOZE_TEMPLATE):
    def __init__(self, reminder_id: str, delay: str):
        super().__init__(
            discord.ui.Button(
                label=SNOOZE_LABELS[delay],
                style=discord.ButtonStyle.secondary,
                custom_id=f"remind:snooze:{reminder_id}:{delay}",
            )
        )
        self.reminder_id = reminder_id
        self.delay = delay

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
    ) -> SnoozeButton:
        return cls(match.group("id"), match.group("delay"))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _reminders_cog(interaction)
        if cog is None:
            _log_info(
                "remind snooze rejected "
                f"{_interaction_context(interaction, reminder_id=self.reminder_id)} "
                f"delay={_one_line(self.delay, 40)} reason=unavailable"
            )
            await interaction.response.send_message("Reminders aren't available right now.", ephemeral=True)
            return
        await cog.handle_snooze(interaction, self.reminder_id, self.delay)


class CancelButton(discord.ui.DynamicItem[discord.ui.Button], template=CANCEL_TEMPLATE):
    def __init__(self, reminder_id: str):
        super().__init__(
            discord.ui.Button(
                label="Cancel",
                style=discord.ButtonStyle.danger,
                custom_id=f"remind:cancel:{reminder_id}",
            )
        )
        self.reminder_id = reminder_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
    ) -> CancelButton:
        return cls(match.group("id"))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _reminders_cog(interaction)
        if cog is None:
            _log_info(
                "remind cancel button "
                f"{_interaction_context(interaction, reminder_id=self.reminder_id)} result=unavailable"
            )
            await interaction.response.send_message("Reminders aren't available right now.", ephemeral=True)
            return
        await cog.handle_cancel_button(interaction, self.reminder_id)


class RemindMeView(discord.ui.View):
    def __init__(self, reminder_id: str):
        super().__init__(timeout=None)
        self.add_item(RemindMeButton(reminder_id))
        self.add_item(CancelButton(reminder_id))


class CancelView(discord.ui.View):
    """Confirmation for a private Grok reminder. Snooze stays on the delivered ping."""

    def __init__(self, reminder_id: str):
        super().__init__(timeout=None)
        self.add_item(CancelButton(reminder_id))


class SnoozeView(discord.ui.View):
    def __init__(self, reminder_id: str):
        super().__init__(timeout=None)
        for delay in ("10m", "1h", "tomorrow"):
            self.add_item(SnoozeButton(reminder_id, delay))


def _reminders_cog(interaction: discord.Interaction) -> Reminders | None:
    cog = interaction.client.get_cog("Reminders")
    if isinstance(cog, Reminders):
        return cog
    return None


def _one_line(value: object, limit: int | None = None) -> str:
    text = " ".join(str(value).split())
    if limit is not None and len(text) > limit:
        return text[:limit]
    return text


def _log_info(message: str) -> None:
    """INFO on the module logger. The message is forced onto one line."""
    log.info("%s", message.replace("\r", " ").replace("\n", " "))


def _preview_text(text: str, *, private: bool) -> str:
    """Length always. Public text is capped at 80 characters. Private text is omitted."""
    cleaned = text.strip()
    if private:
        return f"text_len={len(cleaned)}"
    snippet = cleaned.replace("\r", " ").replace("\n", " ")[:80]
    return f"text_len={len(cleaned)} text={snippet!r}"


def _cached_username(bot, user_id: int) -> str | None:
    getter = getattr(bot, "get_user", None)
    if not callable(getter):
        return None
    user = getter(user_id)
    name = getattr(user, "name", None) if user is not None else None
    if not name:
        return None
    return _one_line(name, 80)


def _cached_guild_name(bot, guild_id: int | None) -> str | None:
    if guild_id is None:
        return None
    getter = getattr(bot, "get_guild", None)
    if not callable(getter):
        return None
    guild = getter(guild_id)
    name = getattr(guild, "name", None) if guild is not None else None
    if not name:
        return None
    return _one_line(name, 80)


def _interaction_context(interaction, *, reminder_id: str | None = None) -> str:
    parts: list[str] = []
    if reminder_id:
        parts.append(f"id={reminder_id}")
    user = getattr(interaction, "user", None)
    parts.append(f"user={getattr(user, 'id', None)}")
    username = getattr(user, "name", None) if user is not None else None
    if username:
        parts.append(f"username={_one_line(username, 80)}")
    guild_id = getattr(interaction, "guild_id", None)
    if guild_id is not None:
        parts.append(f"guild={guild_id}")
        guild = getattr(interaction, "guild", None)
        guild_name = getattr(guild, "name", None) if guild is not None else None
        if guild_name:
            parts.append(f"guild_name={_one_line(guild_name, 80)}")
    channel_id = getattr(interaction, "channel_id", None)
    if channel_id is not None:
        parts.append(f"channel={channel_id}")
        channel = getattr(interaction, "channel", None)
        channel_name = getattr(channel, "name", None) if channel is not None else None
        if channel_name:
            parts.append(f"channel_name={_one_line(channel_name, 80)}")
    return " ".join(parts)


def _delivery_context(bot, reminder: Reminder, *, channel_name: str | None = None) -> str:
    parts = [f"id={reminder.id}", f"user={reminder.creator_id}"]
    username = _cached_username(bot, reminder.creator_id)
    if username:
        parts.append(f"username={username}")
    if reminder.guild_id is not None:
        parts.append(f"guild={reminder.guild_id}")
        guild_name = _cached_guild_name(bot, reminder.guild_id)
        if guild_name:
            parts.append(f"guild_name={guild_name}")
    parts.append(f"channel={reminder.channel_id}")
    if channel_name:
        parts.append(f"channel_name={_one_line(channel_name, 80)}")
    return " ".join(parts)


def _format_schedule(parsed: ParsedWhen) -> str:
    utc = as_utc(parsed.next_fire_at)
    zone, name = zone_or_default(parsed.timezone)
    local = utc.astimezone(zone)
    repeat = "once" if parsed.repeat is None else describe_repeat(parsed.repeat)
    return f"utc={utc.isoformat()} local={local.isoformat()} tz={name} repeat={repeat}"


def _timing(fired: FiredReminder, now: datetime) -> str:
    seconds_late = int((as_utc(now) - as_utc(fired.scheduled_for)).total_seconds())
    if seconds_late < 0:
        seconds_late = 0
    state = "late" if fired.late else "on time"
    return f"timing={state} seconds_late={seconds_late}"


def _exc_name(exc: BaseException | None, default: str) -> str:
    if exc is None:
        return default
    return type(exc).__name__


def _message_id(message_id: int | None) -> str:
    return "none" if message_id is None else str(message_id)


def _dm_who(user, user_id: int) -> str:
    username = getattr(user, "name", None) if user is not None else None
    if username:
        return f"dm_user={user_id} dm_username={_one_line(username, 80)}"
    return f"dm_user={user_id}"


def audience_ids(reminder: Reminder) -> list[int]:
    return _clean_id_list([reminder.creator_id, *reminder.participants])


def allowed_mentions_for_ids(user_ids: list[int]) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[discord.Object(id=user_id) for user_id in _clean_id_list(user_ids)],
    )


def allowed_mentions_for(reminder: Reminder) -> discord.AllowedMentions:
    return allowed_mentions_for_ids(audience_ids(reminder))


def render_confirmation(reminder: Reminder) -> str:
    stamp = int(reminder.next_fire_at.timestamp())
    repeat = "once" if reminder.repeat is None else describe_repeat(reminder.repeat)
    return "\n".join(
        [
            f"Reminder set for <@{reminder.creator_id}>.",
            f"**{reminder.text}**",
            f"Next: <t:{stamp}:F> (<t:{stamp}:R>)",
            f"Repeats: {repeat}",
            f"ID: `{reminder.id}`",
        ]
    )


def render_grok_private_confirmation(reminder: Reminder) -> str:
    """Channel confirmation for a private Grok reminder. The text stays out of it."""
    stamp = int(reminder.next_fire_at.timestamp())
    repeat = "once" if reminder.repeat is None else describe_repeat(reminder.repeat)
    return (
        f"Private reminder set for <@{reminder.creator_id}>. "
        f"I'll DM you <t:{stamp}:F> (<t:{stamp}:R>). "
        f"Repeats: {repeat} "
        f"ID: `{reminder.id}`"
    )


def render_private_fallback(user_ids: list[int]) -> str:
    mentions = " ".join(f"<@{user_id}>" for user_id in _clean_id_list(user_ids))
    return f"{mentions}, {PRIVATE_FALLBACK}"


def render_ping(fired: FiredReminder) -> str:
    reminder = fired.reminder
    mentions = " ".join(f"<@{user_id}>" for user_id in audience_ids(reminder))
    stamp = int(fired.scheduled_for.timestamp())
    lines = [
        mentions,
        f"**Reminder:** {reminder.text}",
        f"Due <t:{stamp}:F> (<t:{stamp}:R>)",
    ]
    if reminder.repeat is not None:
        lines.append(f"Repeats {describe_repeat(reminder.repeat)}.")
    if fired.late:
        lines.append("This reminder is late.")
    return "\n".join(lines)


def render_list(reminders: list[Reminder]) -> list[str]:
    if not reminders:
        return ["You don't have any reminders. Set one with `/remind set`."]
    lines = ["Your reminders:"]
    for reminder in reminders:
        stamp = int(reminder.next_fire_at.timestamp())
        text = reminder.text.replace("\n", " ")
        missed = reminder.status == "fired" and reminder.private and bool(reminder.missed_ids)
        if missed:
            kind = "missed (private)"
        else:
            if len(text) > 80:
                text = text[:79] + "…"
            kind = "once" if reminder.repeat is None else describe_repeat(reminder.repeat)
            if reminder.private:
                kind = f"{kind} (private)"
        lines.append(f"`{reminder.id}` — <t:{stamp}:F> (<t:{stamp}:R>) — {text} — {kind}")
    return _chunk_lines(lines)


class Reminders(Cog):
    """Set personal reminders and ping whoever asked to be included."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        path: str | Path | None = None,
        refs: GrokRefs | None = None,
        refs_path: str | Path | None = None,
    ):
        self.bot = bot
        self.store = ReminderStore(path if path is not None else DEFAULT_PATH)
        self._lock = asyncio.Lock()
        self._grok_seen: set[int] = set()
        # webhook id -> (trusted, webhook or None, monotonic timestamp)
        self._webhook_trust: dict[int, tuple[bool, object | None, float]] = {}
        self._refs = refs
        self._refs_path = None if refs_path is None else Path(refs_path)

    remind_group = app_commands.Group(name="remind", description="Personal reminders.")

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RemindMeButton, SnoozeButton, CancelButton)
        if not self.reminder_loop.is_running():
            self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()
        remove = getattr(self.bot, "remove_dynamic_items", None)
        if remove is not None:
            remove(RemindMeButton, SnoozeButton, CancelButton)

    def _grok_refs(self) -> GrokRefs:
        if self._refs is None:
            path = self._refs_path if self._refs_path is not None else GROK_REFS_PATH
            self._refs = GrokRefs(path)
        return self._refs

    @tasks.loop(seconds=LOOP_SECONDS)
    async def reminder_loop(self) -> None:
        try:
            await self.dispatch_due()
        except Exception:
            log.exception("Reminder loop failed")

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self.dispatch_due()
        except Exception:
            log.exception("Failed to fire overdue reminders on startup")

    async def dispatch_due(self, *, now: datetime | None = None) -> list[FiredReminder]:
        moment = now if now is not None else discord.utils.utcnow()
        async with self._lock:
            due = self.store.claim_due(moment)
        for item in due:
            if item.reminder.repeat is not None:
                current = self.store.get(item.reminder.id)
                next_at = None if current is None else current.next_fire_at
                next_text = as_utc(next_at).isoformat() if next_at is not None else "unknown"
                _log_info(
                    "Repeat rescheduled "
                    f"{_delivery_context(self.bot, item.reminder)} next={next_text} "
                    f"{_preview_text(item.reminder.text, private=item.reminder.private)}"
                )
            try:
                delivered = await self._deliver(item, now=moment)
            except Exception:
                log.exception("Failed to deliver reminder %s", item.reminder.id)
                delivered = False
            if not delivered:
                async with self._lock:
                    self.store.drop(item.reminder.id)
                _log_info(
                    "Dropped "
                    f"{_delivery_context(self.bot, item.reminder)} {_timing(item, moment)} "
                    f"{_preview_text(item.reminder.text, private=item.reminder.private)}"
                )
        return due

    async def handle_join(self, interaction: discord.Interaction, reminder_id: str) -> None:
        async with self._lock:
            result = self.store.toggle_participant(reminder_id, interaction.user.id)
        _log_info(
            f"remind join {_interaction_context(interaction, reminder_id=reminder_id)} result={result}"
        )
        messages = {
            "added": "You're on this reminder. I'll ping you too.",
            "removed": "You're off this reminder.",
            "creator": "You set this reminder, so you stay on it.",
            "missing": "That reminder doesn't exist anymore.",
            "inactive": "That reminder already went out.",
        }
        await interaction.response.send_message(messages[result], ephemeral=True)

    async def handle_snooze(self, interaction: discord.Interaction, reminder_id: str, delay: str) -> None:
        now = discord.utils.utcnow()
        delay_text = _one_line(delay, 40)
        context = _interaction_context(interaction, reminder_id=reminder_id)
        try:
            async with self._lock:
                action, reminder = self.store.snooze(reminder_id, interaction.user.id, delay, now)
        except ReminderNotFound:
            _log_info(f"remind snooze rejected {context} delay={delay_text} reason=not found")
            await interaction.response.send_message("That reminder doesn't exist anymore.", ephemeral=True)
            return
        except NotOnReminder as exc:
            _log_info(f"remind snooze rejected {context} delay={delay_text} reason={_one_line(exc.message)}")
            await interaction.response.send_message(exc.message, ephemeral=True)
            return
        except CapError as exc:
            _log_info(f"remind snooze rejected {context} delay={delay_text} reason=cap reached")
            await interaction.response.send_message(exc.message, ephemeral=True)
            return
        except ValueError:
            _log_info(f"remind snooze rejected {context} delay={delay_text} reason=bad delay")
            await interaction.response.send_message("That isn't a snooze option I know.", ephemeral=True)
            return
        stamp = int(reminder.next_fire_at.timestamp())
        verb = "Snooze updated" if action == "rescheduled" else "Snoozed"
        _log_info(
            "remind snooze "
            f"{_interaction_context(interaction, reminder_id=reminder.id)} "
            f"source={reminder_id} delay={delay_text} result={action} "
            f"next={as_utc(reminder.next_fire_at).isoformat()}"
        )
        await interaction.response.send_message(
            f"{verb}. I'll remind you <t:{stamp}:R> (<t:{stamp}:F>).",
            ephemeral=True,
        )

    async def handle_cancel_button(self, interaction: discord.Interaction, reminder_id: str) -> None:
        context = _interaction_context(interaction, reminder_id=reminder_id)
        try:
            async with self._lock:
                reminder = self.store.cancel(reminder_id, interaction.user.id)
        except ReminderNotFound:
            _log_info(f"remind cancel button {context} result=not found")
            await interaction.response.send_message(ReminderNotFound.message, ephemeral=True)
            return
        except NotYourReminder:
            _log_info(f"remind cancel button {context} result=not yours")
            await interaction.response.send_message(NotYourReminder.message, ephemeral=True)
            return
        _log_info(
            "remind cancel button "
            f"{_interaction_context(interaction, reminder_id=reminder.id)} result=cancelled "
            f"{_preview_text(reminder.text, private=reminder.private)}"
        )
        if reminder.private:
            content = f"Cancelled reminder `{reminder.id}`."
        else:
            content = f"Cancelled reminder `{reminder.id}`: {reminder.text}"
        await interaction.response.send_message(content, ephemeral=True)

    @remind_group.command(name="set", description="Set a reminder.")
    @app_commands.describe(
        when="When to remind you, in plain words.",
        what="What to remind you about.",
        private="DM this reminder instead of posting it in the channel.",
    )
    async def remind_set(
        self,
        interaction: discord.Interaction,
        when: str,
        what: str,
        private: bool = False,
    ) -> None:
        context = _interaction_context(interaction)
        when_text = repr(_one_line(when, 200))
        preview = _preview_text(what, private=private)
        if interaction.channel_id is None:
            _log_info(
                f"remind set rejected {context} when={when_text} private={private} {preview} reason=no channel"
            )
            await interaction.response.send_message("I need a channel to send this reminder in.", ephemeral=True)
            return
        try:
            _validate_text(what)
        except (EmptyReminder, TextTooLong) as exc:
            reason = "too long" if isinstance(exc, TextTooLong) else "empty"
            _log_info(
                f"remind set rejected {context} when={when_text} private={private} {preview} reason={reason}"
            )
            await interaction.response.send_message(exc.message, ephemeral=True)
            return
        try:
            parsed = parse_when(when, now=discord.utils.utcnow(), timezone_name=self.store.timezone_for(interaction.user.id))
        except ParseError as exc:
            reason = "past time" if "past" in exc.reason.lower() else "parse failure"
            _log_info(
                f"remind set rejected {context} when={when_text} private={private} {preview} "
                f"reason={reason} detail={_one_line(exc.reason, 200)!r}"
            )
            await interaction.response.send_message(exc.message, ephemeral=True)
            return
        try:
            async with self._lock:
                reminder = self.store.create(
                    creator_id=interaction.user.id,
                    channel_id=interaction.channel_id,
                    guild_id=interaction.guild_id,
                    text=what,
                    parsed=parsed,
                    private=private,
                )
        except CapError:
            _log_info(
                f"remind set rejected {context} when={when_text} private={private} {preview} reason=cap reached"
            )
            await interaction.response.send_message(CapError.message, ephemeral=True)
            return
        _log_info(
            "remind set "
            f"{_interaction_context(interaction, reminder_id=reminder.id)} "
            f"when={when_text} {_format_schedule(parsed)} private={private} "
            f"{_preview_text(reminder.text, private=reminder.private)}"
        )
        payload: dict = {
            "allowed_mentions": allowed_mentions_for(reminder),
            "ephemeral": reminder.private,
        }
        if not reminder.private:
            payload["view"] = RemindMeView(reminder.id)
        await interaction.response.send_message(render_confirmation(reminder), **payload)

    @remind_group.command(name="list", description="Show your reminders.")
    async def remind_list(self, interaction: discord.Interaction) -> None:
        now = discord.utils.utcnow()
        async with self._lock:
            reminders = self.store.visible_for(interaction.user.id, now)
        _log_info(f"remind list {_interaction_context(interaction)} count={len(reminders)}")
        chunks = render_list(reminders)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @remind_group.command(name="cancel", description="Cancel one of your reminders.")
    @app_commands.describe(id="Reminder id from /remind list.")
    async def remind_cancel(self, interaction: discord.Interaction, id: str) -> None:
        requested = id.strip()
        try:
            async with self._lock:
                reminder = self.store.cancel(requested, interaction.user.id)
        except (ReminderNotFound, NotYourReminder) as exc:
            reason = "not found" if isinstance(exc, ReminderNotFound) else "not yours"
            _log_info(
                "remind cancel "
                f"{_interaction_context(interaction, reminder_id=requested or id)} result={reason}"
            )
            await interaction.response.send_message(exc.message, ephemeral=True)
            return
        _log_info(
            "remind cancel "
            f"{_interaction_context(interaction, reminder_id=reminder.id)} result=cancelled "
            f"{_preview_text(reminder.text, private=reminder.private)}"
        )
        await interaction.response.send_message(
            f"Cancelled reminder `{reminder.id}`: {reminder.text}",
            ephemeral=True,
        )

    @remind_cancel.autocomplete("id")
    async def remind_cancel_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        needle = current.strip().lower()
        async with self._lock:
            reminders = self.store.active_for(interaction.user.id)
        choices: list[app_commands.Choice[str]] = []
        for reminder in reminders:
            preview = reminder.text.replace("\n", " ")
            name = f"{reminder.id} — {preview}"
            if len(name) > 100:
                name = name[:99] + "…"
            if needle and needle not in name.lower():
                continue
            choices.append(app_commands.Choice(name=name, value=reminder.id))
            if len(choices) == 25:
                break
        return choices

    @remind_group.command(name="timezone", description="Set the timezone I use for your reminders.")
    @app_commands.describe(tz="IANA timezone name, like America/Los_Angeles.")
    async def remind_timezone(self, interaction: discord.Interaction, tz: str) -> None:
        requested = _one_line(tz, 200)
        try:
            async with self._lock:
                old = self.store.timezone_for(interaction.user.id)
                canonical = self.store.set_timezone(interaction.user.id, tz)
        except ValueError:
            _log_info(
                "remind timezone rejected "
                f"{_interaction_context(interaction)} tz={requested!r} reason=bad timezone"
            )
            await interaction.response.send_message(
                f"`{tz.strip()}` is not a valid IANA timezone. Example: `America/Los_Angeles`.",
                ephemeral=True,
            )
            return
        _log_info(f"remind timezone {_interaction_context(interaction)} old={old} -> new={canonical}")
        await interaction.response.send_message(
            f"Your reminder timezone is now `{canonical}`.",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._ingest_grok_remind(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        message = getattr(payload, "message", None)
        if message is None:
            return
        await self._ingest_grok_remind(message)

    async def _ingest_grok_remind(self, message) -> None:
        """Turn a trusted PS-REMIND v1 embed into a normal reminder."""
        if not _message_has_block(message):
            return
        block: RemindBlock | None = None
        parse_error: str | None = None
        try:
            block = parse_remind_block(getattr(message, "embeds", None))
        except BlockError as exc:
            parse_error = exc.reason
        if block is None and parse_error is None:
            return
        if not await self._grok_message_trusted(message):
            self._log_grok_rejected(message, block, "untrusted source")
            return
        message_id = _positive_int(getattr(message, "id", None))
        if message_id is None:
            self._log_grok_rejected(message, block, "bad message")
            return
        async with self._lock:
            if message_id in self._grok_seen:
                self._log_grok_rejected(message, block, "duplicate")
                return
            self._grok_seen.add(message_id)
        if parse_error is not None:
            await self._reject_grok(message, block, parse_error)
            return
        assert block is not None
        if block.creator_id not in GROK_REMIND_CREATORS:
            await self._reject_grok(message, block, "creator not allowed")
            return
        metadata = getattr(message, "interaction_metadata", None)
        if metadata is not None:
            meta_user = getattr(metadata, "user", None)
            if _positive_int(getattr(meta_user, "id", None)) != block.creator_id:
                await self._reject_grok(message, block, "interaction user mismatch")
                return
        channel_id = _message_channel_id(message)
        guild_id = _message_guild_id(message)
        if channel_id != block.channel_id:
            await self._reject_grok(message, block, "channel mismatch")
            return
        if guild_id != block.guild_id:
            await self._reject_grok(message, block, "guild mismatch")
            return
        try:
            text = _validate_text(block.text)
        except EmptyReminder:
            await self._reject_grok(message, block, "empty", EmptyReminder.message)
            return
        except TextTooLong:
            await self._reject_grok(message, block, "too long", TextTooLong.message)
            return
        now = discord.utils.utcnow()
        try:
            parsed = _parsed_from_block(block, timezone_name=self.store.timezone_for(block.creator_id), now=now)
        except ParseError as exc:
            if "past" in exc.reason.lower():
                await self._reject_grok(message, block, "past time", "That time is already in the past.")
            else:
                await self._reject_grok(message, block, "bad time", exc.reason)
            return
        if as_utc(parsed.next_fire_at) <= as_utc(now):
            await self._reject_grok(message, block, "past time", "That time is already in the past.")
            return
        others = _dedupe_mentions(block.creator_id, block.mention_ids)
        if block.private and others:
            await self._reject_grok(message, block, PRIVATE_MENTION_REASON)
            return
        if len(others) > MAX_GROK_MENTIONS:
            await self._reject_grok(message, block, "mention_ids can include at most 10 people")
            return
        if others and guild_id is None:
            await self._reject_grok(message, block, "mention_ids require a server")
            return
        member_problem = await self._missing_member(message, others)
        if member_problem is not None:
            await self._reject_grok(message, block, member_problem)
            return
        reminder, status = await self._commit_grok_reminder(
            block,
            text=text,
            parsed=parsed,
            channel_id=channel_id,
            guild_id=guild_id,
            participants=others,
            now=now,
        )
        if status == "created" and reminder is not None:
            if block.exposed_private_text:
                _log_info(
                    "grok remind exposed=yes "
                    f"user={block.creator_id} message={message_id} ref={block.ref}"
                )
            source = _grok_source(self.bot, message)
            _log_info(
                "grok remind set "
                f"id={reminder.id} user={reminder.creator_id} guild={_id_or_none(reminder.guild_id)} "
                f"channel={reminder.channel_id} message={message_id} ref={block.ref} "
                f"source={source} {_format_schedule(parsed)} private={reminder.private} "
                f"participants={len(reminder.participants)} "
                f"{_preview_text(reminder.text, private=reminder.private)}"
            )
            await self._strip_grok_block(message)
            if reminder.private:
                content = render_grok_private_confirmation(reminder)
                view: discord.ui.View | None = CancelView(reminder.id)
            else:
                content = render_confirmation(reminder)
                view = RemindMeView(reminder.id)
            await self._grok_reply(
                message,
                content,
                allowed=allowed_mentions_for(reminder),
                view=view,
            )
            return
        if status == "consumed":
            await self._reject_grok(message, block, "duplicate", REF_PROBLEM)
            return
        if status in {"missing", "expired"}:
            reason = "unknown ref" if status == "missing" else "expired ref"
            await self._reject_grok(message, block, reason, REF_PROBLEM)
            return
        if status == "user mismatch":
            await self._reject_grok(message, block, "ref user mismatch")
            return
        if status == "channel mismatch":
            await self._reject_grok(message, block, "ref channel mismatch")
            return
        if status == "cap":
            await self._reject_grok(message, block, "cap reached", CapError.message)
            return
        await self._reject_grok(message, block, "couldn't save the reminder")

    async def _commit_grok_reminder(
        self,
        block: RemindBlock,
        *,
        text: str,
        parsed: ParsedWhen,
        channel_id: int,
        guild_id: int | None,
        participants: list[int],
        now: datetime,
    ) -> tuple[Reminder | None, str]:
        refs = self._grok_refs()
        async with self._lock:
            status = refs.prepare(
                block.ref,
                user_id=block.creator_id,
                channel_id=block.channel_id,
                now=now,
            )
            if status != "ready":
                return None, status
            if not refs.mark_consumed(block.ref, now=now):
                return None, "consumed"
            try:
                reminder = self.store.create(
                    creator_id=block.creator_id,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    text=text,
                    parsed=parsed,
                    participants=participants,
                    private=block.private,
                )
            except CapError:
                refs.unconsume(block.ref)
                return None, "cap"
            except Exception:
                refs.unconsume(block.ref)
                log.exception("Failed to store grok reminder ref=%s", block.ref)
                return None, "store"
            return reminder, "created"

    async def _grok_message_trusted(self, message) -> bool:
        bot_user = getattr(self.bot, "user", None)
        author = getattr(message, "author", None)
        bot_id = _positive_int(getattr(bot_user, "id", None))
        author_id = _positive_int(getattr(author, "id", None))
        if bot_id is not None and author_id == bot_id:
            return True
        webhook_id = _positive_int(getattr(message, "webhook_id", None))
        if webhook_id is None:
            return False
        app_id = _positive_int(getattr(self.bot, "application_id", None))
        # Interaction follow-ups use the application id as the webhook id.
        if app_id is not None and webhook_id == app_id:
            return True
        trusted, _webhook = await self._trusted_webhook(webhook_id)
        return trusted

    async def _trusted_webhook(self, webhook_id: int) -> tuple[bool, object | None]:
        cached = self._webhook_trust.get(webhook_id)
        if cached is not None:
            trusted, webhook, cached_at = cached
            # Trusted webhooks stay cached. Untrusted results expire so a
            # transient fetch error or a mismatch can be tried again.
            if trusted or time.monotonic() - cached_at < WEBHOOK_UNTRUSTED_SECONDS:
                return trusted, webhook
        trusted = False
        webhook = None
        try:
            fetch = getattr(self.bot, "fetch_webhook", None)
            if not callable(fetch):
                raise RuntimeError("fetch_webhook unavailable")
            webhook = await fetch(webhook_id)
            app_id = _positive_int(getattr(webhook, "application_id", None))
            if app_id is None:
                # discord.py 2.7.1 leaves application_id on the raw webhook
                # payload and does not copy it onto Webhook.
                app_id = await self._raw_webhook_application_id(webhook_id)
            bot_app = _positive_int(getattr(self.bot, "application_id", None))
            trusted = app_id is not None and bot_app is not None and app_id == bot_app
        except Exception:
            trusted = False
            webhook = None
        stored_webhook = webhook if trusted else None
        self._webhook_trust[webhook_id] = (trusted, stored_webhook, time.monotonic())
        return trusted, stored_webhook

    async def _raw_webhook_application_id(self, webhook_id: int) -> int | None:
        http = getattr(self.bot, "http", None)
        getter = getattr(http, "get_webhook", None)
        if not callable(getter):
            return None
        data = await getter(webhook_id)
        if not isinstance(data, dict):
            return None
        return _positive_int(data.get("application_id"))

    async def _missing_member(self, message, user_ids: list[int]) -> str | None:
        if not user_ids:
            return None
        guild = getattr(message, "guild", None)
        if guild is None:
            return "mention_ids require a server"
        for user_id in user_ids:
            member = None
            get_member = getattr(guild, "get_member", None)
            if callable(get_member):
                member = get_member(user_id)
            if member is None:
                fetch_member = getattr(guild, "fetch_member", None)
                if not callable(fetch_member):
                    return f"user {user_id} is not in this server"
                try:
                    member = await fetch_member(user_id)
                except (discord.NotFound, discord.HTTPException):
                    member = None
            if member is None:
                return f"user {user_id} is not in this server"
        return None

    def _log_grok_rejected(self, message, block: RemindBlock | None, reason: str) -> None:
        user = "none" if block is None else str(block.creator_id)
        ref = "none" if block is None else block.ref
        channel_id = _message_channel_id(message)
        message_id = getattr(message, "id", None)
        _log_info(
            "grok remind rejected "
            f"user={user} channel={_id_or_none(channel_id)} message={_id_or_none(message_id)} "
            f"ref={ref} reason={reason}"
        )

    async def _reject_grok(
        self,
        message,
        block: RemindBlock | None,
        log_reason: str,
        reply_reason: str | None = None,
    ) -> None:
        self._log_grok_rejected(message, block, log_reason)
        text = log_reason if reply_reason is None else reply_reason
        await self._strip_grok_block(message)
        await self._grok_reply(
            message,
            f"Couldn't set that reminder: {text}",
            allowed=discord.AllowedMentions.none(),
            view=None,
        )

    async def _grok_reply(self, message, content: str, *, allowed: discord.AllowedMentions, view) -> None:
        channel = getattr(message, "channel", None)
        send = getattr(channel, "send", None) if channel is not None else None
        kwargs: dict = {
            "allowed_mentions": allowed,
            "mention_author": False,
        }
        reference = _reply_reference(message)
        if reference is not None:
            kwargs["reference"] = reference
        if view is not None:
            kwargs["view"] = view
        try:
            if callable(send):
                await send(content, **kwargs)
                return
            log.warning(
                "Couldn't send grok remind reply for message %s: no send method",
                getattr(message, "id", None),
            )
        except Exception as exc:
            log.warning(
                "Couldn't send grok remind reply for message %s: %s",
                getattr(message, "id", None),
                exc,
            )

    async def _strip_grok_block(self, message) -> None:
        message_id = getattr(message, "id", None)
        try:
            embeds = _embeds_without_remind_block(getattr(message, "embeds", None))
        except Exception as exc:
            log.warning("Couldn't strip reminder block from message %s: %s", message_id, exc)
            return
        author_id = _positive_int(getattr(getattr(message, "author", None), "id", None))
        bot_id = _positive_int(getattr(getattr(self.bot, "user", None), "id", None))
        try:
            if bot_id is not None and author_id == bot_id:
                edit = getattr(message, "edit", None)
                if not callable(edit):
                    raise RuntimeError("message cannot be edited")
                await edit(embeds=embeds)
                return
            webhook_id = _positive_int(getattr(message, "webhook_id", None))
            if webhook_id is None:
                raise RuntimeError("webhook message has no webhook id")
            _trusted, webhook = await self._trusted_webhook(webhook_id)
            token = getattr(webhook, "token", None) if webhook is not None else None
            edit_message = getattr(webhook, "edit_message", None) if webhook is not None else None
            if not token or not callable(edit_message):
                log.warning(
                    "Couldn't strip reminder block from message %s: webhook has no token",
                    message_id,
                )
                return
            await edit_message(message_id, embeds=embeds)
        except Exception as exc:
            log.warning("Couldn't strip reminder block from message %s: %s", message_id, exc)

    async def _deliver(self, fired: FiredReminder, *, now: datetime | None = None) -> bool:
        moment = now if now is not None else discord.utils.utcnow()
        if fired.reminder.private:
            return await self._deliver_private(fired, now=moment)
        reminder = fired.reminder
        content = render_ping(fired)
        view = SnoozeView(reminder.id)
        allowed = allowed_mentions_for(reminder)
        timing = _timing(fired, moment)
        preview = _preview_text(reminder.text, private=reminder.private)
        mentions = ",".join(str(user_id) for user_id in audience_ids(reminder))
        channel, channel_error = await self._resolve_channel(reminder.channel_id)
        channel_name = getattr(channel, "name", None) if channel is not None else None
        context = _delivery_context(self.bot, reminder, channel_name=channel_name)
        if channel is None:
            _log_info(f"Channel unavailable {context} exc={channel_error or 'unavailable'} {timing} {preview}")
        else:
            ok, message_id, exc = await self._try_send(channel, content, view, allowed, reminder.id)
            if ok:
                _log_info(
                    f"Fired in channel {context} message_id={_message_id(message_id)} "
                    f"mentions={mentions} {timing} {preview}"
                )
                return True
            _log_info(
                f"Channel unavailable {context} exc={_exc_name(exc, 'unavailable')} {timing} {preview}"
            )
        user, user_error = await self._resolve_user(reminder.creator_id)
        who = _dm_who(user, reminder.creator_id)
        if user is None:
            _log_info(f"DM failed {context} {who} exc={user_error or 'unresolved'} {timing} {preview}")
        else:
            ok, message_id, exc = await self._try_send(
                user,
                "I couldn't send this in the original channel, so I'm DMing you instead.\n" + content,
                view,
                allowed,
                reminder.id,
            )
            if ok:
                _log_info(
                    f"DM delivered {context} {who} message_id={_message_id(message_id)} {timing} {preview}"
                )
                return True
            _log_info(f"DM failed {context} {who} exc={_exc_name(exc, 'send')} {timing} {preview}")
        log.warning(
            "Dropping reminder %s; channel %s is gone or unsendable and creator %s could not be DM'd.",
            reminder.id,
            reminder.channel_id,
            reminder.creator_id,
        )
        return False

    async def _deliver_private(self, fired: FiredReminder, *, now: datetime) -> bool:
        reminder = fired.reminder
        content = render_ping(fired)
        failed: list[int] = []
        timing = _timing(fired, now)
        # Private reminder text is never written to the log.
        preview = _preview_text(reminder.text, private=True)
        cached = self.bot.get_channel(reminder.channel_id)
        channel_name = getattr(cached, "name", None) if cached is not None else None
        context = _delivery_context(self.bot, reminder, channel_name=channel_name)
        for user_id in audience_ids(reminder):
            user, user_error = await self._resolve_user(user_id)
            who = _dm_who(user, user_id)
            if user is None:
                _log_info(f"DM failed {context} {who} exc={user_error or 'unresolved'} {timing} {preview}")
                failed.append(user_id)
                continue
            ok, message_id, exc = await self._try_send(
                user,
                content,
                SnoozeView(reminder.id),
                allowed_mentions_for_ids([user_id]),
                reminder.id,
            )
            if ok:
                _log_info(
                    f"DM delivered {context} {who} message_id={_message_id(message_id)} {timing} {preview}"
                )
            else:
                _log_info(f"DM failed {context} {who} exc={_exc_name(exc, 'send')} {timing} {preview}")
                failed.append(user_id)
        if not failed:
            return True
        fallback = render_private_fallback(failed)
        allowed = allowed_mentions_for_ids(failed)
        channel, channel_error = await self._resolve_channel(reminder.channel_id)
        if channel is not None:
            channel_name = getattr(channel, "name", None) or channel_name
            context = _delivery_context(self.bot, reminder, channel_name=channel_name)
        failed_ids = ",".join(str(user_id) for user_id in failed)
        if channel is None:
            _log_info(
                f"Channel unavailable {context} failed_users={failed_ids} "
                f"exc={channel_error or 'unavailable'} {timing} {preview}"
            )
        else:
            ok, message_id, exc = await self._try_send(channel, fallback, None, allowed, reminder.id)
            if ok:
                if reminder.repeat is None:
                    async with self._lock:
                        self.store.mark_missed(reminder.id, failed, now)
                _log_info(
                    f"Channel fallback sent {context} message_id={_message_id(message_id)} "
                    f"failed_users={failed_ids} {timing} {preview}"
                )
                return True
            _log_info(
                f"Channel unavailable {context} failed_users={failed_ids} "
                f"exc={_exc_name(exc, 'unavailable')} {timing} {preview}"
            )
        log.warning(
            "Dropping reminder %s; private DMs failed for %s and channel %s is unavailable.",
            reminder.id,
            failed,
            reminder.channel_id,
        )
        return False

    async def _resolve_channel(self, channel_id: int) -> tuple[object | None, str | None]:
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel, None
        fetch = getattr(self.bot, "fetch_channel", None)
        if fetch is None:
            return None, "unavailable"
        try:
            fetched = await fetch(channel_id)
        except discord.HTTPException as exc:
            return None, type(exc).__name__
        if fetched is None:
            return None, "unavailable"
        return fetched, None

    async def _resolve_user(self, user_id: int) -> tuple[object | None, str | None]:
        user = self.bot.get_user(user_id)
        if user is not None:
            return user, None
        fetch = getattr(self.bot, "fetch_user", None)
        if fetch is None:
            return None, "unresolved"
        try:
            fetched = await fetch(user_id)
        except discord.HTTPException as exc:
            return None, type(exc).__name__
        if fetched is None:
            return None, "unresolved"
        return fetched, None

    async def _try_send(
        self,
        target,
        content: str,
        view: discord.ui.View | None,
        allowed: discord.AllowedMentions,
        reminder_id: str,
    ) -> tuple[bool, int | None, BaseException | None]:
        send = getattr(target, "send", None)
        if send is None:
            return False, None, None
        kwargs: dict = {"allowed_mentions": allowed}
        if view is not None:
            kwargs["view"] = view
        try:
            sent = await send(content, **kwargs)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Failed to send reminder %s: %s", reminder_id, _one_line(exc, 300))
            return False, None, exc
        message_id = getattr(sent, "id", None)
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            message_id = None
        return True, message_id, None


def _validate_text(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise EmptyReminder()
    if len(cleaned) > MAX_TEXT:
        raise TextTooLong()
    return cleaned


def _clean_id_list(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    cleaned: list[int] = []
    for value in values:
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            continue
        if user_id not in cleaned:
            cleaned.append(user_id)
    return cleaned


def _miss_started(reminder: Reminder) -> datetime:
    if reminder.missed_at is not None:
        return as_utc(reminder.missed_at)
    return as_utc(reminder.next_fire_at)


def _miss_expired(reminder: Reminder, now_utc: datetime) -> bool:
    return now_utc - _miss_started(reminder) >= MISSED_KEEP


def _is_open_miss(reminder: Reminder, now_utc: datetime) -> bool:
    if reminder.status != "fired" or not reminder.private or reminder.repeat is not None:
        return False
    if not reminder.missed_ids:
        return False
    return not _miss_expired(reminder, now_utc)


def _clean_participants(creator_id: int, participants: list) -> list[int]:
    cleaned: list[int] = []
    for value in participants:
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            continue
        if user_id == creator_id or user_id in cleaned:
            continue
        cleaned.append(user_id)
    return cleaned


def _load_dt(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_timezones(raw: object, path: Path) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        log.warning("Reminder timezones in %s are corrupt; ignoring them.", path)
        return {}
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            log.warning("Ignoring timezone for user %s in %s.", key, path)
            continue
        try:
            resolve_timezone(value)
        except ZoneInfoNotFoundError:
            log.warning("Ignoring unknown timezone %s for user %s.", value, key)
            continue
        cleaned[str(key)] = value
    return cleaned


def _read_reminders(raw: object, path: Path) -> list[Reminder]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        log.warning("Reminder list in %s is corrupt; starting empty.", path)
        return []
    reminders: list[Reminder] = []
    for index, item in enumerate(raw):
        try:
            reminders.append(Reminder.from_dict(item))
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping reminder %s in %s: %s", index, path, exc)
    return reminders


def _snooze_deadline(timezone_name: str, delay: str, now: datetime) -> datetime:
    now_utc = as_utc(now)
    if delay == "10m":
        return now_utc + timedelta(minutes=10)
    if delay == "1h":
        return now_utc + timedelta(hours=1)
    if delay == "tomorrow":
        zone, _name = zone_or_default(timezone_name)
        local = now_utc.astimezone(zone)
        day = local.date() + timedelta(days=1)
        return datetime(day.year, day.month, day.day, 9, 0, tzinfo=zone).astimezone(timezone.utc)
    raise ValueError(delay)


def _future_repeat(reminder: Reminder, now_utc: datetime, scheduled: datetime) -> datetime:
    assert reminder.repeat is not None
    nxt = next_occurrence(
        reminder.repeat,
        now=now_utc,
        timezone_name=reminder.timezone,
        scheduled=scheduled,
    )
    if nxt <= now_utc:
        nxt = next_occurrence(
            reminder.repeat,
            now=nxt + timedelta(seconds=1),
            timezone_name=reminder.timezone,
            scheduled=nxt,
        )
    if nxt <= now_utc:
        interval = reminder.repeat.interval_seconds or 86400
        nxt = now_utc + timedelta(seconds=max(interval, 60))
    return nxt


def _parsed_from_block(block: RemindBlock, *, timezone_name: str, now: datetime) -> ParsedWhen:
    if block.repeat:
        return parse_when(block.repeat, now=now, timezone_name=timezone_name)
    assert block.when_utc is not None
    return ParsedWhen(next_fire_at=as_utc(block.when_utc), repeat=None, timezone=timezone_name)


def _dedupe_mentions(creator_id: int, mention_ids: tuple[int, ...] | list[int]) -> list[int]:
    cleaned: list[int] = []
    for user_id in mention_ids:
        if user_id == creator_id or user_id in cleaned:
            continue
        cleaned.append(user_id)
    return cleaned


def _message_has_block(message) -> bool:
    embeds = getattr(message, "embeds", None)
    if not embeds:
        return False
    for embed in embeds:
        fields = getattr(embed, "fields", None) or []
        for field in fields:
            if getattr(field, "name", None) == BLOCK_FIELD_NAME:
                return True
    return False


def _clone_embed(embed: discord.Embed) -> discord.Embed:
    """Copy an embed without sharing its field list.

    discord.py 2.7.1 Embed.copy() keeps the same _fields list, so remove_field
    would also change the message still sitting in memory.
    """
    data = embed.to_dict()
    fields = data.get("fields")
    if fields:
        data["fields"] = [dict(field) for field in fields]
    return discord.Embed.from_dict(data)


def _embeds_without_remind_block(embeds) -> list[discord.Embed]:
    cleaned: list[discord.Embed] = []
    for embed in embeds or []:
        if not isinstance(embed, discord.Embed):
            continue
        copy = _clone_embed(embed)
        index = len(copy.fields) - 1
        while index >= 0:
            if copy.fields[index].name == BLOCK_FIELD_NAME:
                copy.remove_field(index)
            index -= 1
        if _embed_visible(copy):
            cleaned.append(copy)
    return cleaned


def _embed_visible(embed: discord.Embed) -> bool:
    if embed.title or embed.description or embed.url or embed.fields:
        return True
    image = embed.image
    if image is not None and getattr(image, "url", None):
        return True
    thumbnail = embed.thumbnail
    if thumbnail is not None and getattr(thumbnail, "url", None):
        return True
    footer = embed.footer
    if footer is not None and getattr(footer, "text", None):
        return True
    author = embed.author
    if author is not None and getattr(author, "name", None):
        return True
    return False


def _reply_reference(message):
    to_reference = getattr(message, "to_reference", None)
    if callable(to_reference):
        try:
            return to_reference(fail_if_not_exists=False)
        except Exception:
            return None
    channel_id = _message_channel_id(message)
    message_id = _positive_int(getattr(message, "id", None))
    if channel_id is None or message_id is None:
        return None
    return discord.MessageReference(
        message_id=message_id,
        channel_id=channel_id,
        guild_id=_message_guild_id(message),
        fail_if_not_exists=False,
    )


def _message_channel_id(message) -> int | None:
    channel = getattr(message, "channel", None)
    if channel is not None:
        channel_id = _positive_int(getattr(channel, "id", None))
        if channel_id is not None:
            return channel_id
    return _positive_int(getattr(message, "channel_id", None))


def _message_guild_id(message) -> int | None:
    guild = getattr(message, "guild", None)
    if guild is not None:
        guild_id = _positive_int(getattr(guild, "id", None))
        if guild_id is not None:
            return guild_id
    return _positive_int(getattr(message, "guild_id", None))


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _id_or_none(value: int | None) -> str:
    return "none" if value is None else str(value)


def _grok_source(bot, message) -> str:
    bot_id = _positive_int(getattr(getattr(bot, "user", None), "id", None))
    author_id = _positive_int(getattr(getattr(message, "author", None), "id", None))
    if bot_id is not None and author_id == bot_id:
        return "interaction"
    return "webhook"


def _chunk_lines(lines: list[str], limit: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        piece = line if not current else f"{current}\n{line}"
        if current and len(piece) > limit:
            chunks.append(current)
            current = line
        else:
            current = piece
    if current:
        chunks.append(current)
    return chunks


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reminders(bot))
