"""Backfill window, classification, and checkpoint. No Discord and no config."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from cogs.utils.stats_schedule import LA, week_start_for
from cogs.utils.stats_store import BackfillPart, StatsStore

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "backfill_weekly_stats",
    ROOT / "scripts" / "backfill_weekly_stats.py",
)
backfill = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = backfill
_SPEC.loader.exec_module(backfill)

CUTOFF = backfill.DEFAULT_CUTOFF
CURRENT = date(2026, 11, 16)
PREVIOUS = date(2026, 11, 9)


def test_days_7_snaps_to_previous_monday_and_stops_at_the_default_cutoff():
    start, end = backfill.resolve_window(days=7, since=None, cutoff=None)
    assert start == datetime(2026, 11, 9, 0, 0, tzinfo=LA)
    assert end == CUTOFF
    assert start == datetime(week_start_for(end - timedelta(days=7)).year, week_start_for(end - timedelta(days=7)).month, week_start_for(end - timedelta(days=7)).day, tzinfo=LA)
    assert backfill.current_week_start(end) == CURRENT
    assert backfill.completed_week_starts(start, end) == [PREVIOUS]


def test_classify_cutoff_current_week_and_previous_week():
    just_before = CUTOFF - timedelta(seconds=1)
    assert backfill.classify_message(CUTOFF, CUTOFF) == "skip"
    assert backfill.classify_message(CUTOFF + timedelta(seconds=1), CUTOFF) == "skip"
    assert backfill.classify_message(just_before, CUTOFF) == "current"
    assert week_start_for(just_before) == CURRENT
    assert backfill.classify_message(datetime(2026, 11, 12, 15, 0, tzinfo=LA), CUTOFF) == "historical"
    assert backfill.classify_message(datetime(2026, 11, 9, 0, 0, tzinfo=LA), CUTOFF) == "historical"
    assert backfill.classify_message(datetime(2026, 11, 15, 23, 30, tzinfo=LA), CUTOFF) == "historical"
    # Sunday 21:00 still belongs to the Monday that started that week.
    assert week_start_for(datetime(2026, 11, 15, 21, 30, tzinfo=LA)) == PREVIOUS
    assert backfill.classify_message(datetime(2026, 11, 16, 0, 5, tzinfo=LA), CUTOFF) == "current"


def test_wider_window_fetches_only_the_earlier_gap():
    start, end = backfill.resolve_window(days=7, since=None, cutoff=CUTOFF)
    after, before = backfill.window_bounds(start, end)
    covered = [(after + 1, before - 1)]
    assert backfill.fetch_gaps(covered, start, end) == []

    wider, _end = backfill.resolve_window(days=30, since=None, cutoff=CUTOFF)
    gaps = backfill.fetch_gaps(covered, wider, end)
    new_after, _new_before = backfill.window_bounds(wider, end)
    assert gaps == [(new_after, after + 1)]
    assert backfill.uncovered_ranges(after + 1, before - 1, covered) == []
    assert backfill.uncovered_ranges(new_after + 1, after, covered) == [(new_after + 1, after)]


def test_prefix_name_is_the_first_token_only():
    assert backfill.prefix_command_name("!stats please") == "stats"
    assert backfill.prefix_command_name("hello") is None
    assert backfill.prefix_command_name("!") is None


def test_current_week_backfill_updates_weekly_and_active_members(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        when = datetime(2026, 11, 18, 12, 0, tzinfo=LA)
        store.record_message(1, 7, 50, when)
        store.record_voice(1, 9, None, when, when + timedelta(minutes=4))
        store.ensure_baseline(1, PREVIOUS)
        store.flush()
        before = store.build_stats(1, when)
        applied = store.apply_backfill(
            1,
            7,
            (100, 110),
            [
                BackfillPart(
                    into_current_week=True,
                    week_start=CURRENT,
                    messages=2,
                    reactions=(("111", "spark", True, 3),),
                    commands=(("stats", 1),),
                    heatmap=((when.weekday(), when.hour, 2),),
                    active_ids=(50, 60),
                )
            ],
            closed_at=CUTOFF,
        )
        assert applied is True
        assert store.apply_backfill(1, 7, (100, 110), [
            BackfillPart(into_current_week=True, week_start=CURRENT, messages=2, active_ids=(60,))
        ]) is False
        stats = store.build_stats(1, when)
        assert stats.messages_weekly == before.messages_weekly + 2
        assert stats.messages_all_time == before.messages_all_time + 2
        assert stats.reactions_weekly == 3
        assert stats.reactions_all_time == 3
        assert stats.commands_weekly == 1
        assert stats.heatmap[when.weekday()][when.hour] == before.heatmap[when.weekday()][when.hour] + 2
        assert stats.active_members == 2
        assert stats.voice_minutes == before.voice_minutes
        assert store.last_posted_week_start(1) == PREVIOUS
        assert store._conn.execute("SELECT COUNT(*) AS n FROM weekly_snapshots").fetchone()["n"] == 0
    finally:
        store.close()


def test_historical_week_updates_all_time_and_snapshot_only(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        when = datetime(2026, 11, 18, 12, 0, tzinfo=LA)
        store.record_message(1, 7, 50, when)
        store.record_voice(1, 9, None, when, when + timedelta(minutes=4))
        store.ensure_baseline(1, PREVIOUS)
        store.flush()
        before = store.build_stats(1, when)
        phrase = "secret phrase that must not be stored"
        assert store.apply_backfill(
            1,
            8,
            (200, 240),
            [
                BackfillPart(
                    into_current_week=False,
                    week_start=PREVIOUS,
                    messages=4,
                    reactions=(("222", "wave", False, 5),),
                    commands=(("ping", 2),),
                    heatmap=((3, 9, 4),),
                    member_delta=3,
                )
            ],
            closed_at=CUTOFF,
        )
        again = store.apply_backfill(
            1,
            8,
            (200, 240),
            [BackfillPart(into_current_week=False, week_start=PREVIOUS, messages=4, member_delta=3)],
            closed_at=CUTOFF,
        )
        assert again is False
        added = store.apply_backfill(
            1,
            8,
            (100, 150),
            [BackfillPart(into_current_week=False, week_start=PREVIOUS, messages=1, member_delta=1)],
            closed_at=CUTOFF,
        )
        assert added is True
        stats = store.build_stats(1, when)
        assert stats.messages_weekly == before.messages_weekly
        assert stats.messages_all_time == before.messages_all_time + 5
        assert stats.reactions_weekly == 0
        assert stats.reactions_all_time == 5
        assert stats.commands_weekly == 0
        assert stats.commands_all_time == 2
        assert stats.heatmap[3][9] == 0
        heat_all = store._conn.execute(
            "SELECT all_time FROM activity_heatmap WHERE guild_id = 1 AND weekday = 3 AND hour = 9"
        ).fetchone()["all_time"]
        assert heat_all == 4
        assert stats.active_members == before.active_members
        assert stats.voice_minutes == before.voice_minutes
        assert store.last_posted_week_start(1) == PREVIOUS
        assert stats.messages_last_week == 5
        assert stats.reactions_last_week == 5
        assert stats.active_members_last_week == 4
        assert stats.voice_minutes_last_week == 0
        raw = store._conn.execute("SELECT summary_json FROM weekly_snapshots").fetchone()["summary_json"]
        assert phrase not in raw
        assert "user_id" not in raw
        blob = Path(store.path).read_bytes()
        assert phrase.encode() not in blob
        assert b"user_counters" not in blob
    finally:
        store.close()


def test_opted_out_author_is_not_marked_active(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        store.opt_out(1, 99)
        store.apply_backfill(
            1,
            7,
            (1, 2),
            [BackfillPart(into_current_week=True, week_start=CURRENT, messages=1, active_ids=(99, 100))],
        )
        ids = {row["user_id"] for row in store._conn.execute("SELECT user_id FROM active_members")}
        assert ids == {100}
    finally:
        store.close()
