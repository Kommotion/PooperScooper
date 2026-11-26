"""Weekly stats stay aggregate. No message text and no user ids in the snapshot.

The only table that stores a user id is this week's active_members.
"""

from __future__ import annotations

import inspect
import json
from datetime import date, datetime, timedelta

from cogs.utils.stats_schedule import LA
from cogs.utils.stats_store import StatsStore
from cogs.weekly_stats import WeeklyStats

WHEN = datetime(2026, 11, 18, 15, 5, tzinfo=LA)
FORBIDDEN_COLUMN_WORDS = ("content", "text", "prompt", "message_body")


def test_schema_columns_do_not_name_message_content(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        tables = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        assert tables
        checked = 0
        for table in tables:
            columns = store._conn.execute(f"PRAGMA table_info({table['name']})").fetchall()
            assert columns
            for column in columns:
                checked += 1
                lowered = column["name"].lower()
                for word in FORBIDDEN_COLUMN_WORDS:
                    assert word not in lowered, (table["name"], column["name"])
        assert checked > 0
    finally:
        store.close()


def test_record_signatures_have_no_prompt_or_content_params():
    methods = (
        StatsStore.record_message,
        StatsStore.record_imagegen,
        WeeklyStats.record_imagegen,
    )
    for method in methods:
        signature = inspect.signature(method)
        for name, param in signature.parameters.items():
            lowered = name.lower()
            assert "prompt" not in lowered
            assert "content" not in lowered
            assert param.kind is not inspect.Parameter.VAR_KEYWORD


def test_weekly_snapshot_json_has_no_user_ids(tmp_path):
    user_id = 867530986
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        store.record_message(1, 10, user_id, WHEN)
        store.record_reaction(1, user_id, "111", name="spark", is_custom=True, when=WHEN)
        closed = store.close_week(1, date(2026, 11, 16), WHEN)
        assert closed is not None
        raw = store._conn.execute(
            "SELECT summary_json FROM weekly_snapshots WHERE guild_id = 1"
        ).fetchone()["summary_json"]
        assert str(user_id) not in raw
        assert "user_id" not in raw.lower()

        def walk(value) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    assert "user" not in str(key).lower()
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, int):
                assert value != user_id
            elif isinstance(value, str):
                assert str(user_id) not in value

        walk(json.loads(raw))
    finally:
        store.close()


def test_only_active_members_contain_user_ids(tmp_path):
    active_id = 867530986
    other_id = 867530987
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        tables = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        assert tables
        names = {table["name"] for table in tables}
        assert not any(name.startswith("opt_") for name in names)
        with_user_id = set()
        for table in tables:
            name = table["name"]
            assert name != "user_counters"
            columns = store._conn.execute(f"PRAGMA table_info({name})").fetchall()
            for column in columns:
                lowered = column["name"].lower()
                if "user" in lowered:
                    assert lowered == "user_id", (name, column["name"])
                    with_user_id.add(name)
        assert with_user_id == {"active_members"}

        store.record_message(1, 10, active_id, WHEN)
        store.record_reaction(1, active_id, "111", name="spark", is_custom=True, when=WHEN)
        store.record_voice(1, 50, active_id, WHEN, WHEN + timedelta(minutes=5))
        store.record_message(1, 11, other_id, WHEN)
        store.record_reaction(1, other_id, "222", name="quiet", is_custom=True, when=WHEN)
        store.record_message(1, 12, other_id, WHEN + timedelta(seconds=1))
        store.flush()

        active_ids = {
            row["user_id"]
            for row in store._conn.execute("SELECT user_id FROM active_members")
        }
        assert active_ids == {active_id, other_id}

        for table in tables:
            name = table["name"]
            if name in with_user_id:
                continue
            rows = store._conn.execute(f"SELECT * FROM {name}").fetchall()
            blob = " ".join(str(tuple(row)) for row in rows)
            assert str(active_id) not in blob
            assert str(other_id) not in blob
    finally:
        store.close()
