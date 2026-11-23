"""Aggregate weekly stats. No message text, prompts, or per-person totals.

Counters live in an in-memory buffer until ``flush`` writes every pending
delta in one transaction. Weekly columns reset in ``close_week``. All-time
columns do not. This week's active-member list is cleared with the week.
Opted-out users are removed from that list and are not marked active again.
Channel, emoji, voice, and heatmap totals still count, because those rows
have no user id.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cogs.utils.constants import DATA_DIR
from cogs.utils.stats_commands import is_owner_only_name
from cogs.utils.stats_schedule import LA, split_voice_minutes, week_start_for

DEFAULT_DB_PATH = DATA_DIR / "weekly_stats.sqlite3"
TOP_N = 10
COMMAND_LIMIT = 5
DEAD_LIMIT = 15
IMAGE_TOP_N = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_messages (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    weekly INTEGER NOT NULL DEFAULT 0,
    all_time INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS reaction_counts (
    guild_id INTEGER NOT NULL,
    emoji_key TEXT NOT NULL,
    emoji_name TEXT NOT NULL,
    is_custom INTEGER NOT NULL DEFAULT 0,
    weekly INTEGER NOT NULL DEFAULT 0,
    all_time INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, emoji_key)
);
CREATE TABLE IF NOT EXISTS command_counts (
    guild_id INTEGER NOT NULL,
    command_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    weekly INTEGER NOT NULL DEFAULT 0,
    all_time INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, command_name, kind)
);
CREATE TABLE IF NOT EXISTS voice_channels (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    weekly INTEGER NOT NULL DEFAULT 0,
    all_time INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS activity_heatmap (
    guild_id INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    hour INTEGER NOT NULL,
    weekly INTEGER NOT NULL DEFAULT 0,
    all_time INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, weekday, hour)
);
CREATE TABLE IF NOT EXISTS voice_heatmap (
    guild_id INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    hour INTEGER NOT NULL,
    weekly INTEGER NOT NULL DEFAULT 0,
    all_time INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, weekday, hour)
);
CREATE TABLE IF NOT EXISTS imagegen_counts (
    guild_id INTEGER NOT NULL,
    dimension TEXT NOT NULL,
    item_key TEXT NOT NULL,
    weekly INTEGER NOT NULL DEFAULT 0,
    all_time INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, dimension, item_key)
);
CREATE TABLE IF NOT EXISTS imagegen_totals (
    guild_id INTEGER NOT NULL,
    metric TEXT NOT NULL,
    weekly REAL NOT NULL DEFAULT 0,
    all_time REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, metric)
);
CREATE TABLE IF NOT EXISTS active_members (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS weekly_snapshots (
    guild_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    PRIMARY KEY (guild_id, week_start)
);
CREATE TABLE IF NOT EXISTS guild_state (
    guild_id INTEGER PRIMARY KEY,
    last_posted_week_start TEXT
);
CREATE TABLE IF NOT EXISTS applied_corrections (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS opt_outs (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS backfill_span (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    low_id INTEGER NOT NULL,
    high_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id, low_id)
);
CREATE TABLE IF NOT EXISTS backfill_week (
    guild_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    kind TEXT NOT NULL,
    item_key TEXT NOT NULL,
    item_label TEXT NOT NULL DEFAULT '',
    flag INTEGER NOT NULL DEFAULT 0,
    amount INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, week_start, kind, item_key)
);
CREATE TABLE IF NOT EXISTS backfill_meta (
    guild_id INTEGER PRIMARY KEY,
    live_at TEXT
);
CREATE TABLE IF NOT EXISTS weekly_card_state (
    guild_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (guild_id, week_start)
);
CREATE TABLE IF NOT EXISTS history_applied (
    guild_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, source, message_id)
);
"""

_ZERO_TABLES = (
    "channel_messages",
    "reaction_counts",
    "command_counts",
    "voice_channels",
    "activity_heatmap",
    "voice_heatmap",
    "imagegen_counts",
    "imagegen_totals",
)


_DISCORD_EPOCH_MS = 1_420_070_400_000


def _snowflake_date(message_id: int) -> date:
    millis = (int(message_id) >> 22) + _DISCORD_EPOCH_MS
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).astimezone(LA).date()


def _as_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _clip(value: str, limit: int = 80) -> str:
    text = str(value).replace("\n", " ").strip()
    return text[:limit]


@dataclass(frozen=True)
class ChannelCount:
    channel_id: int
    weekly: int
    all_time: int


@dataclass(frozen=True)
class ReactionCount:
    emoji_key: str
    name: str
    is_custom: bool
    weekly: int
    all_time: int


@dataclass(frozen=True)
class CommandCount:
    name: str
    kind: str
    weekly: int
    all_time: int


@dataclass(frozen=True)
class DimCount:
    key: str
    weekly: int
    all_time: int


@dataclass(frozen=True)
class ImageGenSummary:
    models: tuple[DimCount, ...]
    loras: tuple[DimCount, ...]
    edit_modes: tuple[DimCount, ...]
    resolutions: tuple[DimCount, ...]
    steps: tuple[DimCount, ...]
    samplers: tuple[DimCount, ...]
    avg_seconds: float | None
    success_rate: float | None
    error_rate: float | None
    success_count: int
    error_count: int
    regenerate_count: int
    edit_count: int
    kept_first: int
    generation_count: int
    generation_count_all_time: int = 0
    success_count_all_time: int = 0
    error_count_all_time: int = 0


def _empty_imagegen() -> ImageGenSummary:
    return ImageGenSummary(
        models=(),
        loras=(),
        edit_modes=(),
        resolutions=(),
        steps=(),
        samplers=(),
        avg_seconds=None,
        success_rate=None,
        error_rate=None,
        success_count=0,
        error_count=0,
        regenerate_count=0,
        edit_count=0,
        kept_first=0,
        generation_count=0,
    )


@dataclass(frozen=True)
class WeeklyStats:
    guild_id: int
    week_start: date
    generated_at: datetime
    top_channels: tuple[ChannelCount, ...]
    bottom_channels: tuple[ChannelCount, ...]
    dead_channels: tuple[ChannelCount, ...]
    top_reactions: tuple[ReactionCount, ...]
    least_custom_emojis: tuple[ReactionCount, ...]
    top_commands: tuple[CommandCount, ...]
    bottom_commands: tuple[CommandCount, ...]
    imagegen: ImageGenSummary
    active_members: int
    active_members_last_week: int | None
    heatmap: tuple[tuple[int, ...], ...]
    voice_minutes: int
    voice_minutes_all_time: int
    top_voice_channels: tuple[ChannelCount, ...]
    voice_heatmap: tuple[tuple[int, ...], ...]
    messages_weekly: int
    messages_all_time: int
    reactions_weekly: int
    reactions_all_time: int
    commands_weekly: int
    commands_all_time: int
    # Previous closed week, when a snapshot exists. None means there is no prior week.
    messages_last_week: int | None = None
    reactions_last_week: int | None = None
    voice_minutes_last_week: int | None = None
    imagegen_last_week: int | None = None
    # All-time card. Defaults keep older constructors on the weekly card.
    heatmap_all_time: tuple[tuple[int, ...], ...] = ()
    voice_heatmap_all_time: tuple[tuple[int, ...], ...] = ()
    week_history: tuple = ()
    earliest_week: date | None = None
    live_since: date | None = None
    imagegen_since: date | None = None
    top_channels_all_time: tuple[ChannelCount, ...] = ()
    bottom_channels_all_time: tuple[ChannelCount, ...] = ()
    top_reactions_all_time: tuple[ReactionCount, ...] = ()
    least_custom_emojis_weekly: tuple[ReactionCount, ...] = ()
    top_commands_all_time: tuple[CommandCount, ...] = ()
    bottom_commands_all_time: tuple[CommandCount, ...] = ()
    top_voice_channels_all_time: tuple[ChannelCount, ...] = ()

    @property
    def week_end(self) -> date:
        return self.week_start + timedelta(days=6)


# Image gen and voice were not backfilled. Live tracking starts on this day
# unless backfill_meta.live_at names an earlier or later moment.
LIVE_TRACKING_SINCE = date(2026, 11, 19)


@dataclass(frozen=True)
class WeekPoint:
    week_start: date
    messages: int
    reactions: int
    active_members: int
    commands: int


class _Buffer:
    """Pending deltas. Nothing here is written until flush."""

    def __init__(self) -> None:
        self.channels: dict[tuple[int, int], int] = {}
        self.reactions: dict[tuple[int, str], list] = {}
        self.commands: dict[tuple[int, str, str], int] = {}
        self.voice_channels: dict[tuple[int, int], int] = {}
        self.heatmap: dict[tuple[int, int, int], int] = {}
        self.voice_heatmap: dict[tuple[int, int, int], int] = {}
        self.imagegen: dict[tuple[int, str, str], int] = {}
        self.imagegen_totals: dict[tuple[int, str], float] = {}
        self.active: set[tuple[int, int]] = set()

    def empty(self) -> bool:
        return not (
            self.channels
            or self.reactions
            or self.commands
            or self.voice_channels
            or self.heatmap
            or self.voice_heatmap
            or self.imagegen
            or self.imagegen_totals
            or self.active
        )

    def clear(self) -> None:
        self.__init__()

    def add_channel(self, guild_id: int, channel_id: int, amount: int = 1) -> None:
        key = (guild_id, channel_id)
        self.channels[key] = self.channels.get(key, 0) + amount

    def add_reaction(self, guild_id: int, emoji_key: str, name: str, is_custom: bool, amount: int = 1) -> None:
        key = (guild_id, emoji_key)
        current = self.reactions.get(key)
        if current is None:
            self.reactions[key] = [amount, name, bool(is_custom)]
            return
        current[0] += amount
        current[1] = name
        current[2] = bool(is_custom)

    def add_command(self, guild_id: int, name: str, kind: str, amount: int = 1) -> None:
        key = (guild_id, name, kind)
        self.commands[key] = self.commands.get(key, 0) + amount

    def add_voice_channel(self, guild_id: int, channel_id: int, minutes: int) -> None:
        key = (guild_id, channel_id)
        self.voice_channels[key] = self.voice_channels.get(key, 0) + minutes

    def add_heatmap(self, guild_id: int, weekday: int, hour: int, amount: int = 1) -> None:
        key = (guild_id, weekday, hour)
        self.heatmap[key] = self.heatmap.get(key, 0) + amount

    def add_voice_heatmap(self, guild_id: int, weekday: int, hour: int, minutes: int) -> None:
        key = (guild_id, weekday, hour)
        self.voice_heatmap[key] = self.voice_heatmap.get(key, 0) + minutes

    def add_imagegen(self, guild_id: int, dimension: str, item_key: str, amount: int = 1) -> None:
        key = (guild_id, dimension, item_key)
        self.imagegen[key] = self.imagegen.get(key, 0) + amount

    def add_imagegen_total(self, guild_id: int, metric: str, amount: float) -> None:
        key = (guild_id, metric)
        self.imagegen_totals[key] = self.imagegen_totals.get(key, 0.0) + amount

    def add_active(self, guild_id: int, user_id: int) -> None:
        self.active.add((guild_id, user_id))

    def drop_user(self, guild_id: int, user_id: int) -> None:
        self.active.discard((guild_id, user_id))


def _known_emoji_pairs(known) -> list[tuple[str, str]]:
    if not known:
        return []
    pairs: list[tuple[str, str]] = []
    for item in known:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            pairs.append((str(item[0]), str(item[1])))
            continue
        if isinstance(item, dict):
            key = item.get("id", item.get("key"))
            if key is None:
                continue
            pairs.append((str(key), str(item.get("name") or key)))
            continue
        emoji_id = getattr(item, "id", None)
        if emoji_id is None:
            continue
        pairs.append((str(emoji_id), str(getattr(item, "name", None) or emoji_id)))
    return pairs


def _summary_payload(stats: WeeklyStats) -> dict:
    """Small snapshot. Totals, short top lists, and the active-member count."""
    return {
        "active_members": stats.active_members,
        "messages": stats.messages_weekly,
        "reactions": stats.reactions_weekly,
        "voice_minutes": stats.voice_minutes,
        "commands": stats.commands_weekly,
        "imagegen_success": stats.imagegen.success_count,
        "imagegen_error": stats.imagegen.error_count,
        "top_channels": [
            {"id": row.channel_id, "weekly": row.weekly} for row in stats.top_channels[:5]
        ],
        "top_commands": [
            {"name": row.name, "kind": row.kind, "weekly": row.weekly} for row in stats.top_commands[:5]
        ],
        "top_reactions": [
            {"key": row.emoji_key, "weekly": row.weekly} for row in stats.top_reactions[:5]
        ],
    }


@dataclass(frozen=True)
class BackfillPart:
    """One week of counts already filtered to messages inside a new span.

    ``active_ids`` are current-week authors only. ``member_delta`` is the
    number of newly seen historical authors. Those ids are not stored.
    """

    into_current_week: bool
    week_start: date
    messages: int = 0
    reactions: tuple = ()
    commands: tuple = ()
    heatmap: tuple = ()
    active_ids: tuple = ()
    member_delta: int = 0


class StatsStore:
    def __init__(self, path: str | Path | None = None, *, busy_timeout_ms: int | None = None) -> None:
        if path is None:
            self.path = Path(DEFAULT_DB_PATH)
        else:
            self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        timeout_s = 5.0 if not busy_timeout_ms else max(5.0, float(busy_timeout_ms) / 1000.0)
        self._conn = sqlite3.connect(self.path, timeout=timeout_s, isolation_level=None)
        if busy_timeout_ms:
            self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # Earlier builds stored per-person totals. Drop that table if it is still here.
        self._conn.execute("DROP TABLE IF EXISTS user_counters")
        self._buffer = _Buffer()
        self._opted: set[tuple[int, int]] = set()
        self._load_opt_outs()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._conn.close()

    @contextmanager
    def _transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _load_opt_outs(self) -> None:
        rows = self._conn.execute("SELECT guild_id, user_id FROM opt_outs").fetchall()
        self._opted = {(int(row["guild_id"]), int(row["user_id"])) for row in rows}

    def is_opted_out(self, guild_id: int, user_id: int) -> bool:
        return (int(guild_id), int(user_id)) in self._opted

    def opt_out(self, guild_id: int, user_id: int) -> None:
        """Opt this user out and remove them from this week's active members.

        Later messages and reactions still move aggregate totals. They do not
        mark this user active.
        """
        guild_id = int(guild_id)
        user_id = int(user_id)
        self._opted.add((guild_id, user_id))
        self._buffer.drop_user(guild_id, user_id)
        with self._transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO opt_outs (guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
            self._conn.execute(
                "DELETE FROM active_members WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )

    def opt_in(self, guild_id: int, user_id: int) -> None:
        guild_id = int(guild_id)
        user_id = int(user_id)
        self._opted.discard((guild_id, user_id))
        with self._transaction():
            self._conn.execute(
                "DELETE FROM opt_outs WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )

    def record_message(self, guild_id: int, channel_id: int, user_id: int, when: datetime) -> bool:
        """Count one message toward the channel total and the activity heatmap.

        Every message counts. Opted-out authors are not marked active.
        Returns True.
        """
        guild_id = int(guild_id)
        channel_id = int(channel_id)
        user_id = int(user_id)
        when = _as_aware(when)
        self._buffer.add_channel(guild_id, channel_id, 1)
        local = when.astimezone(LA)
        self._buffer.add_heatmap(guild_id, local.weekday(), local.hour, 1)
        if not self.is_opted_out(guild_id, user_id):
            self._buffer.add_active(guild_id, user_id)
        return True

    def record_reaction(
        self,
        guild_id: int,
        user_id: int | None,
        emoji_key: str,
        *,
        name: str,
        is_custom: bool,
        when: datetime,
    ) -> None:
        guild_id = int(guild_id)
        key = _clip(emoji_key, 100) or "?"
        label = _clip(name or key, 80) or key
        when = _as_aware(when)
        self._buffer.add_reaction(guild_id, key, label, bool(is_custom), 1)
        local = when.astimezone(LA)
        self._buffer.add_heatmap(guild_id, local.weekday(), local.hour, 1)
        if user_id is None:
            return
        user_id = int(user_id)
        if self.is_opted_out(guild_id, user_id):
            return
        self._buffer.add_active(guild_id, user_id)

    def record_command(self, guild_id: int, name: str, kind: str) -> None:
        if kind not in {"slash", "prefix"}:
            return
        label = _clip(name, 80)
        if not label or is_owner_only_name(label):
            return
        self._buffer.add_command(int(guild_id), label, kind, 1)

    def record_voice(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int | None,
        start: datetime,
        end: datetime,
    ) -> int:
        """Add whole minutes for a closed voice session. Returns minutes stored.

        Channel minutes and the voice heatmap always count. ``user_id`` is the
        in-memory session owner and is not stored. Voice time does not mark a
        member active: active members are people who posted or reacted.
        """
        parts = split_voice_minutes(start, end)
        total = sum(minutes for _, _, minutes in parts)
        if total <= 0:
            return 0
        guild_id = int(guild_id)
        channel_id = int(channel_id)
        self._buffer.add_voice_channel(guild_id, channel_id, total)
        for weekday, hour, minutes in parts:
            self._buffer.add_voice_heatmap(guild_id, weekday, hour, minutes)
        return total

    def record_imagegen(
        self,
        guild_id: int,
        *,
        model: str,
        loras: tuple | list = (),
        edit_mode: str | None = None,
        width: int | None = None,
        height: int | None = None,
        steps: int | None = None,
        sampler: str | None = None,
        seconds: float | None = None,
        success: bool = True,
    ) -> None:
        guild_id = int(guild_id)
        model_name = _clip(model, 80) if model else ""
        if model_name:
            self._buffer.add_imagegen(guild_id, "model", model_name, 1)
        for lora in loras or ():
            lora_name = _clip(lora, 80) if lora else ""
            if lora_name:
                self._buffer.add_imagegen(guild_id, "lora", lora_name, 1)
        if edit_mode:
            self._buffer.add_imagegen(guild_id, "edit_mode", _clip(edit_mode, 80), 1)
        if width is not None and height is not None:
            self._buffer.add_imagegen(guild_id, "resolution", f"{int(width)}x{int(height)}", 1)
        if steps is not None:
            self._buffer.add_imagegen(guild_id, "steps", str(int(steps)), 1)
        if sampler:
            self._buffer.add_imagegen(guild_id, "sampler", _clip(sampler, 80), 1)
        self._buffer.add_imagegen(guild_id, "outcome", "success" if success else "error", 1)
        self._buffer.add_imagegen_total(guild_id, "generations", 1)
        if seconds is not None:
            self._buffer.add_imagegen_total(guild_id, "seconds", float(seconds))
            self._buffer.add_imagegen_total(guild_id, "timed", 1)

    def record_imagegen_followup(self, guild_id: int, kind: str) -> None:
        """Count a regenerate or edit click. ``kept`` is computed when the card is built."""
        if kind == "kept":
            return
        if kind not in {"regenerate", "edit"}:
            return
        self._buffer.add_imagegen(int(guild_id), "followup", kind, 1)

    def flush(self) -> None:
        """Write every pending delta in one transaction."""
        if self._buffer.empty():
            return
        with self._transaction():
            self._write_buffer(self._buffer)
        self._buffer.clear()

    def backfill_spans(self, guild_id: int, channel_id: int) -> list[tuple[int, int]]:
        rows = self._conn.execute(
            """
            SELECT low_id, high_id FROM backfill_span
            WHERE guild_id = ? AND channel_id = ?
            ORDER BY low_id
            """,
            (int(guild_id), int(channel_id)),
        ).fetchall()
        return [(int(row["low_id"]), int(row["high_id"])) for row in rows]

    def recorded_live_at(self, guild_id: int) -> datetime | None:
        row = self._conn.execute(
            "SELECT live_at FROM backfill_meta WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchone()
        if row is None or not row["live_at"]:
            return None
        parsed = datetime.fromisoformat(str(row["live_at"]))
        return _as_aware(parsed)

    def apply_backfill(
        self,
        guild_id: int,
        channel_id: int,
        span: tuple[int, int],
        parts: list[BackfillPart] | tuple[BackfillPart, ...],
        closed_at: datetime | None = None,
        command_map: dict[str, str] | None = None,
    ) -> bool:
        """Apply one newly fetched inclusive snowflake span.

        Returns False when that span was already covered. A covered span does
        not change counters, snapshots, active members, or last_posted.

        Current-week parts add to weekly and all_time and mark authors active.
        Historical parts add to all_time only (weekly += 0), keep a week bucket,
        and replace that week's snapshot. They do not mark active members, do
        not change last_posted, and do not zero weekly columns.
        """
        guild_id = int(guild_id)
        channel_id = int(channel_id)
        low, high = int(span[0]), int(span[1])
        if high < low:
            return False
        if self._span_covered(guild_id, channel_id, low, high):
            return False
        closed = _as_aware(closed_at or datetime.now(timezone.utc))
        weeks: set[date] = set()
        with self._transaction():
            # Re-check inside the write lock. A concurrent flush cannot cover
            # this span, but a second backfill connection could.
            if self._span_covered(guild_id, channel_id, low, high):
                return False
            for part in parts:
                week = _as_date(part.week_start)
                weekly_amount = int(part.messages) if part.into_current_week else 0
                all_amount = int(part.messages)
                if all_amount:
                    self._upsert_split(
                        "channel_messages",
                        ("guild_id", "channel_id"),
                        (guild_id, channel_id),
                        weekly_amount,
                        all_amount,
                    )
                    if not part.into_current_week:
                        self._add_week_bucket(guild_id, week, "channel", str(channel_id), "", 0, all_amount)
                for raw in part.reactions or ():
                    key, name, is_custom, count = raw[0], raw[1], raw[2], int(raw[3])
                    if count <= 0:
                        continue
                    emoji_key = _clip(str(key), 100) or "?"
                    label = _clip(str(name or emoji_key), 80) or emoji_key
                    weekly_amount = count if part.into_current_week else 0
                    self._upsert_reaction(guild_id, emoji_key, label, bool(is_custom), weekly_amount, count)
                    if not part.into_current_week:
                        self._add_week_bucket(
                            guild_id,
                            week,
                            "reaction",
                            emoji_key,
                            label,
                            1 if is_custom else 0,
                            count,
                        )
                merged_commands: dict[str, int] = {}
                for raw in part.commands or ():
                    name, count = raw[0], int(raw[1])
                    label = _clip(str(name), 80)
                    if not label or count <= 0:
                        continue
                    if command_map is not None:
                        canonical = command_map.get(label.casefold())
                        if not canonical:
                            continue
                        label = _clip(canonical, 80)
                        if not label:
                            continue
                    if is_owner_only_name(label):
                        continue
                    merged_commands[label] = merged_commands.get(label, 0) + count
                for label, count in merged_commands.items():
                    weekly_amount = count if part.into_current_week else 0
                    self._upsert_split(
                        "command_counts",
                        ("guild_id", "command_name", "kind"),
                        (guild_id, label, "prefix"),
                        weekly_amount,
                        count,
                    )
                    if not part.into_current_week:
                        self._add_week_bucket(guild_id, week, "command", label, "prefix", 0, count)
                for raw in part.heatmap or ():
                    weekday, hour, count = int(raw[0]), int(raw[1]), int(raw[2])
                    if count <= 0 or not (0 <= weekday <= 6 and 0 <= hour <= 23):
                        continue
                    weekly_amount = count if part.into_current_week else 0
                    self._upsert_split(
                        "activity_heatmap",
                        ("guild_id", "weekday", "hour"),
                        (guild_id, weekday, hour),
                        weekly_amount,
                        count,
                    )
                    if not part.into_current_week:
                        self._add_week_bucket(guild_id, week, "heat", f"{weekday}:{hour}", "", 0, count)
                if part.into_current_week:
                    for user_id in part.active_ids or ():
                        user_id = int(user_id)
                        if self.is_opted_out(guild_id, user_id):
                            continue
                        self._conn.execute(
                            "INSERT OR IGNORE INTO active_members (guild_id, user_id) VALUES (?, ?)",
                            (guild_id, user_id),
                        )
                elif int(part.member_delta) > 0:
                    self._add_week_bucket(guild_id, week, "members", "n", "", 0, int(part.member_delta))
                if not part.into_current_week:
                    weeks.add(week)
            self._add_span(guild_id, channel_id, low, high)
            for week in weeks:
                self._write_backfill_snapshot(guild_id, week, closed)
        return True

    def write_historical_snapshot(self, guild_id: int, week_start: date | datetime | str, closed_at: datetime) -> None:
        """Replace one historical snapshot from the stored week buckets.

        Does not change weekly counters, all_time counters, active members, or
        last_posted. Used so a completed week still has a row when it was empty.
        """
        guild_id = int(guild_id)
        week = _as_date(week_start)
        closed = _as_aware(closed_at)
        with self._transaction():
            self._write_backfill_snapshot(guild_id, week, closed)

    def _span_covered(self, guild_id: int, channel_id: int, low: int, high: int) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM backfill_span
            WHERE guild_id = ? AND channel_id = ? AND low_id <= ? AND high_id >= ?
            LIMIT 1
            """,
            (guild_id, channel_id, low, high),
        ).fetchone()
        return row is not None

    def _add_span(self, guild_id: int, channel_id: int, low: int, high: int) -> None:
        rows = self._conn.execute(
            "SELECT low_id, high_id FROM backfill_span WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchall()
        spans = [(int(row["low_id"]), int(row["high_id"])) for row in rows]
        spans.append((low, high))
        spans.sort()
        merged: list[tuple[int, int]] = []
        for lo, hi in spans:
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        self._conn.execute(
            "DELETE FROM backfill_span WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        self._conn.executemany(
            "INSERT INTO backfill_span (guild_id, channel_id, low_id, high_id) VALUES (?, ?, ?, ?)",
            [(guild_id, channel_id, lo, hi) for lo, hi in merged],
        )

    def _add_week_bucket(
        self,
        guild_id: int,
        week: date,
        kind: str,
        item_key: str,
        item_label: str,
        flag: int,
        amount: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO backfill_week
                (guild_id, week_start, kind, item_key, item_label, flag, amount)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, week_start, kind, item_key) DO UPDATE SET
                amount = amount + excluded.amount,
                item_label = excluded.item_label,
                flag = excluded.flag
            """,
            (guild_id, week.isoformat(), kind, item_key, item_label, int(flag), int(amount)),
        )

    def _upsert_reaction(
        self,
        guild_id: int,
        emoji_key: str,
        name: str,
        is_custom: bool,
        weekly: int,
        all_time: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO reaction_counts
                (guild_id, emoji_key, emoji_name, is_custom, weekly, all_time)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, emoji_key) DO UPDATE SET
                weekly = weekly + excluded.weekly,
                all_time = all_time + excluded.all_time,
                emoji_name = excluded.emoji_name,
                is_custom = excluded.is_custom
            """,
            (guild_id, emoji_key, name, 1 if is_custom else 0, weekly, all_time),
        )

    def _write_backfill_snapshot(self, guild_id: int, week: date, closed_at: datetime) -> None:
        rows = self._conn.execute(
            """
            SELECT kind, item_key, item_label, flag, amount
            FROM backfill_week
            WHERE guild_id = ? AND week_start = ?
            """,
            (guild_id, week.isoformat()),
        ).fetchall()
        channels = []
        reactions = []
        commands = []
        messages = 0
        reaction_total = 0
        command_total = 0
        members = 0
        for row in rows:
            kind = str(row["kind"])
            amount = int(row["amount"])
            if kind == "channel":
                messages += amount
                channels.append(ChannelCount(int(row["item_key"]), amount, amount))
            elif kind == "reaction":
                reaction_total += amount
                reactions.append(
                    ReactionCount(str(row["item_key"]), str(row["item_label"] or row["item_key"]), bool(row["flag"]), amount, amount)
                )
            elif kind == "command":
                command_total += amount
                commands.append(CommandCount(str(row["item_key"]), "prefix", amount, amount))
            elif kind == "members":
                members += amount
        channels.sort(key=lambda row: (-row.weekly, row.channel_id))
        reactions.sort(key=lambda row: (-row.weekly, row.emoji_key))
        commands.sort(key=lambda row: (-row.weekly, row.name))
        blank = tuple(tuple(0 for _ in range(24)) for _ in range(7))
        stats = WeeklyStats(
            guild_id=guild_id,
            week_start=week,
            generated_at=closed_at,
            top_channels=tuple(channels[:5]),
            bottom_channels=(),
            dead_channels=(),
            top_reactions=tuple(reactions[:5]),
            least_custom_emojis=(),
            top_commands=tuple(commands[:5]),
            bottom_commands=(),
            imagegen=_empty_imagegen(),
            active_members=members,
            active_members_last_week=None,
            heatmap=blank,
            voice_minutes=0,
            voice_minutes_all_time=0,
            top_voice_channels=(),
            voice_heatmap=blank,
            messages_weekly=messages,
            messages_all_time=messages,
            reactions_weekly=reaction_total,
            reactions_all_time=reaction_total,
            commands_weekly=command_total,
            commands_all_time=command_total,
        )
        self._save_snapshot(guild_id, week, closed_at, stats)

    def _upsert_split(self, table: str, columns: tuple[str, ...], values: tuple, weekly: int, all_time: int) -> None:
        column_sql = ", ".join((*columns, "weekly", "all_time"))
        placeholders = ", ".join("?" for _ in (*columns, "weekly", "all_time"))
        conflict = ", ".join(columns)
        self._conn.execute(
            f"""
            INSERT INTO {table} ({column_sql})
            VALUES ({placeholders})
            ON CONFLICT({conflict}) DO UPDATE SET
                weekly = weekly + excluded.weekly,
                all_time = all_time + excluded.all_time
            """,
            (*values, weekly, all_time),
        )

    def _write_buffer(self, buffer: _Buffer) -> None:
        for (guild_id, channel_id), amount in buffer.channels.items():
            self._upsert("channel_messages", ("guild_id", "channel_id"), (guild_id, channel_id), amount)
        for (guild_id, emoji_key), (amount, name, is_custom) in buffer.reactions.items():
            self._conn.execute(
                """
                INSERT INTO reaction_counts
                    (guild_id, emoji_key, emoji_name, is_custom, weekly, all_time)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, emoji_key) DO UPDATE SET
                    weekly = weekly + excluded.weekly,
                    all_time = all_time + excluded.all_time,
                    emoji_name = excluded.emoji_name,
                    is_custom = excluded.is_custom
                """,
                (guild_id, emoji_key, name, 1 if is_custom else 0, amount, amount),
            )
        for (guild_id, name, kind), amount in buffer.commands.items():
            self._upsert(
                "command_counts",
                ("guild_id", "command_name", "kind"),
                (guild_id, name, kind),
                amount,
            )
        for (guild_id, channel_id), amount in buffer.voice_channels.items():
            self._upsert("voice_channels", ("guild_id", "channel_id"), (guild_id, channel_id), amount)
        for (guild_id, weekday, hour), amount in buffer.heatmap.items():
            self._upsert(
                "activity_heatmap",
                ("guild_id", "weekday", "hour"),
                (guild_id, weekday, hour),
                amount,
            )
        for (guild_id, weekday, hour), amount in buffer.voice_heatmap.items():
            self._upsert(
                "voice_heatmap",
                ("guild_id", "weekday", "hour"),
                (guild_id, weekday, hour),
                amount,
            )
        for (guild_id, dimension, item_key), amount in buffer.imagegen.items():
            self._upsert(
                "imagegen_counts",
                ("guild_id", "dimension", "item_key"),
                (guild_id, dimension, item_key),
                amount,
            )
        for (guild_id, metric), amount in buffer.imagegen_totals.items():
            self._upsert_amount(
                "imagegen_totals",
                ("guild_id", "metric"),
                (guild_id, metric),
                amount,
            )
        for guild_id, user_id in buffer.active:
            self._conn.execute(
                "INSERT OR IGNORE INTO active_members (guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )

    def _upsert(self, table: str, columns: tuple[str, ...], values: tuple, amount: int) -> None:
        self._upsert_amount(table, columns, values, amount)

    def _upsert_amount(self, table: str, columns: tuple[str, ...], values: tuple, amount: float) -> None:
        column_sql = ", ".join((*columns, "weekly", "all_time"))
        placeholders = ", ".join("?" for _ in (*columns, "weekly", "all_time"))
        conflict = ", ".join(columns)
        self._conn.execute(
            f"""
            INSERT INTO {table} ({column_sql})
            VALUES ({placeholders})
            ON CONFLICT({conflict}) DO UPDATE SET
                weekly = weekly + excluded.weekly,
                all_time = all_time + excluded.all_time
            """,
            (*values, amount, amount),
        )

    def last_posted_week_start(self, guild_id: int) -> date | None:
        row = self._conn.execute(
            "SELECT last_posted_week_start FROM guild_state WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchone()
        if row is None or row["last_posted_week_start"] is None:
            return None
        return date.fromisoformat(row["last_posted_week_start"])

    def ensure_baseline(self, guild_id: int, week_start: date | datetime | str) -> bool:
        """Remember ``week_start`` as already posted, without a card or a reset.

        Used the first time an enabled guild is seen, so the empty catch-up
        card is skipped. Weekly counters are left alone. Returns False when
        this guild already has a posted week.
        """
        guild_id = int(guild_id)
        if self.last_posted_week_start(guild_id) is not None:
            return False
        week = _as_date(week_start)
        with self._transaction():
            self._set_last_posted(guild_id, week)
        return True

    def build_stats(
        self,
        guild_id: int,
        now: datetime,
        *,
        week_start: date | datetime | str | None = None,
        known_custom_emojis=None,
        known_channel_ids=None,
        command_map: dict[str, str] | None = None,
    ) -> WeeklyStats:
        """Build the current card.

        ``known_channel_ids`` limits channel lists to channels that still
        exist. Dead channels are then the known channels with no weekly
        messages, including ones never stored. ``known_custom_emojis`` limits
        the least-used list to emojis that still exist, including unused ones.
        None for either argument keeps the stored rows only.
        """
        self.flush()
        moment = _as_aware(now)
        week = _as_date(week_start) if week_start is not None else week_start_for(moment)
        return self._build(
            int(guild_id),
            week,
            moment,
            known_custom_emojis,
            known_channel_ids,
            command_map,
        )

    def purge_unregistered_commands(self, mapping: dict[str, str]) -> list[dict]:
        """Drop prefix commands that are not in ``mapping``. Merge aliases.

        ``command_counts`` prefix rows and ``backfill_week`` rows with
        ``kind="command"`` are the only counter tables changed. Each
        ``weekly_snapshots`` summary then gets ``commands`` and
        ``top_commands`` from the cleaned command buckets for that week.
        Returns the deleted counter rows. Alias rows are merged, not listed.
        """
        self.flush()
        dropped: list[dict] = []
        with self._transaction():
            rows = self._conn.execute(
                """
                SELECT guild_id, command_name, kind, weekly, all_time
                FROM command_counts
                """
            ).fetchall()
            prefix_by_guild: dict[int, list] = {}
            for row in rows:
                if str(row["kind"]) != "prefix":
                    continue
                prefix_by_guild.setdefault(int(row["guild_id"]), []).append(row)
            for guild_id, guild_rows in prefix_by_guild.items():
                merged: dict[str, list[int]] = {}
                for row in guild_rows:
                    name = str(row["command_name"])
                    weekly = int(row["weekly"])
                    all_time = int(row["all_time"])
                    canonical = mapping.get(name.casefold()) if mapping else None
                    if not canonical:
                        dropped.append(
                            {"name": name, "kind": "prefix", "weekly": weekly, "all_time": all_time}
                        )
                        continue
                    canonical = _clip(canonical, 80) or canonical
                    slot = merged.get(canonical)
                    if slot is None:
                        merged[canonical] = [weekly, all_time]
                    else:
                        slot[0] += weekly
                        slot[1] += all_time
                self._conn.execute(
                    "DELETE FROM command_counts WHERE guild_id = ? AND kind = 'prefix'",
                    (guild_id,),
                )
                for name, (weekly, all_time) in merged.items():
                    self._conn.execute(
                        """
                        INSERT INTO command_counts
                            (guild_id, command_name, kind, weekly, all_time)
                        VALUES (?, ?, 'prefix', ?, ?)
                        """,
                        (guild_id, name, weekly, all_time),
                    )
            buckets = self._conn.execute(
                """
                SELECT guild_id, week_start, item_key, item_label, flag, amount
                FROM backfill_week
                WHERE kind = 'command'
                """
            ).fetchall()
            grouped: dict[tuple[int, str], list] = {}
            for row in buckets:
                grouped.setdefault((int(row["guild_id"]), str(row["week_start"])), []).append(row)
            for (guild_id, week_start), week_rows in grouped.items():
                merged_buckets: dict[str, list] = {}
                for row in week_rows:
                    name = str(row["item_key"])
                    canonical = mapping.get(name.casefold()) if mapping else None
                    if not canonical:
                        continue
                    canonical = _clip(canonical, 80) or canonical
                    amount = int(row["amount"])
                    slot = merged_buckets.get(canonical)
                    if slot is None:
                        merged_buckets[canonical] = [str(row["item_label"] or "prefix"), int(row["flag"] or 0), amount]
                    else:
                        slot[2] += amount
                self._conn.execute(
                    """
                    DELETE FROM backfill_week
                    WHERE guild_id = ? AND week_start = ? AND kind = 'command'
                    """,
                    (guild_id, week_start),
                )
                for name, (label, flag, amount) in merged_buckets.items():
                    self._conn.execute(
                        """
                        INSERT INTO backfill_week
                            (guild_id, week_start, kind, item_key, item_label, flag, amount)
                        VALUES (?, ?, 'command', ?, ?, ?, ?)
                        """,
                        (guild_id, week_start, name, label, flag, amount),
                    )
            snapshots = self._conn.execute(
                "SELECT guild_id, week_start, summary_json FROM weekly_snapshots"
            ).fetchall()
            for snap in snapshots:
                self._rewrite_snapshot_commands(int(snap["guild_id"]), str(snap["week_start"]), snap["summary_json"])
        dropped.sort(key=lambda item: (item["name"], item["weekly"], item["all_time"]))
        return dropped

    def _rewrite_snapshot_commands(self, guild_id: int, week_start: str, summary_json: str) -> None:
        try:
            payload = json.loads(summary_json)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        rows = self._conn.execute(
            """
            SELECT item_key, item_label, amount
            FROM backfill_week
            WHERE guild_id = ? AND week_start = ? AND kind = 'command'
            """,
            (guild_id, week_start),
        ).fetchall()
        ranked = sorted(
            (
                (str(row["item_key"]), str(row["item_label"] or "prefix"), int(row["amount"]))
                for row in rows
            ),
            key=lambda item: (-item[2], item[0]),
        )
        payload["commands"] = sum(item[2] for item in ranked)
        payload["top_commands"] = [
            {"name": name, "kind": kind, "weekly": amount}
            for name, kind, amount in ranked[:5]
        ]
        self._conn.execute(
            """
            UPDATE weekly_snapshots
            SET summary_json = ?
            WHERE guild_id = ? AND week_start = ?
            """,
            (json.dumps(payload, separators=(",", ":"), sort_keys=True), guild_id, week_start),
        )

    def close_week(
        self,
        guild_id: int,
        week_start: date | datetime | str,
        closed_at: datetime,
        *,
        known_custom_emojis=None,
        known_channel_ids=None,
        command_map: dict[str, str] | None = None,
    ) -> WeeklyStats | None:
        """Snapshot this guild's week and zero its weekly counters.

        Idempotent: if this ``week_start`` was already closed, pending deltas
        are flushed into the new week and this returns None without resetting.
        """
        guild_id = int(guild_id)
        week = _as_date(week_start)
        closed = _as_aware(closed_at)
        if self.last_posted_week_start(guild_id) == week:
            self.flush()
            return None
        with self._transaction():
            self._write_buffer(self._buffer)
            stats = self._build(
                guild_id,
                week,
                closed,
                known_custom_emojis,
                known_channel_ids,
                command_map,
            )
            self._save_snapshot(guild_id, week, closed, stats)
            self._save_card_state(guild_id, week, stats)
            self._zero_weekly(guild_id)
            self._set_last_posted(guild_id, week)
        self._buffer.clear()
        return stats

    def _save_card_state(self, guild_id: int, week: date, stats: WeeklyStats) -> None:
        payload = json.dumps(_card_payload(stats), separators=(",", ":"), sort_keys=True)
        self._conn.execute(
            """
            INSERT INTO weekly_card_state (guild_id, week_start, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, week_start) DO UPDATE SET
                payload = excluded.payload
            """,
            (guild_id, week.isoformat(), payload),
        )

    def write_weekly_card_state(self, guild_id: int, week_start: date | datetime | str, payload: dict) -> None:
        """Replace one closed-week card payload. Does not touch counters."""
        guild_id = int(guild_id)
        week = _as_date(week_start)
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO weekly_card_state (guild_id, week_start, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, week_start) DO UPDATE SET
                    payload = excluded.payload
                """,
                (guild_id, week.isoformat(), body),
            )

    def _load_card_state(self, guild_id: int, week: date) -> dict | None:
        row = self._conn.execute(
            """
            SELECT payload FROM weekly_card_state
            WHERE guild_id = ? AND week_start = ?
            """,
            (guild_id, week.isoformat()),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _save_snapshot(self, guild_id: int, week: date, closed_at: datetime, stats: WeeklyStats) -> None:
        payload = json.dumps(_summary_payload(stats), separators=(",", ":"), sort_keys=True)
        self._conn.execute(
            """
            INSERT INTO weekly_snapshots (guild_id, week_start, closed_at, summary_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, week_start) DO UPDATE SET
                closed_at = excluded.closed_at,
                summary_json = excluded.summary_json
            """,
            (guild_id, week.isoformat(), closed_at.isoformat(), payload),
        )

    def _set_last_posted(self, guild_id: int, week: date) -> None:
        self._conn.execute(
            """
            INSERT INTO guild_state (guild_id, last_posted_week_start)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                last_posted_week_start = excluded.last_posted_week_start
            """,
            (guild_id, week.isoformat()),
        )

    def _zero_weekly(self, guild_id: int) -> None:
        for table in _ZERO_TABLES:
            self._conn.execute(f"UPDATE {table} SET weekly = 0 WHERE guild_id = ?", (guild_id,))
        self._conn.execute("DELETE FROM active_members WHERE guild_id = ?", (guild_id,))

    def _build(
        self,
        guild_id: int,
        week: date,
        now: datetime,
        known_custom_emojis,
        known_channel_ids=None,
        command_map: dict[str, str] | None = None,
    ) -> WeeklyStats:
        channels = self._conn.execute(
            "SELECT channel_id, weekly, all_time FROM channel_messages WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        reactions = self._conn.execute(
            """
            SELECT emoji_key, emoji_name, is_custom, weekly, all_time
            FROM reaction_counts WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchall()
        commands = self._conn.execute(
            """
            SELECT command_name, kind, weekly, all_time
            FROM command_counts WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchall()
        commands = [row for row in commands if not is_owner_only_name(str(row["command_name"]))]
        if command_map is not None:
            commands = _merge_command_rows(
                _inject_registered_prefix_rows(commands, command_map),
                command_map,
            )
            commands = [row for row in commands if not is_owner_only_name(str(row["command_name"]))]
        voice_rows = self._conn.execute(
            "SELECT channel_id, weekly, all_time FROM voice_channels WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        heat_rows = self._conn.execute(
            "SELECT weekday, hour, weekly, all_time FROM activity_heatmap WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        voice_heat_rows = self._conn.execute(
            "SELECT weekday, hour, weekly, all_time FROM voice_heatmap WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        image_rows = self._conn.execute(
            """
            SELECT dimension, item_key, weekly, all_time
            FROM imagegen_counts WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchall()
        total_rows = self._conn.execute(
            "SELECT metric, weekly, all_time FROM imagegen_totals WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        active = self._conn.execute(
            "SELECT COUNT(*) AS n FROM active_members WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()["n"]

        top_channels, bottom_channels, dead_channels = _split_channels(channels, known_channel_ids)
        top_channels_all, bottom_channels_all, _dead_all = _split_channels(
            channels, known_channel_ids, metric="all_time"
        )
        top_reactions = _top_reactions(reactions)
        top_reactions_all = _top_reactions(reactions, metric="all_time")
        least_custom = _least_custom(reactions, known_custom_emojis)
        least_weekly = _least_custom(reactions, known_custom_emojis, metric="weekly")
        top_commands, bottom_commands = _split_commands(commands)
        top_commands_all, bottom_commands_all = _split_commands(commands, metric="all_time")
        imagegen = _imagegen_summary(image_rows, total_rows)
        previous = self._previous_snapshot(guild_id, week)
        voice_sorted = sorted(
            (row for row in voice_rows if int(row["weekly"]) > 0),
            key=lambda row: (-int(row["weekly"]), -int(row["all_time"]), int(row["channel_id"])),
        )
        voice_sorted_all = sorted(
            (row for row in voice_rows if int(row["all_time"]) > 0),
            key=lambda row: (-int(row["all_time"]), -int(row["weekly"]), int(row["channel_id"])),
        )
        messages_weekly = sum(int(row["weekly"]) for row in channels)
        reactions_weekly = sum(int(row["weekly"]) for row in reactions)
        commands_weekly = sum(int(row["weekly"]) for row in commands)
        active_count = int(active)
        built = WeeklyStats(
            guild_id=guild_id,
            week_start=week,
            generated_at=now,
            top_channels=tuple(_channel(row) for row in top_channels),
            bottom_channels=tuple(_channel(row) for row in bottom_channels),
            dead_channels=tuple(_channel(row) for row in dead_channels),
            top_reactions=tuple(top_reactions),
            least_custom_emojis=tuple(least_custom),
            top_commands=tuple(top_commands),
            bottom_commands=tuple(bottom_commands),
            imagegen=imagegen,
            active_members=active_count,
            active_members_last_week=_snapshot_int(previous, "active_members"),
            heatmap=_heatmap(heat_rows),
            voice_minutes=sum(int(row["weekly"]) for row in voice_rows),
            voice_minutes_all_time=sum(int(row["all_time"]) for row in voice_rows),
            top_voice_channels=tuple(_channel(row) for row in voice_sorted[:TOP_N]),
            voice_heatmap=_heatmap(voice_heat_rows),
            messages_weekly=messages_weekly,
            messages_all_time=sum(int(row["all_time"]) for row in channels),
            reactions_weekly=reactions_weekly,
            reactions_all_time=sum(int(row["all_time"]) for row in reactions),
            commands_weekly=commands_weekly,
            commands_all_time=sum(int(row["all_time"]) for row in commands),
            messages_last_week=_snapshot_int(previous, "messages"),
            reactions_last_week=_snapshot_int(previous, "reactions"),
            voice_minutes_last_week=_snapshot_int(previous, "voice_minutes"),
            imagegen_last_week=_snapshot_imagegens(previous),
            heatmap_all_time=_heatmap(heat_rows, "all_time"),
            voice_heatmap_all_time=_heatmap(voice_heat_rows, "all_time"),
            week_history=self._week_history(
                guild_id,
                week,
                messages_weekly,
                reactions_weekly,
                active_count,
                commands_weekly,
            ),
            earliest_week=self._earliest_week(guild_id, week),
            live_since=self._live_since(guild_id),
            imagegen_since=self._imagegen_since(guild_id),
            top_channels_all_time=tuple(_channel(row) for row in top_channels_all),
            bottom_channels_all_time=tuple(_channel(row) for row in bottom_channels_all),
            top_reactions_all_time=tuple(top_reactions_all),
            least_custom_emojis_weekly=tuple(least_weekly),
            top_commands_all_time=tuple(top_commands_all),
            bottom_commands_all_time=tuple(bottom_commands_all),
            top_voice_channels_all_time=tuple(_channel(row) for row in voice_sorted_all[:TOP_N]),
        )
        if self.last_posted_week_start(guild_id) == week:
            card = self._load_card_state(guild_id, week)
            if card is not None:
                built = _apply_closed_card(built, card)
        return built

    def _earliest_week(self, guild_id: int, week: date) -> date:
        row = self._conn.execute(
            "SELECT MIN(week_start) AS week_start FROM weekly_snapshots WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        if row is None or not row["week_start"]:
            return week
        return date.fromisoformat(str(row["week_start"]))

    def _live_since(self, guild_id: int) -> date:
        moment = self.recorded_live_at(guild_id)
        if moment is None:
            return LIVE_TRACKING_SINCE
        return moment.astimezone(LA).date()

    def _imagegen_since(self, guild_id: int) -> date | None:
        """Earliest image-gen day, or None when this guild has no image history."""
        earliest: date | None = None
        row = self._conn.execute(
            """
            SELECT MIN(message_id) AS message_id
            FROM history_applied
            WHERE guild_id = ? AND source = 'imagegen'
            """,
            (guild_id,),
        ).fetchone()
        if row is not None and row["message_id"]:
            earliest = _snowflake_date(int(row["message_id"]))
        snaps = self._conn.execute(
            """
            SELECT week_start, summary_json FROM weekly_snapshots WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchall()
        for snap in snaps:
            try:
                payload = json.loads(snap["summary_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or int(payload.get("imagegen_success") or 0) <= 0:
                continue
            start = date.fromisoformat(str(snap["week_start"]))
            if earliest is None or start < earliest:
                earliest = start
        return earliest

    def _week_history(
        self,
        guild_id: int,
        week: date,
        messages: int,
        reactions: int,
        active_members: int,
        commands: int,
    ) -> tuple:
        """Closed snapshots, oldest first, then the in-progress week."""
        rows = self._conn.execute(
            """
            SELECT week_start, summary_json FROM weekly_snapshots
            WHERE guild_id = ?
            ORDER BY week_start
            """,
            (guild_id,),
        ).fetchall()
        points: list[WeekPoint] = []
        seen: set[date] = set()
        for row in rows:
            start = date.fromisoformat(str(row["week_start"]))
            try:
                payload = json.loads(row["summary_json"])
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            points.append(
                WeekPoint(
                    start,
                    int(payload.get("messages") or 0),
                    int(payload.get("reactions") or 0),
                    int(payload.get("active_members") or 0),
                    int(payload.get("commands") or 0),
                )
            )
            seen.add(start)
        current = WeekPoint(week, int(messages), int(reactions), int(active_members), int(commands))
        if week in seen:
            points = [current if point.week_start == week else point for point in points]
        else:
            points.append(current)
        return tuple(points)

    def apply_closed_week_imagegen_backfill(
        self,
        guild_id: int,
        week_start: date | datetime | str,
        models: dict[str, int],
        *,
        success: int,
        error: int = 0,
        key: str,
    ) -> bool:
        """Add closed-week image runs to all-time totals and that week's snapshot.

        Weekly columns stay as they are. ``last_posted`` and message counts
        are not touched. The same ``key`` for a guild applies once.
        """
        guild_id = int(guild_id)
        token = str(key or "").strip()
        if not token:
            return False
        week = _as_date(week_start)
        success = int(success)
        error = int(error)
        model_counts: list[tuple[str, int]] = []
        for name, amount in (models or {}).items():
            model_name = _clip(name, 80) if name else ""
            count = int(amount)
            if model_name and count > 0:
                model_counts.append((model_name, count))
        self.flush()
        with self._transaction():
            already = self._conn.execute(
                'SELECT 1 FROM applied_corrections WHERE guild_id = ? AND key = ?',
                (guild_id, token),
            ).fetchone()
            if already is not None:
                return False
            self._conn.execute(
                'INSERT INTO applied_corrections (guild_id, key) VALUES (?, ?)',
                (guild_id, token),
            )
            for model_name, count in model_counts:
                self._add_all_time_imagegen("model", guild_id, model_name, count)
            if success > 0:
                self._add_all_time_imagegen("outcome", guild_id, "success", success)
            if error > 0:
                self._add_all_time_imagegen("outcome", guild_id, "error", error)
            generations = success + error
            if generations > 0:
                self._conn.execute(
                    """
                    INSERT INTO imagegen_totals (guild_id, metric, weekly, all_time)
                    VALUES (?, 'generations', 0, ?)
                    ON CONFLICT(guild_id, metric) DO UPDATE SET
                        all_time = all_time + excluded.all_time
                    """,
                    (guild_id, generations),
                )
            row = self._conn.execute(
                """
                SELECT summary_json FROM weekly_snapshots
                WHERE guild_id = ? AND week_start = ?
                """,
                (guild_id, week.isoformat()),
            ).fetchone()
            if row is not None:
                try:
                    payload = json.loads(row["summary_json"])
                except (TypeError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    payload["imagegen_success"] = int(payload.get("imagegen_success") or 0) + success
                    payload["imagegen_error"] = int(payload.get("imagegen_error") or 0) + error
                    self._conn.execute(
                        """
                        UPDATE weekly_snapshots
                        SET summary_json = ?
                        WHERE guild_id = ? AND week_start = ?
                        """,
                        (
                            json.dumps(payload, separators=(",", ":"), sort_keys=True),
                            guild_id,
                            week.isoformat(),
                        ),
                    )
            if success > 0:
                self._bump_card_imagegen(guild_id, week, "", success)
            for model_name, count in model_counts:
                self._bump_card_model(guild_id, week, model_name, count)
        return True

    def apply_history_imagegen(
        self,
        guild_id: int,
        message_id: int,
        week_start: date | datetime | str,
        model: str,
    ) -> bool:
        """Add one historical success image to all-time totals.

        The weekly columns stay unchanged. The same message id is a no-op.
        A closed week's thin snapshot and card state, when present, gain the
        same success. The model name is a short key, never a caption.
        """
        guild_id = int(guild_id)
        message_id = int(message_id)
        week = _as_date(week_start)
        model_name = _clip(model, 80) or "unknown"
        self.flush()
        with self._transaction():
            if not self._claim_history(guild_id, "imagegen", message_id):
                return False
            self._add_all_time_imagegen("model", guild_id, model_name, 1)
            self._add_all_time_imagegen("outcome", guild_id, "success", 1)
            self._conn.execute(
                """
                INSERT INTO imagegen_totals (guild_id, metric, weekly, all_time)
                VALUES (?, 'generations', 0, 1)
                ON CONFLICT(guild_id, metric) DO UPDATE SET
                    all_time = all_time + 1
                """,
                (guild_id,),
            )
            self._bump_snapshot_imagegen(guild_id, week, 1, 0)
            self._bump_card_imagegen(guild_id, week, "", 1)
            self._bump_card_model(guild_id, week, model_name, 1)
        return True

    def replace_all_time_imagegen_models(self, guild_id: int, counts: dict[str, int]) -> bool:
        """Set model all-time counts to ``counts``. Weekly columns and other totals stay.

        Writing the same mapping again changes nothing. Keys are short model ids.
        """
        guild_id = int(guild_id)
        wanted: dict[str, int] = {}
        for raw_key, raw_count in counts.items():
            key = _clip(str(raw_key), 80) or "unknown"
            amount = int(raw_count)
            if amount < 0:
                amount = 0
            wanted[key] = wanted.get(key, 0) + amount
        wanted = {key: amount for key, amount in wanted.items() if amount > 0}
        self.flush()
        rows = self._conn.execute(
            """
            SELECT item_key, weekly, all_time
            FROM imagegen_counts
            WHERE guild_id = ? AND dimension = 'model'
            """,
            (guild_id,),
        ).fetchall()
        current = {
            str(row["item_key"]): (int(row["weekly"]), int(row["all_time"]))
            for row in rows
        }
        if {key: all_time for key, (_weekly, all_time) in current.items() if all_time} == wanted:
            return False
        with self._transaction():
            for key, (weekly, _all_time) in current.items():
                if key in wanted:
                    continue
                if weekly == 0:
                    self._conn.execute(
                        """
                        DELETE FROM imagegen_counts
                        WHERE guild_id = ? AND dimension = 'model' AND item_key = ?
                        """,
                        (guild_id, key),
                    )
                else:
                    self._conn.execute(
                        """
                        UPDATE imagegen_counts SET all_time = 0
                        WHERE guild_id = ? AND dimension = 'model' AND item_key = ?
                        """,
                        (guild_id, key),
                    )
            for key, amount in wanted.items():
                self._conn.execute(
                    """
                    INSERT INTO imagegen_counts (guild_id, dimension, item_key, weekly, all_time)
                    VALUES (?, 'model', ?, 0, ?)
                    ON CONFLICT(guild_id, dimension, item_key) DO UPDATE SET
                        all_time = excluded.all_time
                    """,
                    (guild_id, key, amount),
                )
        return True

    def apply_history_slash(
        self,
        guild_id: int,
        message_id: int,
        week_start: date | datetime | str,
        name: str,
    ) -> bool:
        """Add one historical slash command to all-time totals.

        Weekly columns stay unchanged. Owner-only names are marked seen and
        do not change counts. A bare ``imagegen`` name is stored as
        ``imagegen generate``. The same message id is a no-op.
        """
        guild_id = int(guild_id)
        message_id = int(message_id)
        week = _as_date(week_start)
        label = _clip(name, 80)
        if label == "imagegen":
            label = "imagegen generate"
        self.flush()
        with self._transaction():
            if not self._claim_history(guild_id, "slash", message_id):
                return False
            if not label or is_owner_only_name(label):
                return True
            self._conn.execute(
                """
                INSERT INTO command_counts (guild_id, command_name, kind, weekly, all_time)
                VALUES (?, ?, 'slash', 0, 1)
                ON CONFLICT(guild_id, command_name, kind) DO UPDATE SET
                    all_time = all_time + 1
                """,
                (guild_id, label),
            )
            self._bump_snapshot_command(guild_id, week, label, "slash", 1)
            self._bump_card_command(guild_id, week, label, "slash", 1)
        return True

    def purge_owner_only_commands(self, names: set[str] | frozenset[str]) -> list[dict]:
        """Delete owner-only command rows from this database.

        Touches ``command_counts``, command week buckets, thin snapshots, and
        card-state command lists. Meant for a copy or a temporary database.
        """
        blocked = {str(name).casefold() for name in names if str(name).strip()}
        if not blocked:
            return []
        self.flush()
        dropped: list[dict] = []
        with self._transaction():
            rows = self._conn.execute(
                "SELECT guild_id, command_name, kind, weekly, all_time FROM command_counts"
            ).fetchall()
            for row in rows:
                if str(row["command_name"]).casefold() not in blocked:
                    continue
                dropped.append(
                    {
                        "name": str(row["command_name"]),
                        "kind": str(row["kind"]),
                        "weekly": int(row["weekly"]),
                        "all_time": int(row["all_time"]),
                    }
                )
                self._conn.execute(
                    """
                    DELETE FROM command_counts
                    WHERE guild_id = ? AND command_name = ? AND kind = ?
                    """,
                    (int(row["guild_id"]), str(row["command_name"]), str(row["kind"])),
                )
            buckets = self._conn.execute(
                """
                SELECT guild_id, week_start, item_key
                FROM backfill_week
                WHERE kind = 'command'
                """
            ).fetchall()
            for row in buckets:
                if str(row["item_key"]).casefold() not in blocked:
                    continue
                self._conn.execute(
                    """
                    DELETE FROM backfill_week
                    WHERE guild_id = ? AND week_start = ? AND kind = 'command' AND item_key = ?
                    """,
                    (int(row["guild_id"]), str(row["week_start"]), str(row["item_key"])),
                )
            snapshots = self._conn.execute(
                "SELECT guild_id, week_start, summary_json FROM weekly_snapshots"
            ).fetchall()
            for snap in snapshots:
                self._rewrite_snapshot_commands(int(snap["guild_id"]), str(snap["week_start"]), snap["summary_json"])
            cards = self._conn.execute(
                "SELECT guild_id, week_start, payload FROM weekly_card_state"
            ).fetchall()
            for card in cards:
                self._strip_card_commands(int(card["guild_id"]), str(card["week_start"]), card["payload"], blocked)
        dropped.sort(key=lambda item: (item["name"], item["kind"]))
        return dropped

    def _claim_history(self, guild_id: int, source: str, message_id: int) -> bool:
        existing = self._conn.execute(
            """
            SELECT 1 FROM history_applied
            WHERE guild_id = ? AND source = ? AND message_id = ?
            """,
            (guild_id, source, message_id),
        ).fetchone()
        if existing is not None:
            return False
        self._conn.execute(
            """
            INSERT INTO history_applied (guild_id, source, message_id)
            VALUES (?, ?, ?)
            """,
            (guild_id, source, message_id),
        )
        return True

    def _bump_snapshot_imagegen(self, guild_id: int, week: date, success: int, error: int) -> None:
        row = self._conn.execute(
            """
            SELECT summary_json FROM weekly_snapshots
            WHERE guild_id = ? AND week_start = ?
            """,
            (guild_id, week.isoformat()),
        ).fetchone()
        if row is None:
            return
        try:
            payload = json.loads(row["summary_json"])
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        payload["imagegen_success"] = int(payload.get("imagegen_success") or 0) + int(success)
        payload["imagegen_error"] = int(payload.get("imagegen_error") or 0) + int(error)
        self._conn.execute(
            """
            UPDATE weekly_snapshots
            SET summary_json = ?
            WHERE guild_id = ? AND week_start = ?
            """,
            (json.dumps(payload, separators=(",", ":"), sort_keys=True), guild_id, week.isoformat()),
        )

    def _bump_snapshot_command(self, guild_id: int, week: date, name: str, kind: str, amount: int) -> None:
        row = self._conn.execute(
            """
            SELECT summary_json FROM weekly_snapshots
            WHERE guild_id = ? AND week_start = ?
            """,
            (guild_id, week.isoformat()),
        ).fetchone()
        if row is None:
            return
        try:
            payload = json.loads(row["summary_json"])
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        payload["commands"] = int(payload.get("commands") or 0) + int(amount)
        rows = list(payload.get("top_commands") or [])
        found = False
        for item in rows:
            if not isinstance(item, dict):
                continue
            if item.get("name") == name and item.get("kind") == kind:
                item["weekly"] = int(item.get("weekly") or 0) + int(amount)
                found = True
                break
        if not found:
            rows.append({"name": name, "kind": kind, "weekly": int(amount)})
        rows.sort(key=lambda item: (-int(item.get("weekly") or 0), str(item.get("name") or "")))
        payload["top_commands"] = rows[:5]
        self._conn.execute(
            """
            UPDATE weekly_snapshots
            SET summary_json = ?
            WHERE guild_id = ? AND week_start = ?
            """,
            (json.dumps(payload, separators=(",", ":"), sort_keys=True), guild_id, week.isoformat()),
        )

    def _bump_card_imagegen(self, guild_id: int, week: date, model: str, success: int) -> None:
        payload = self._load_card_state(guild_id, week)
        if payload is None or success <= 0:
            return
        image = payload.get("imagegen")
        if not isinstance(image, dict):
            image = {}
            payload["imagegen"] = image
        image["success_count"] = int(image.get("success_count") or 0) + int(success)
        image["generation_count"] = int(image.get("generation_count") or 0) + int(success)
        attempts = int(image["success_count"]) + int(image.get("error_count") or 0)
        image["success_rate"] = (int(image["success_count"]) / attempts) if attempts else None
        image["kept_first"] = max(
            0,
            int(image["success_count"]) - int(image.get("regenerate_count") or 0) - int(image.get("edit_count") or 0),
        )
        self._conn.execute(
            """
            UPDATE weekly_card_state
            SET payload = ?
            WHERE guild_id = ? AND week_start = ?
            """,
            (json.dumps(payload, separators=(",", ":"), sort_keys=True), guild_id, week.isoformat()),
        )

    def _bump_card_model(self, guild_id: int, week: date, model: str, amount: int) -> None:
        if not model or amount <= 0:
            return
        payload = self._load_card_state(guild_id, week)
        if payload is None:
            return
        image = payload.get("imagegen")
        if not isinstance(image, dict):
            image = {}
            payload["imagegen"] = image
        models = list(image.get("models") or [])
        found = False
        for item in models:
            if isinstance(item, dict) and item.get("key") == model:
                item["weekly"] = int(item.get("weekly") or 0) + int(amount)
                found = True
                break
        if not found:
            models.append({"key": model, "weekly": int(amount), "all_time": 0})
        image["models"] = models
        self._conn.execute(
            """
            UPDATE weekly_card_state
            SET payload = ?
            WHERE guild_id = ? AND week_start = ?
            """,
            (json.dumps(payload, separators=(",", ":"), sort_keys=True), guild_id, week.isoformat()),
        )

    def _bump_card_command(self, guild_id: int, week: date, name: str, kind: str, amount: int) -> None:
        payload = self._load_card_state(guild_id, week)
        if payload is None:
            return
        payload["commands"] = int(payload.get("commands") or 0) + int(amount)
        rows = list(payload.get("command_rows") or [])
        if not rows:
            rows = list(payload.get("top_commands") or []) + list(payload.get("bottom_commands") or [])
        found = False
        for item in rows:
            if isinstance(item, dict) and item.get("name") == name and item.get("kind") == kind:
                item["weekly"] = int(item.get("weekly") or 0) + int(amount)
                found = True
                break
        if not found:
            rows.append({"name": name, "kind": kind, "weekly": int(amount), "all_time": 0})
        split_top, split_bottom = _split_commands(_command_dicts(rows), metric="weekly")
        payload["command_rows"] = rows
        payload["top_commands"] = [
            {"name": row.name, "kind": row.kind, "weekly": row.weekly, "all_time": row.all_time}
            for row in split_top
        ]
        payload["bottom_commands"] = [
            {"name": row.name, "kind": row.kind, "weekly": row.weekly, "all_time": row.all_time}
            for row in split_bottom
        ]
        self._conn.execute(
            """
            UPDATE weekly_card_state
            SET payload = ?
            WHERE guild_id = ? AND week_start = ?
            """,
            (json.dumps(payload, separators=(",", ":"), sort_keys=True), guild_id, week.isoformat()),
        )

    def _strip_card_commands(self, guild_id: int, week_start: str, raw: str, blocked: set[str]) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        rows = [
            item
            for item in list(payload.get("command_rows") or [])
            if isinstance(item, dict) and str(item.get("name") or "").casefold() not in blocked
        ]
        if not payload.get("command_rows"):
            rows = [
                item
                for item in list(payload.get("top_commands") or []) + list(payload.get("bottom_commands") or [])
                if isinstance(item, dict) and str(item.get("name") or "").casefold() not in blocked
            ]
        payload["commands"] = sum(int(item.get("weekly") or 0) for item in rows)
        split_top, split_bottom = _split_commands(_command_dicts(rows), metric="weekly") if rows else ([], [])
        payload["command_rows"] = rows
        payload["top_commands"] = [
            {"name": row.name, "kind": row.kind, "weekly": row.weekly, "all_time": row.all_time} for row in split_top
        ]
        payload["bottom_commands"] = [
            {"name": row.name, "kind": row.kind, "weekly": row.weekly, "all_time": row.all_time} for row in split_bottom
        ]
        self._conn.execute(
            """
            UPDATE weekly_card_state SET payload = ? WHERE guild_id = ? AND week_start = ?
            """,
            (json.dumps(payload, separators=(",", ":"), sort_keys=True), guild_id, week_start),
        )

    def _add_all_time_imagegen(self, dimension: str, guild_id: int, item_key: str, amount: int) -> None:
        self._conn.execute(
            """
            INSERT INTO imagegen_counts (guild_id, dimension, item_key, weekly, all_time)
            VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(guild_id, dimension, item_key) DO UPDATE SET
                all_time = all_time + excluded.all_time
            """,
            (guild_id, dimension, item_key, amount),
        )

    def _previous_snapshot(self, guild_id: int, week: date) -> dict | None:
        row = self._conn.execute(
            """
            SELECT summary_json FROM weekly_snapshots
            WHERE guild_id = ? AND week_start < ?
            ORDER BY week_start DESC
            LIMIT 1
            """,
            (guild_id, week.isoformat()),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["summary_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload


def _card_payload(stats: WeeklyStats) -> dict:
    """Closed-week fields the weekly card renders. No user ids and no text."""

    def channels(rows) -> list[dict]:
        return [
            {"id": int(row.channel_id), "weekly": int(row.weekly), "all_time": int(row.all_time)}
            for row in rows
        ]

    def reactions(rows) -> list[dict]:
        return [
            {
                "key": row.emoji_key,
                "name": row.name,
                "is_custom": bool(row.is_custom),
                "weekly": int(row.weekly),
                "all_time": int(row.all_time),
            }
            for row in rows
        ]

    def commands(rows) -> list[dict]:
        return [
            {"name": row.name, "kind": row.kind, "weekly": int(row.weekly), "all_time": int(row.all_time)}
            for row in rows
        ]

    image = stats.imagegen

    def dims(rows) -> list[dict]:
        return [{"key": row.key, "weekly": int(row.weekly), "all_time": int(row.all_time)} for row in rows]

    command_rows = commands(stats.top_commands) + commands(stats.bottom_commands)
    return {
        "messages": int(stats.messages_weekly),
        "reactions": int(stats.reactions_weekly),
        "commands": int(stats.commands_weekly),
        "active_members": int(stats.active_members),
        "voice_minutes": int(stats.voice_minutes),
        "top_channels": channels(stats.top_channels),
        "bottom_channels": channels(stats.bottom_channels),
        "dead_channels": channels(stats.dead_channels),
        "top_reactions": reactions(stats.top_reactions),
        "least_custom_emojis": reactions(stats.least_custom_emojis),
        "least_custom_emojis_weekly": reactions(stats.least_custom_emojis_weekly),
        "top_commands": commands(stats.top_commands),
        "bottom_commands": commands(stats.bottom_commands),
        "command_rows": command_rows,
        "heatmap": [list(row) for row in stats.heatmap],
        "voice_heatmap": [list(row) for row in stats.voice_heatmap],
        "top_voice_channels": channels(stats.top_voice_channels),
        "imagegen": {
            "models": dims(image.models),
            "loras": dims(image.loras),
            "edit_modes": dims(image.edit_modes),
            "resolutions": dims(image.resolutions),
            "steps": dims(image.steps),
            "samplers": dims(image.samplers),
            "avg_seconds": image.avg_seconds,
            "success_rate": image.success_rate,
            "error_rate": image.error_rate,
            "success_count": int(image.success_count),
            "error_count": int(image.error_count),
            "regenerate_count": int(image.regenerate_count),
            "edit_count": int(image.edit_count),
            "kept_first": int(image.kept_first),
            "generation_count": int(image.generation_count),
        },
    }


def _channels_from(raw) -> tuple[ChannelCount, ...]:
    rows = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        rows.append(ChannelCount(int(item.get("id") or 0), int(item.get("weekly") or 0), int(item.get("all_time") or 0)))
    return tuple(rows)


def _reactions_from(raw) -> tuple[ReactionCount, ...]:
    rows = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            ReactionCount(
                str(item.get("key") or ""),
                str(item.get("name") or ""),
                bool(item.get("is_custom")),
                int(item.get("weekly") or 0),
                int(item.get("all_time") or 0),
            )
        )
    return tuple(rows)


def _commands_from(raw) -> tuple[CommandCount, ...]:
    rows = []
    for item in raw or []:
        if not isinstance(item, dict) or is_owner_only_name(str(item.get("name") or "")):
            continue
        rows.append(
            CommandCount(
                str(item.get("name") or ""),
                str(item.get("kind") or ""),
                int(item.get("weekly") or 0),
                int(item.get("all_time") or 0),
            )
        )
    return tuple(rows)


def _grid_from(raw) -> tuple[tuple[int, ...], ...]:
    grid = [[0 for _ in range(24)] for _ in range(7)]
    for weekday, row in enumerate(list(raw or [])[:7]):
        for hour, value in enumerate(list(row or [])[:24]):
            grid[weekday][hour] = int(value or 0)
    return tuple(tuple(line) for line in grid)


def _dims_from(raw) -> tuple[DimCount, ...]:
    rows = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        rows.append(DimCount(str(item.get("key") or ""), int(item.get("weekly") or 0), int(item.get("all_time") or 0)))
    return tuple(rows)


def _merge_model_dims(payload_raw, live: tuple[DimCount, ...]) -> tuple[DimCount, ...]:
    """Weekly counts from the closed card. All-time counts stay on the live rows."""
    payload = _dims_from(payload_raw)
    live_by = {row.key: row for row in live}
    payload_by = {row.key: row for row in payload}
    keys: list[str] = []
    seen: set[str] = set()
    for row in (*payload, *live):
        if row.key in seen:
            continue
        seen.add(row.key)
        keys.append(row.key)
    merged = []
    for key in keys:
        weekly = payload_by[key].weekly if key in payload_by else 0
        if key in live_by:
            all_time = live_by[key].all_time
        else:
            all_time = payload_by[key].all_time
        merged.append(DimCount(key, weekly, all_time))
    return tuple(merged)


def _imagegen_from_payload(raw, live: ImageGenSummary) -> ImageGenSummary:
    raw = raw if isinstance(raw, dict) else {}
    return ImageGenSummary(
        models=_merge_model_dims(raw.get("models"), live.models),
        loras=_dims_from(raw.get("loras")),
        edit_modes=_dims_from(raw.get("edit_modes")),
        resolutions=_dims_from(raw.get("resolutions")),
        steps=_dims_from(raw.get("steps")),
        samplers=_dims_from(raw.get("samplers")),
        avg_seconds=raw.get("avg_seconds"),
        success_rate=raw.get("success_rate"),
        error_rate=raw.get("error_rate"),
        success_count=int(raw.get("success_count") or 0),
        error_count=int(raw.get("error_count") or 0),
        regenerate_count=int(raw.get("regenerate_count") or 0),
        edit_count=int(raw.get("edit_count") or 0),
        kept_first=int(raw.get("kept_first") or 0),
        generation_count=int(raw.get("generation_count") or 0),
        generation_count_all_time=live.generation_count_all_time,
        success_count_all_time=live.success_count_all_time,
        error_count_all_time=live.error_count_all_time,
    )


def _apply_closed_card(stats: WeeklyStats, payload: dict) -> WeeklyStats:
    """Weekly lists and KPIs from the closed card. All-time fields stay live."""
    messages = int(payload.get("messages") or 0)
    reactions = int(payload.get("reactions") or 0)
    commands = int(payload.get("commands") or 0)
    active = int(payload.get("active_members") or 0)
    history = []
    for point in stats.week_history:
        if point.week_start == stats.week_start:
            history.append(WeekPoint(point.week_start, messages, reactions, active, commands))
        else:
            history.append(point)
    return replace(
        stats,
        top_channels=_channels_from(payload.get("top_channels")),
        bottom_channels=_channels_from(payload.get("bottom_channels")),
        dead_channels=_channels_from(payload.get("dead_channels")),
        top_reactions=_reactions_from(payload.get("top_reactions")),
        least_custom_emojis_weekly=_reactions_from(
            payload.get("least_custom_emojis_weekly") or payload.get("least_custom_emojis")
        ),
        top_commands=_commands_from(payload.get("top_commands")),
        bottom_commands=_commands_from(payload.get("bottom_commands")),
        imagegen=_imagegen_from_payload(payload.get("imagegen"), stats.imagegen),
        active_members=active,
        heatmap=_grid_from(payload.get("heatmap")),
        voice_minutes=int(payload.get("voice_minutes") or 0),
        top_voice_channels=_channels_from(payload.get("top_voice_channels")),
        voice_heatmap=_grid_from(payload.get("voice_heatmap")),
        messages_weekly=messages,
        reactions_weekly=reactions,
        commands_weekly=commands,
        week_history=tuple(history),
    )


def _command_dicts(rows) -> list[dict]:
    normalized = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "command_name": item.get("name") or item.get("command_name") or "",
                "kind": item.get("kind") or "",
                "weekly": int(item.get("weekly") or 0),
                "all_time": int(item.get("all_time") or 0),
            }
        )
    return normalized


def _snapshot_int(payload, key: str) -> int | None:
    if not isinstance(payload, dict) or key not in payload:
        return None
    value = payload[key]
    if value is None:
        return None
    return int(value)


def _snapshot_imagegens(payload) -> int | None:
    """Previous image runs. The snapshot stores successes and errors, not the generations counter."""
    if not isinstance(payload, dict):
        return None
    if "imagegen_success" not in payload and "imagegen_error" not in payload:
        return None
    return int(payload.get("imagegen_success") or 0) + int(payload.get("imagegen_error") or 0)


def _channel(row) -> ChannelCount:
    return ChannelCount(int(row["channel_id"]), int(row["weekly"]), int(row["all_time"]))


def _split_channels(rows, known_channel_ids=None, metric: str = "weekly") -> tuple[list, list, list]:
    indexed = {int(row["channel_id"]): row for row in rows}
    if known_channel_ids is not None:
        scoped = []
        seen: set[int] = set()
        for raw in known_channel_ids:
            try:
                channel_id = int(raw)
            except (TypeError, ValueError):
                continue
            if channel_id in seen:
                continue
            seen.add(channel_id)
            row = indexed.get(channel_id)
            if row is None:
                row = {"channel_id": channel_id, "weekly": 0, "all_time": 0}
            scoped.append(row)
        rows = scoped
    if metric == "all_time":
        active = [row for row in rows if int(row["all_time"]) > 0]
        active.sort(key=lambda row: (-int(row["all_time"]), -int(row["weekly"]), int(row["channel_id"])))
        top = active[:TOP_N]
        top_ids = {int(row["channel_id"]) for row in top}
        quiet = [row for row in active if int(row["channel_id"]) not in top_ids]
        quiet.sort(key=lambda row: (int(row["all_time"]), int(row["weekly"]), int(row["channel_id"])))
        return top, quiet[:TOP_N], []
    active = [row for row in rows if int(row["weekly"]) > 0]
    active.sort(key=lambda row: (-int(row["weekly"]), -int(row["all_time"]), int(row["channel_id"])))
    top = active[:TOP_N]
    top_ids = {int(row["channel_id"]) for row in top}
    bottom_pool = [row for row in active if int(row["channel_id"]) not in top_ids]
    bottom_pool.sort(key=lambda row: (int(row["weekly"]), int(row["all_time"]), int(row["channel_id"])))
    dead = [row for row in rows if int(row["weekly"]) == 0]
    dead.sort(key=lambda row: (-int(row["all_time"]), int(row["channel_id"])))
    return top, bottom_pool[:TOP_N], dead[:DEAD_LIMIT]


def _top_reactions(rows, metric: str = "weekly") -> list[ReactionCount]:
    primary = "all_time" if metric == "all_time" else "weekly"
    secondary = "weekly" if primary == "all_time" else "all_time"
    ranked = [row for row in rows if int(row[primary]) > 0]
    ranked.sort(key=lambda row: (-int(row[primary]), -int(row[secondary]), str(row["emoji_name"]).lower()))
    return [_reaction(row) for row in ranked[:TOP_N]]


def _reaction(row) -> ReactionCount:
    return ReactionCount(
        str(row["emoji_key"]),
        str(row["emoji_name"]),
        bool(row["is_custom"]),
        int(row["weekly"]),
        int(row["all_time"]),
    )


def _least_custom(rows, known, metric: str = "all_time") -> list[ReactionCount]:
    """Least-used custom emojis.

    ``known`` None keeps every stored custom emoji. A list, including an
    empty one, keeps only emojis that still exist and includes unused ones.
    """
    restrict = known is not None
    allowed = dict(_known_emoji_pairs(known)) if restrict else {}
    found: dict[str, dict] = {}
    for row in rows:
        if not row["is_custom"]:
            continue
        key = str(row["emoji_key"])
        if restrict and key not in allowed:
            continue
        found[key] = {
            "name": str(row["emoji_name"]),
            "weekly": int(row["weekly"]),
            "all_time": int(row["all_time"]),
        }
    if restrict:
        for key, name in allowed.items():
            slot = found.get(key)
            if slot is None:
                found[key] = {"name": name, "weekly": 0, "all_time": 0}
            elif name:
                slot["name"] = name
    primary = "weekly" if metric == "weekly" else "all_time"
    secondary = "all_time" if primary == "weekly" else "weekly"
    ranked = sorted(
        found.items(),
        key=lambda item: (item[1][primary], item[1][secondary], item[1]["name"].lower(), item[0]),
    )
    return [
        ReactionCount(key, slot["name"], True, slot["weekly"], slot["all_time"])
        for key, slot in ranked[:TOP_N]
    ]


def _inject_registered_prefix_rows(rows, command_map: dict[str, str]) -> list:
    """Add each canonical prefix name that is not already stored, at zero."""
    present = {
        (str(row["command_name"]), str(row["kind"]))
        for row in rows
    }
    extra = []
    seen: set[str] = set()
    for canonical in command_map.values():
        if not canonical or canonical in seen or is_owner_only_name(canonical):
            continue
        seen.add(canonical)
        if (canonical, "prefix") in present:
            continue
        extra.append({"command_name": canonical, "kind": "prefix", "weekly": 0, "all_time": 0})
    if not extra:
        return rows
    return list(rows) + extra


def _merge_command_rows(rows, command_map: dict[str, str]) -> list[dict]:
    """Keep slash rows. Drop unregistered prefix names and merge aliases."""
    merged: dict[tuple[str, str], list[int]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        kind = str(row["kind"])
        name = str(row["command_name"])
        weekly = int(row["weekly"])
        all_time = int(row["all_time"])
        if kind == "prefix":
            canonical = command_map.get(name.casefold())
            if not canonical:
                continue
            name = canonical
        if is_owner_only_name(name):
            continue
        key = (name, kind)
        slot = merged.get(key)
        if slot is None:
            merged[key] = [weekly, all_time]
            order.append(key)
        else:
            slot[0] += weekly
            slot[1] += all_time
    aliased = [
        {"command_name": name, "kind": kind, "weekly": merged[(name, kind)][0], "all_time": merged[(name, kind)][1]}
        for name, kind in order
    ]
    return _collapse_command_kinds(aliased)


def _collapse_command_kinds(rows) -> list[dict]:
    """Sum prefix and slash rows that share a canonical name."""
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in rows:
        name = str(row["command_name"])
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(row)
    collapsed = []
    for name in order:
        items = grouped[name]
        contributing = [
            item for item in items if int(item["weekly"]) > 0 or int(item["all_time"]) > 0
        ]
        kinds = {str(item["kind"]) for item in (contributing or items)}
        if "prefix" in kinds and "slash" in kinds:
            kind = "both"
        elif "slash" in kinds:
            kind = "slash"
        elif "prefix" in kinds:
            kind = "prefix"
        else:
            kind = next(iter(kinds)) if len(kinds) == 1 else "both"
        collapsed.append(
            {
                "command_name": name,
                "kind": kind,
                "weekly": sum(int(item["weekly"]) for item in items),
                "all_time": sum(int(item["all_time"]) for item in items),
            }
        )
    return collapsed


def _split_commands(rows, metric: str = "weekly") -> tuple[list[CommandCount], list[CommandCount]]:
    primary = "all_time" if metric == "all_time" else "weekly"
    secondary = "weekly" if primary == "all_time" else "all_time"
    ranked = list(rows)
    ranked.sort(
        key=lambda row: (
            -int(row[primary]),
            -int(row[secondary]),
            str(row["command_name"]),
            str(row["kind"]),
        )
    )
    pool = ranked if metric == "all_time" else [row for row in ranked if int(row["weekly"]) > 0]
    top = pool[:COMMAND_LIMIT]
    top_keys = {(str(row["command_name"]), str(row["kind"])) for row in top}
    rest = [row for row in ranked if (str(row["command_name"]), str(row["kind"])) not in top_keys]
    rest.sort(
        key=lambda row: (
            int(row[primary]),
            int(row[secondary]),
            str(row["command_name"]),
            str(row["kind"]),
        )
    )
    return [_command(row) for row in top], [_command(row) for row in rest[:COMMAND_LIMIT]]


def _command(row) -> CommandCount:
    return CommandCount(str(row["command_name"]), str(row["kind"]), int(row["weekly"]), int(row["all_time"]))


def _heatmap(rows, column: str = "weekly") -> tuple[tuple[int, ...], ...]:
    grid = [[0 for _ in range(24)] for _ in range(7)]
    for row in rows:
        weekday = int(row["weekday"])
        hour = int(row["hour"])
        if 0 <= weekday <= 6 and 0 <= hour <= 23:
            grid[weekday][hour] = int(row[column])
    return tuple(tuple(line) for line in grid)


def _imagegen_summary(rows, totals) -> ImageGenSummary:
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(str(row["dimension"]), []).append(row)

    def top(dimension: str, *, limit: int | None = IMAGE_TOP_N) -> tuple[DimCount, ...]:
        items = grouped.get(dimension, [])
        items = sorted(items, key=lambda row: (-int(row["weekly"]), -int(row["all_time"]), str(row["item_key"])))
        if limit is not None:
            items = items[:limit]
        return tuple(
            DimCount(str(row["item_key"]), int(row["weekly"]), int(row["all_time"]))
            for row in items
            if int(row["weekly"]) > 0 or int(row["all_time"]) > 0
        )

    outcomes = {str(row["item_key"]): row for row in grouped.get("outcome", [])}
    followups = {str(row["item_key"]): row for row in grouped.get("followup", [])}
    success = int(outcomes["success"]["weekly"]) if "success" in outcomes else 0
    error = int(outcomes["error"]["weekly"]) if "error" in outcomes else 0
    regenerate = int(followups["regenerate"]["weekly"]) if "regenerate" in followups else 0
    edit = int(followups["edit"]["weekly"]) if "edit" in followups else 0
    attempts = success + error
    by_metric = {str(row["metric"]): row for row in totals}
    seconds = float(by_metric["seconds"]["weekly"]) if "seconds" in by_metric else 0.0
    timed = int(by_metric["timed"]["weekly"]) if "timed" in by_metric else 0
    generations = int(by_metric["generations"]["weekly"]) if "generations" in by_metric else 0
    generations_all = int(by_metric["generations"]["all_time"]) if "generations" in by_metric else 0
    success_all = int(outcomes["success"]["all_time"]) if "success" in outcomes else 0
    error_all = int(outcomes["error"]["all_time"]) if "error" in outcomes else 0
    return ImageGenSummary(
        models=top("model", limit=None),
        loras=top("lora"),
        edit_modes=top("edit_mode"),
        resolutions=top("resolution"),
        steps=top("steps"),
        samplers=top("sampler"),
        avg_seconds=(seconds / timed) if timed else None,
        success_rate=(success / attempts) if attempts else None,
        error_rate=(error / attempts) if attempts else None,
        success_count=success,
        error_count=error,
        regenerate_count=regenerate,
        edit_count=edit,
        kept_first=max(0, success - regenerate - edit),
        generation_count=generations,
        generation_count_all_time=generations_all,
        success_count_all_time=success_all,
        error_count_all_time=error_all,
    )
