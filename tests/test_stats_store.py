"""Aggregate weekly stats store. No Discord, no message text."""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from cogs.utils.stats_schedule import LA
from cogs.utils.stats_store import StatsStore

WEEK = date(2026, 11, 16)
WHEN = datetime(2026, 11, 18, 15, 5, tzinfo=LA)
FORBIDDEN_COLUMN_WORDS = ("content", "prompt", "text")


@pytest.fixture
def store(tmp_path):
    opened = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


def _outside_count(path, table: str) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


def _table_names(path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        connection.close()


def _user_id_tables(connection) -> set[str]:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    found = set()
    for table in tables:
        name = table["name"]
        columns = connection.execute(f"PRAGMA table_info({name})").fetchall()
        for column in columns:
            lowered = column["name"].lower()
            if "user" in lowered:
                assert lowered == "user_id", (name, column["name"])
                found.add(name)
    return found


def test_flush_writes_one_transaction_and_nothing_before_it(store):
    assert store.record_message(1, 10, 100, WHEN) is True
    store.record_reaction(1, 100, "wave", name="wave", is_custom=False, when=WHEN)
    store.record_command(1, "imagegen", "slash")
    store.record_imagegen(1, model="krea2_turbo", seconds=1.0, success=True)
    store.record_voice(
        1,
        50,
        100,
        WHEN.replace(hour=10, minute=0),
        WHEN.replace(hour=10, minute=0) + timedelta(minutes=2),
    )

    assert _outside_count(store.path, "channel_messages") == 0
    assert _outside_count(store.path, "reaction_counts") == 0
    assert _outside_count(store.path, "command_counts") == 0
    assert "user_counters" not in _table_names(store.path)
    assert _outside_count(store.path, "imagegen_counts") == 0
    assert _outside_count(store.path, "voice_channels") == 0
    assert _outside_count(store.path, "active_members") == 0

    traced: list[str] = []
    store._conn.set_trace_callback(traced.append)
    store.flush()
    store._conn.set_trace_callback(None)

    begins = [sql for sql in traced if sql.strip().upper().startswith("BEGIN")]
    commits = [sql for sql in traced if sql.strip().upper().startswith("COMMIT")]
    rollbacks = [sql for sql in traced if sql.strip().upper().startswith("ROLLBACK")]
    assert begins == ["BEGIN IMMEDIATE"]
    assert commits == ["COMMIT"]
    assert rollbacks == []

    assert _outside_count(store.path, "channel_messages") == 1
    assert _outside_count(store.path, "reaction_counts") == 1
    assert _outside_count(store.path, "command_counts") == 1
    assert "user_counters" not in _table_names(store.path)
    assert _outside_count(store.path, "imagegen_counts") >= 1
    assert _outside_count(store.path, "voice_channels") == 1
    assert _outside_count(store.path, "active_members") == 1


def test_all_time_survives_close_and_weekly_resets_for_that_guild_only(store):
    other_when = WHEN + timedelta(seconds=1)
    assert store.record_message(1, 10, 100, WHEN) is True
    assert store.record_message(2, 20, 200, other_when) is True
    store.record_reaction(1, 100, "111", name="spark", is_custom=True, when=WHEN)
    store.record_command(1, "imagegen", "slash")
    voice_start = datetime(2026, 11, 18, 10, 0, tzinfo=LA)
    voice_end = datetime(2026, 11, 18, 11, 30, tzinfo=LA)
    store.record_voice(1, 50, 100, voice_start, voice_end)

    closed = store.close_week(1, WEEK, datetime(2026, 11, 22, 21, tzinfo=LA))
    assert closed is not None
    assert closed.week_start == WEEK
    assert closed.messages_weekly == 1
    assert closed.messages_all_time == 1
    assert closed.voice_minutes == 90

    fresh = store.build_stats(1, WHEN)
    assert fresh.messages_weekly == 1
    assert fresh.messages_all_time == 1
    assert fresh.reactions_weekly == 1
    assert fresh.reactions_all_time == 1
    assert fresh.commands_weekly == 1
    assert fresh.commands_all_time == 1
    assert fresh.voice_minutes == 90
    assert fresh.voice_minutes_all_time == 90

    rolled = store.build_stats(1, datetime(2026, 11, 23, 0, 0, tzinfo=LA))
    assert rolled.messages_weekly == 0
    assert rolled.reactions_weekly == 0
    assert rolled.commands_weekly == 0
    assert rolled.voice_minutes == 0
    assert rolled.messages_all_time == 1
    assert rolled.voice_minutes_all_time == 90

    other = store.build_stats(2, other_when)
    assert other.messages_weekly == 1
    assert other.messages_all_time == 1
    assert store.last_posted_week_start(2) is None
    assert store.last_posted_week_start(1) == WEEK


def test_close_week_is_idempotent(store):
    posted_at = datetime(2026, 11, 22, 21, tzinfo=LA)
    store.record_message(1, 5, 9, WHEN)
    closed = store.close_week(1, WEEK, posted_at)
    assert closed is not None
    assert closed.messages_weekly == 1

    later = posted_at + timedelta(seconds=30)
    assert store.record_message(1, 5, 9, later) is True
    again = store.close_week(1, WEEK, later)
    assert again is None

    stats = store.build_stats(1, later)
    assert stats.messages_weekly == 1
    assert stats.messages_all_time == 2
    assert store.last_posted_week_start(1) == WEEK


def test_active_member_delta_comes_from_the_previous_snapshot(store):
    first = datetime(2026, 11, 10, 12, tzinfo=LA)
    store.record_message(1, 8, 10, first)
    store.record_message(1, 8, 11, first)
    closed = store.close_week(1, date(2026, 11, 9), datetime(2026, 11, 15, 21, tzinfo=LA))
    assert closed is not None
    assert closed.active_members == 2
    assert closed.active_members_last_week is None

    payload = store._conn.execute(
        "SELECT summary_json FROM weekly_snapshots WHERE guild_id = 1"
    ).fetchone()["summary_json"]
    assert "active_members" in payload
    assert "prompt" not in payload
    assert "10" not in payload
    assert "11" not in payload

    second = datetime(2026, 11, 17, 12, tzinfo=LA)
    for user_id in (10, 11, 12):
        store.record_message(1, 8, user_id, second)
    stats = store.build_stats(1, second, week_start=date(2026, 11, 16))
    assert stats.active_members == 3
    assert stats.active_members_last_week == 2


def test_rankings_heatmaps_and_least_used_custom_emojis(store):
    user_id = 1000
    for count in range(1, 13):
        for _ in range(count):
            user_id += 1
            store.record_message(1, 100 + count, user_id, WHEN)
    store.record_message(1, 200, 7, WHEN - timedelta(days=8))
    store.close_week(1, date(2026, 11, 9), datetime(2026, 11, 15, 21, tzinfo=LA))

    # Same people as the closed week, still Wednesday 15:00 PT. Every message counts.
    later = WHEN + timedelta(seconds=1)
    user_id = 1000
    for count in range(1, 13):
        for _ in range(count):
            user_id += 1
            store.record_message(1, 100 + count, user_id, later)
    for count in range(1, 13):
        for _ in range(count):
            store.record_command(1, f"cmd{count:02d}", "slash")
    store.record_reaction(1, 5, "111", name="old", is_custom=True, when=later)
    store.record_reaction(1, 6, "111", name="old", is_custom=True, when=later)
    store.record_reaction(1, 5, "wave", name="wave", is_custom=False, when=later)

    stats = store.build_stats(
        1,
        WHEN,
        known_custom_emojis=[("222", "unused"), ("111", "spark")],
    )
    assert [row.weekly for row in stats.top_channels] == list(range(12, 2, -1))
    assert [row.channel_id for row in stats.top_channels] == list(range(112, 102, -1))
    assert [row.weekly for row in stats.bottom_channels] == [1, 2]
    assert any(row.channel_id == 200 and row.weekly == 0 and row.all_time == 1 for row in stats.dead_channels)

    assert stats.top_commands[0].name == "cmd12"
    assert stats.top_commands[0].all_time == 12
    assert [row.name for row in stats.top_commands] == ["cmd12", "cmd11", "cmd10", "cmd09", "cmd08"]
    assert [row.name for row in stats.bottom_commands] == ["cmd01", "cmd02", "cmd03", "cmd04", "cmd05"]
    assert len(stats.heatmap) == 7
    assert all(len(row) == 24 for row in stats.heatmap)
    assert stats.heatmap[2][15] == 78 + 3  # messages this hour plus three reactions
    sunday = datetime(2026, 11, 1, 21, 30, tzinfo=LA)
    store.record_message(1, 112, 4242, sunday)
    sunday_stats = store.build_stats(1, sunday)
    assert sunday_stats.heatmap[6][21] == 1

    least = stats.least_custom_emojis
    assert least[0].emoji_key == "222"
    assert least[0].name == "unused"
    assert least[0].all_time == 0
    spark = next(row for row in least if row.emoji_key == "111")
    assert spark.name == "spark"
    assert spark.all_time == 2
    assert all(row.is_custom for row in least)


def test_late_sunday_after_close_stays_out_of_the_next_week(store):
    week = date(2026, 11, 16)
    store.close_week(1, week, datetime(2026, 11, 22, 21, tzinfo=LA))
    late = datetime(2026, 11, 22, 21, 30, tzinfo=LA)
    store.record_message(1, 8, 10, late)
    store.record_voice(1, 9, 10, late, late + timedelta(minutes=5))
    monday = datetime(2026, 11, 23, 8, tzinfo=LA)
    store.record_message(1, 8, 11, monday)
    morning = datetime(2026, 11, 23, 9, 30, tzinfo=LA)
    stats = store.build_stats(1, morning)
    assert stats.week_start == date(2026, 11, 23)
    assert stats.heatmap[6][21] == 0
    assert stats.heatmap[0][8] > 0
    assert stats.heatmap_all_time[6][21] >= 1
    assert stats.voice_heatmap[6][21] == 0
    assert stats.voice_heatmap_all_time[6][21] >= 1
    assert stats.messages_weekly == 1
    assert stats.messages_all_time == 2


def test_every_message_counts_without_a_cooldown(tmp_path):
    path = tmp_path / "weekly_stats.sqlite3"
    store = StatsStore(path)
    start = WHEN
    try:
        assert store.record_message(1, 10, 5, start) is True
        assert store.record_message(1, 11, 5, start) is True
        assert store.record_message(1, 12, 5, start + timedelta(seconds=1)) is True
        assert store.record_message(1, 13, 5, start + timedelta(seconds=29)) is True
        assert store._buffer.channels[(1, 10, True)] == 1
        assert store._buffer.channels[(1, 11, True)] == 1
        assert store._buffer.heatmap[(1, 2, 15, True)] == 4
        assert (1, 5) in store._buffer.active
        assert store.record_message(2, 10, 5, start) is True
        stats = store.build_stats(1, start)
        assert stats.messages_weekly == 4
        assert stats.heatmap[2][15] == 4
        assert stats.active_members == 1
        assert _outside_count(path, "channel_messages") == 5
        assert "user_counters" not in _table_names(path)
    finally:
        store.close()

    reopened = StatsStore(path)
    try:
        assert reopened.record_message(1, 14, 5, start + timedelta(seconds=1)) is True
        reopened.flush()
        assert _outside_count(path, "channel_messages") == 6
        assert reopened.build_stats(1, start).active_members == 1
    finally:
        reopened.close()


def test_opt_out_removes_active_member_and_blocks_future_active_marking(store):
    store.record_message(1, 10, 5, WHEN)
    store.record_reaction(1, 5, "wave", name="wave", is_custom=False, when=WHEN)
    store.flush()
    assert store._conn.execute("SELECT COUNT(*) FROM active_members").fetchone()[0] == 1

    later = WHEN + timedelta(seconds=1)
    store.record_message(1, 10, 5, later)
    store.record_message(1, 10, 6, later)
    store.opt_out(1, 5)

    assert store.is_opted_out(1, 5) is True
    assert (1, 5) not in store._buffer.active
    assert (1, 6) in store._buffer.active
    assert store._conn.execute(
        "SELECT COUNT(*) FROM active_members WHERE guild_id = 1 AND user_id = 5"
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM opt_outs WHERE guild_id = 1 AND user_id = 5"
    ).fetchone()[0] == 1

    store.record_message(1, 11, 5, later + timedelta(seconds=1))
    store.record_reaction(1, 5, "wave", name="wave", is_custom=False, when=later)
    store.record_voice(1, 50, 5, later, later + timedelta(minutes=2))
    stats = store.build_stats(1, later)
    assert stats.messages_weekly == 4
    assert stats.reactions_weekly == 2
    assert stats.voice_minutes == 2
    assert stats.active_members == 1
    active_ids = {
        row["user_id"]
        for row in store._conn.execute("SELECT user_id FROM active_members WHERE guild_id = 1")
    }
    assert active_ids == {6}

    store.opt_in(1, 5)
    assert store.is_opted_out(1, 5) is False
    resumed = later + timedelta(minutes=2)
    store.record_message(1, 10, 5, resumed)
    store.record_reaction(1, 5, "wave", name="wave", is_custom=False, when=resumed)
    resumed_stats = store.build_stats(1, resumed)
    assert resumed_stats.active_members == 2
    resumed_ids = {
        row["user_id"]
        for row in store._conn.execute("SELECT user_id FROM active_members WHERE guild_id = 1")
    }
    assert resumed_ids == {5, 6}


def test_imagegen_summary_has_no_prompt_and_derives_kept_first(store):
    signature = inspect.signature(StatsStore.record_imagegen)
    assert "prompt" not in signature.parameters
    assert not any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())

    for _ in range(3):
        store.record_imagegen(
            1,
            model="krea2_turbo",
            loras=("detail",),
            edit_mode="img2img",
            width=512,
            height=768,
            steps=8,
            sampler="euler",
            seconds=None,
            success=True,
        )
    store.record_imagegen(1, model="flux", seconds=3.0, success=True)
    store.record_imagegen(1, model="flux", seconds=6.0, success=False)
    store.record_imagegen_followup(1, "regenerate")
    store.record_imagegen_followup(1, "edit")
    store.record_imagegen_followup(1, "kept")
    store.record_imagegen_followup(1, "nope")

    stats = store.build_stats(1, WHEN).imagegen
    assert stats.models[0].key == "krea2_turbo"
    assert stats.models[0].weekly == 3
    assert stats.loras[0].key == "detail"
    assert stats.edit_modes[0].key == "img2img"
    assert stats.resolutions[0].key == "512x768"
    assert stats.steps[0].key == "8"
    assert stats.samplers[0].key == "euler"
    assert stats.success_count == 4
    assert stats.error_count == 1
    assert stats.success_rate == pytest.approx(0.8)
    assert stats.error_rate == pytest.approx(0.2)
    assert stats.avg_seconds == pytest.approx(4.5)
    assert stats.regenerate_count == 1
    assert stats.edit_count == 1
    assert stats.kept_first == 2
    assert stats.generation_count == 5


def test_schema_columns_do_not_store_content_prompt_or_text(store):
    tables = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    assert tables
    names = {row["name"] for row in tables}
    assert "user_counters" not in names
    assert _user_id_tables(store._conn) == {"active_members", "opt_outs"}
    for row in tables:
        info = store._conn.execute(f"PRAGMA table_info({row['name']})").fetchall()
        for column in info:
            lowered = column["name"].lower()
            for word in FORBIDDEN_COLUMN_WORDS:
                assert word not in lowered, (row["name"], column["name"])


def test_voice_hours_split_into_the_voice_heatmap(store):
    start = datetime(2026, 11, 16, 10, 0, tzinfo=LA)
    end = datetime(2026, 11, 16, 11, 30, tzinfo=LA)
    assert store.record_voice(1, 50, 7, start, end) == 90
    stats = store.build_stats(1, end)
    assert stats.voice_minutes == 90
    assert stats.voice_heatmap[0][10] == 60
    assert stats.voice_heatmap[0][11] == 30
    assert stats.heatmap[0][10] == 0
    assert stats.top_voice_channels[0].channel_id == 50
    assert stats.top_voice_channels[0].weekly == 90
    assert stats.voice_minutes_all_time == 90
    assert stats.active_members == 0
    assert "user_counters" not in _table_names(store.path)


def test_ensure_baseline_does_not_post_or_reset_counters(store):
    assert store.record_message(1, 10, 5, WHEN) is True
    assert store.ensure_baseline(1, WEEK) is True
    assert store.last_posted_week_start(1) == WEEK
    assert store.ensure_baseline(1, date(2026, 11, 23)) is False
    assert store.last_posted_week_start(1) == WEEK

    stats = store.build_stats(1, WHEN)
    assert stats.messages_weekly == 1
    assert stats.messages_all_time == 1
    assert store._conn.execute("SELECT COUNT(*) FROM weekly_snapshots").fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT weekly, all_time FROM channel_messages WHERE guild_id = 1 AND channel_id = 10"
    ).fetchone()["weekly"] == 1


def test_opening_drops_a_leftover_user_counters_table(tmp_path):
    path = tmp_path / "weekly_stats.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE user_counters (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            metric TEXT NOT NULL,
            weekly INTEGER NOT NULL DEFAULT 0,
            all_time INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, metric)
        )
        """
    )
    connection.execute(
        "INSERT INTO user_counters (guild_id, user_id, metric, weekly, all_time) VALUES (1, 5, 'messages', 4, 9)"
    )
    connection.execute(
        "INSERT INTO user_counters (guild_id, user_id, metric, weekly, all_time) VALUES (1, 6, 'voice_minutes', 30, 30)"
    )
    connection.commit()
    connection.close()

    store = StatsStore(path)
    try:
        assert "user_counters" not in _table_names(path)
        assert _user_id_tables(store._conn) == {"active_members", "opt_outs"}
        store.record_message(1, 10, 5, WHEN)
        stats = store.build_stats(1, WHEN)
        assert stats.messages_weekly == 1
        assert stats.active_members == 1
    finally:
        store.close()

    again = StatsStore(path)
    try:
        assert "user_counters" not in _table_names(path)
        assert again.build_stats(1, WHEN).messages_weekly == 1
    finally:
        again.close()


def test_known_channels_hide_deleted_ones_and_list_unseen_dead_channels(store):
    for user_id in range(1, 6):
        store.record_message(1, 99, user_id, WHEN)
    store.record_message(1, 10, 6, WHEN)
    old = WHEN - timedelta(days=8)
    store.record_message(1, 40, 7, old)
    store.close_week(1, date(2026, 11, 9), datetime(2026, 11, 15, 21, tzinfo=LA))
    for user_id in range(1, 6):
        store.record_message(1, 99, user_id, WHEN)
    store.record_message(1, 10, 6, WHEN)

    stats = store.build_stats(1, WHEN, known_channel_ids=[10, 40, 20, 20])
    assert [row.channel_id for row in stats.top_channels] == [10]
    assert stats.bottom_channels == ()
    assert [(row.channel_id, row.weekly, row.all_time) for row in stats.dead_channels] == [
        (40, 0, 1),
        (20, 0, 0),
    ]
    assert stats.messages_weekly == 6
    listed = [row.channel_id for row in stats.top_channels + stats.bottom_channels + stats.dead_channels]
    assert 99 not in listed

    plain = store.build_stats(1, WHEN)
    plain_ids = [row.channel_id for row in plain.top_channels + plain.bottom_channels + plain.dead_channels]
    assert 99 in plain_ids
    assert 40 in [row.channel_id for row in plain.dead_channels]
    assert 20 not in plain_ids


def test_known_custom_emojis_drop_deleted_ones(store):
    store.record_reaction(1, 5, "333", name="gone", is_custom=True, when=WHEN)
    store.record_reaction(1, 6, "111", name="old", is_custom=True, when=WHEN)
    store.record_reaction(1, 6, "wave", name="wave", is_custom=False, when=WHEN)

    limited = store.build_stats(
        1,
        WHEN,
        known_custom_emojis=[("222", "unused"), ("111", "spark")],
    )
    keys = [row.emoji_key for row in limited.least_custom_emojis]
    assert "333" not in keys
    assert keys[0] == "222"
    spark = next(row for row in limited.least_custom_emojis if row.emoji_key == "111")
    assert spark.name == "spark"
    assert spark.all_time == 1

    stored = store.build_stats(1, WHEN)
    stored_keys = [row.emoji_key for row in stored.least_custom_emojis]
    assert "333" in stored_keys
    assert "222" not in stored_keys

    empty = store.build_stats(1, WHEN, known_custom_emojis=[])
    assert empty.least_custom_emojis == ()


def test_all_time_heatmap_rankings_history_and_earliest_week(store):
    from cogs.utils.stats_store import LIVE_TRACKING_SINCE

    previous = WHEN - timedelta(days=7)
    store.record_message(1, 11, 6, previous)
    store.record_message(1, 11, 6, previous)
    store.record_reaction(1, 6, "old", name="old", is_custom=True, when=previous)
    store.record_command(1, "oldcmd", "prefix")
    store.close_week(1, date(2026, 11, 9), datetime(2026, 11, 15, 21, tzinfo=LA))
    store.record_message(1, 10, 7, WHEN)
    for channel_id in range(101, 111):
        store.record_message(1, channel_id, 7, WHEN + timedelta(hours=1))
    store.record_reaction(1, 7, "new", name="new", is_custom=True, when=WHEN)
    store.record_command(1, "newcmd", "slash")
    known_channels = [10, 11, 12, *range(101, 111)]
    stats = store.build_stats(
        1,
        WHEN,
        known_channel_ids=known_channels,
        known_custom_emojis=[("old", "old"), ("new", "new"), ("zzz", "zzz")],
    )
    assert stats.heatmap[2][15] == 2  # one message and one reaction this week
    assert stats.heatmap_all_time[2][15] == 5  # prior week: 2 messages + 1 reaction, plus this week
    assert stats.heatmap_all_time != stats.heatmap
    assert [row.channel_id for row in stats.top_channels_all_time] == [
        11, 10, 101, 102, 103, 104, 105, 106, 107, 108
    ]
    assert [row.channel_id for row in stats.bottom_channels_all_time] == [109, 110]
    assert all(row.all_time > 0 for row in stats.bottom_channels_all_time)
    assert not any(row.channel_id == 12 for row in stats.bottom_channels_all_time)
    assert [row.emoji_key for row in stats.top_reactions] == ["new"]
    assert [row.emoji_key for row in stats.top_reactions_all_time] == ["new", "old"]
    assert stats.least_custom_emojis[0].emoji_key == "zzz"
    assert stats.least_custom_emojis_weekly[0].emoji_key == "zzz"
    assert [row.name for row in stats.top_commands_all_time] == ["newcmd", "oldcmd"]
    assert stats.earliest_week == date(2026, 11, 9)
    assert [point.week_start for point in stats.week_history] == [date(2026, 11, 9), date(2026, 11, 16)]
    assert stats.week_history[0].messages == 2
    assert stats.week_history[0].reactions == 1
    assert stats.week_history[1].messages == 11
    assert stats.week_history[1].active_members == 1
    assert stats.live_since == LIVE_TRACKING_SINCE

    fresh = store.build_stats(2, WHEN)
    assert fresh.earliest_week == fresh.week_start
    assert [point.week_start for point in fresh.week_history] == [fresh.week_start]
    assert fresh.live_since == LIVE_TRACKING_SINCE

    store._conn.execute(
        "INSERT INTO backfill_meta (guild_id, live_at) VALUES (1, ?)",
        ("2026-11-20T18:00:00+00:00",),
    )
    marked = store.build_stats(1, WHEN)
    assert marked.live_since == date(2026, 11, 20)


def test_all_time_quiet_channels_are_lowest_nonzero(store):
    for channel_id, count in enumerate(range(1, 13), start=1):
        for _ in range(count):
            store.record_message(1, channel_id, 5, WHEN)
    stats = store.build_stats(1, WHEN, known_channel_ids=list(range(1, 14)))
    assert [row.channel_id for row in stats.top_channels_all_time] == list(range(12, 2, -1))
    assert [(row.channel_id, row.all_time) for row in stats.bottom_channels_all_time] == [(1, 1), (2, 2)]
    assert all(row.all_time > 0 for row in stats.top_channels_all_time + stats.bottom_channels_all_time)
    weekly = store.build_stats(1, WHEN, known_channel_ids=list(range(1, 14)))
    assert any(row.channel_id == 13 and row.weekly == 0 for row in weekly.dead_channels)


def test_command_lists_are_top_and_least_five_including_unused(store):
    command_map = {name: name for name in [f"cmd{count:02d}" for count in range(1, 10)]}
    command_map["unused"] = "unused"
    for count in range(1, 10):
        for _ in range(count):
            store.record_command(1, f"cmd{count:02d}", "prefix")
    store.flush()
    stats = store.build_stats(1, WHEN, command_map=command_map)
    assert [row.name for row in stats.top_commands_all_time] == ["cmd09", "cmd08", "cmd07", "cmd06", "cmd05"]
    assert [row.name for row in stats.bottom_commands_all_time] == ["unused", "cmd01", "cmd02", "cmd03", "cmd04"]
    assert stats.bottom_commands_all_time[0].all_time == 0
    assert [row.name for row in stats.top_commands] == ["cmd09", "cmd08", "cmd07", "cmd06", "cmd05"]
    assert [row.name for row in stats.bottom_commands] == ["unused", "cmd01", "cmd02", "cmd03", "cmd04"]
    assert stats.commands_weekly == 45

    store.close_week(1, date(2026, 11, 9), datetime(2026, 11, 15, 21, tzinfo=LA))
    for _ in range(3):
        store.record_command(1, "cmd01", "prefix")
    weekly = store.build_stats(1, WHEN, command_map=command_map)
    assert [row.name for row in weekly.top_commands] == ["cmd01"]
    least = [row.name for row in weekly.bottom_commands]
    assert least[0] == "unused"
    assert weekly.bottom_commands[0].weekly == 0
    assert "cmd01" not in least
    plain = store.build_stats(1, WHEN)
    assert "unused" not in {row.name for row in plain.top_commands + plain.bottom_commands}


def test_closed_week_imagegen_backfill_is_idempotent(store):
    store.record_message(1, 8, 10, WHEN)
    closed = store.close_week(1, date(2026, 11, 9), datetime(2026, 11, 15, 21, tzinfo=LA))
    assert closed is not None
    posted = store.last_posted_week_start(1)
    before_messages = store._conn.execute(
        "SELECT weekly, all_time FROM channel_messages WHERE guild_id = 1"
    ).fetchone()
    applied = store.apply_closed_week_imagegen_backfill(
        1,
        date(2026, 11, 9),
        {"krea2_turbo": 3, "flux": 1},
        success=3,
        error=1,
        key="inbox-2022-01",
    )
    assert applied is True
    models = {
        row["item_key"]: (row["weekly"], row["all_time"])
        for row in store._conn.execute(
            "SELECT item_key, weekly, all_time FROM imagegen_counts WHERE guild_id = 1 AND dimension = 'model'"
        )
    }
    assert models == {"krea2_turbo": (0, 3), "flux": (0, 1)}
    outcomes = {
        row["item_key"]: (row["weekly"], row["all_time"])
        for row in store._conn.execute(
            "SELECT item_key, weekly, all_time FROM imagegen_counts WHERE guild_id = 1 AND dimension = 'outcome'"
        )
    }
    assert outcomes == {"success": (0, 3), "error": (0, 1)}
    totals = store._conn.execute(
        "SELECT weekly, all_time FROM imagegen_totals WHERE guild_id = 1 AND metric = 'generations'"
    ).fetchone()
    assert (totals["weekly"], totals["all_time"]) == (0, 4)
    payload = json.loads(
        store._conn.execute(
            "SELECT summary_json FROM weekly_snapshots WHERE guild_id = 1 AND week_start = '2026-11-09'"
        ).fetchone()["summary_json"]
    )
    assert payload["imagegen_success"] == 3
    assert payload["imagegen_error"] == 1
    assert payload["messages"] == closed.messages_weekly
    assert store.last_posted_week_start(1) == posted
    after_messages = store._conn.execute(
        "SELECT weekly, all_time FROM channel_messages WHERE guild_id = 1"
    ).fetchone()
    assert (after_messages["weekly"], after_messages["all_time"]) == (
        before_messages["weekly"],
        before_messages["all_time"],
    )
    again = store.apply_closed_week_imagegen_backfill(
        1,
        date(2026, 11, 9),
        {"krea2_turbo": 9},
        success=9,
        error=9,
        key="inbox-2022-01",
    )
    assert again is False
    totals = store._conn.execute(
        "SELECT weekly, all_time FROM imagegen_totals WHERE guild_id = 1 AND metric = 'generations'"
    ).fetchone()
    assert (totals["weekly"], totals["all_time"]) == (0, 4)
    payload_again = json.loads(
        store._conn.execute(
            "SELECT summary_json FROM weekly_snapshots WHERE guild_id = 1"
        ).fetchone()["summary_json"]
    )
    assert payload_again["imagegen_success"] == 3
    assert payload_again["imagegen_error"] == 1
