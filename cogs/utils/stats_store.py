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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cogs.utils.constants import DATA_DIR
from cogs.utils.stats_schedule import LA, split_voice_minutes, week_start_for

DEFAULT_DB_PATH = DATA_DIR / "weekly_stats.sqlite3"
TOP_N = 10
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
CREATE TABLE IF NOT EXISTS opt_outs (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
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

    @property
    def week_end(self) -> date:
        return self.week_start + timedelta(days=6)


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


class StatsStore:
    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            self.path = Path(DEFAULT_DB_PATH)
        else:
            self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
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
        if not label:
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
        )

    def close_week(
        self,
        guild_id: int,
        week_start: date | datetime | str,
        closed_at: datetime,
        *,
        known_custom_emojis=None,
        known_channel_ids=None,
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
            )
            self._save_snapshot(guild_id, week, closed, stats)
            self._zero_weekly(guild_id)
            self._set_last_posted(guild_id, week)
        self._buffer.clear()
        return stats

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
        voice_rows = self._conn.execute(
            "SELECT channel_id, weekly, all_time FROM voice_channels WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        heat_rows = self._conn.execute(
            "SELECT weekday, hour, weekly FROM activity_heatmap WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
        voice_heat_rows = self._conn.execute(
            "SELECT weekday, hour, weekly FROM voice_heatmap WHERE guild_id = ?",
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
        top_reactions = _top_reactions(reactions)
        least_custom = _least_custom(reactions, known_custom_emojis)
        top_commands, bottom_commands = _split_commands(commands)
        imagegen = _imagegen_summary(image_rows, total_rows)
        previous = self._previous_snapshot(guild_id, week)
        voice_sorted = sorted(
            (row for row in voice_rows if int(row["weekly"]) > 0),
            key=lambda row: (-int(row["weekly"]), -int(row["all_time"]), int(row["channel_id"])),
        )
        return WeeklyStats(
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
            active_members=int(active),
            active_members_last_week=_snapshot_int(previous, "active_members"),
            heatmap=_heatmap(heat_rows),
            voice_minutes=sum(int(row["weekly"]) for row in voice_rows),
            voice_minutes_all_time=sum(int(row["all_time"]) for row in voice_rows),
            top_voice_channels=tuple(_channel(row) for row in voice_sorted[:TOP_N]),
            voice_heatmap=_heatmap(voice_heat_rows),
            messages_weekly=sum(int(row["weekly"]) for row in channels),
            messages_all_time=sum(int(row["all_time"]) for row in channels),
            reactions_weekly=sum(int(row["weekly"]) for row in reactions),
            reactions_all_time=sum(int(row["all_time"]) for row in reactions),
            commands_weekly=sum(int(row["weekly"]) for row in commands),
            commands_all_time=sum(int(row["all_time"]) for row in commands),
            messages_last_week=_snapshot_int(previous, "messages"),
            reactions_last_week=_snapshot_int(previous, "reactions"),
            voice_minutes_last_week=_snapshot_int(previous, "voice_minutes"),
            imagegen_last_week=_snapshot_imagegens(previous),
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


def _split_channels(rows, known_channel_ids=None) -> tuple[list, list, list]:
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
    active = [row for row in rows if int(row["weekly"]) > 0]
    active.sort(key=lambda row: (-int(row["weekly"]), -int(row["all_time"]), int(row["channel_id"])))
    top = active[:TOP_N]
    top_ids = {int(row["channel_id"]) for row in top}
    bottom_pool = [row for row in active if int(row["channel_id"]) not in top_ids]
    bottom_pool.sort(key=lambda row: (int(row["weekly"]), int(row["all_time"]), int(row["channel_id"])))
    dead = [row for row in rows if int(row["weekly"]) == 0]
    dead.sort(key=lambda row: (-int(row["all_time"]), int(row["channel_id"])))
    return top, bottom_pool[:TOP_N], dead[:DEAD_LIMIT]


def _top_reactions(rows) -> list[ReactionCount]:
    ranked = [row for row in rows if int(row["weekly"]) > 0]
    ranked.sort(key=lambda row: (-int(row["weekly"]), -int(row["all_time"]), str(row["emoji_name"]).lower()))
    return [_reaction(row) for row in ranked[:TOP_N]]


def _reaction(row) -> ReactionCount:
    return ReactionCount(
        str(row["emoji_key"]),
        str(row["emoji_name"]),
        bool(row["is_custom"]),
        int(row["weekly"]),
        int(row["all_time"]),
    )


def _least_custom(rows, known) -> list[ReactionCount]:
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
    ranked = sorted(found.items(), key=lambda item: (item[1]["all_time"], item[1]["weekly"], item[1]["name"].lower(), item[0]))
    return [
        ReactionCount(key, slot["name"], True, slot["weekly"], slot["all_time"])
        for key, slot in ranked[:TOP_N]
    ]


def _split_commands(rows) -> tuple[list[CommandCount], list[CommandCount]]:
    used = [row for row in rows if int(row["weekly"]) > 0]
    used.sort(key=lambda row: (-int(row["weekly"]), -int(row["all_time"]), str(row["command_name"]), str(row["kind"])))
    top = used[:TOP_N]
    top_keys = {(str(row["command_name"]), str(row["kind"])) for row in top}
    rest = [row for row in rows if (str(row["command_name"]), str(row["kind"])) not in top_keys]
    rest.sort(key=lambda row: (int(row["weekly"]), int(row["all_time"]), str(row["command_name"]), str(row["kind"])))
    return [_command(row) for row in top], [_command(row) for row in rest[:TOP_N]]


def _command(row) -> CommandCount:
    return CommandCount(str(row["command_name"]), str(row["kind"]), int(row["weekly"]), int(row["all_time"]))


def _heatmap(rows) -> tuple[tuple[int, ...], ...]:
    grid = [[0 for _ in range(24)] for _ in range(7)]
    for row in rows:
        weekday = int(row["weekday"])
        hour = int(row["hour"])
        if 0 <= weekday <= 6 and 0 <= hour <= 23:
            grid[weekday][hour] = int(row["weekly"])
    return tuple(tuple(line) for line in grid)


def _imagegen_summary(rows, totals) -> ImageGenSummary:
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(str(row["dimension"]), []).append(row)

    def top(dimension: str) -> tuple[DimCount, ...]:
        items = grouped.get(dimension, [])
        items = sorted(items, key=lambda row: (-int(row["weekly"]), -int(row["all_time"]), str(row["item_key"])))
        return tuple(
            DimCount(str(row["item_key"]), int(row["weekly"]), int(row["all_time"]))
            for row in items[:IMAGE_TOP_N]
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
    return ImageGenSummary(
        models=top("model"),
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
    )
