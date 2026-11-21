"""Prefix-command filtering for weekly stats. No bot, no Discord, no config."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

from cogs.utils.stats_commands import alias_map_from_source, canonical_command_name
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

SOURCE_MAP = alias_map_from_source(ROOT)
WEEK = "2026-11-09"
WHEN = datetime(2026, 11, 12, 15, 0, tzinfo=LA)
JUNK = ("playhttps", "gay!gay!gay", "plworld", "spot")


def test_source_map_has_registered_names_only():
    for name in (
        "palworld",
        "play",
        "gay",
        "help",
        "stop",
        "join",
        "uptime",
        "candyadmin",
        "sync_commands",
        "statscard",
    ):
        assert name in SOURCE_MAP
    for name in ("spot", "plworld", "gay!gay!gay"):
        assert name not in SOURCE_MAP


def test_canonical_command_name_keeps_registered_tokens():
    assert canonical_command_name("!play https://x", SOURCE_MAP) == "play"
    assert canonical_command_name("play https://x", SOURCE_MAP) == "play"
    assert canonical_command_name("!Play", SOURCE_MAP) == "play"
    assert canonical_command_name("!gay", SOURCE_MAP) == "gay"
    for raw in ("!playhttps://x", "!gay!gay!gay", "!plworld", "!spot", "", None):
        assert canonical_command_name(raw, SOURCE_MAP) is None


def _payload(content: str) -> dict:
    return {
        "content": content,
        "timestamp": "2026-11-12T15:00:00-08:00",
        "author": {"id": "5"},
    }


def _tally(contents: list[str], command_map: dict[str, str]) -> dict[str, int]:
    weeks: dict = {}
    tally = backfill.WindowTally()
    for content in contents:
        backfill._tally_message(
            _payload(content),
            7,
            backfill.DEFAULT_CUTOFF,
            set(),
            tally,
            weeks,
            command_map,
        )
    bucket = weeks.get(week_start_for(WHEN))
    return {} if bucket is None else dict(bucket.commands)


def test_tally_counts_registered_names_from_the_source_map():
    counted = _tally(
        ["!gay", "!play https://x", "!playhttps://x", "!spot", "!plworld", "!gay!gay!gay"],
        SOURCE_MAP,
    )
    assert counted.get("gay") == 1
    assert counted.get("play") == 1
    for name in JUNK:
        assert name not in counted


def test_tally_merges_alias_tokens_that_share_a_canonical_name():
    counted = _tally(["!p song", "!play song", "!spot"], {"p": "play", "play": "play"})
    assert counted == {"play": 2}


def test_apply_backfill_drops_unregistered_and_merges_aliases(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        applied = store.apply_backfill(
            1,
            7,
            (1, 10),
            [
                BackfillPart(
                    into_current_week=False,
                    week_start=WEEK,
                    commands=(("play", 2), ("p", 3), ("spot", 4), ("playhttps", 1)),
                )
            ],
            command_map={"play": "play", "p": "play"},
        )
        assert applied is True
        counts = {
            row["command_name"]: (row["weekly"], row["all_time"])
            for row in store._conn.execute("SELECT command_name, weekly, all_time FROM command_counts")
        }
        assert counts == {"play": (0, 5)}
        buckets = {
            row["item_key"]: row["amount"]
            for row in store._conn.execute("SELECT item_key, amount FROM backfill_week WHERE kind = 'command'")
        }
        assert buckets == {"play": 5}
    finally:
        store.close()


def _seed(store: StatsStore) -> None:
    prefix_rows = [
        ("play", 10, 20),
        ("p", 1, 2),
        ("gay", 6, 7),
        ("playhttps", 2, 5),
        ("gay!gay!gay", 1, 1),
        ("plworld", 3, 3),
        ("spot", 4, 8),
    ]
    for name, weekly, all_time in prefix_rows:
        store._conn.execute(
            """
            INSERT INTO command_counts (guild_id, command_name, kind, weekly, all_time)
            VALUES (1, ?, 'prefix', ?, ?)
            """,
            (name, weekly, all_time),
        )
    store._conn.execute(
        """
        INSERT INTO command_counts (guild_id, command_name, kind, weekly, all_time)
        VALUES (1, 'imagine', 'slash', 9, 9)
        """
    )
    for name, _weekly, all_time in prefix_rows:
        store._conn.execute(
            """
            INSERT INTO backfill_week
                (guild_id, week_start, kind, item_key, item_label, flag, amount)
            VALUES (1, ?, 'command', ?, 'prefix', 0, ?)
            """,
            (WEEK, name, all_time),
        )
    summary = {
        "messages": 40,
        "commands": 99,
        "top_commands": [
            {"name": name, "kind": "prefix", "weekly": 1} for name in ("spot", "play", "playhttps")
        ],
    }
    store._conn.execute(
        """
        INSERT INTO weekly_snapshots (guild_id, week_start, closed_at, summary_json)
        VALUES (1, ?, ?, ?)
        """,
        (WEEK, "2026-11-16T00:00:00-08:00", json.dumps(summary)),
    )


def test_purge_drops_junk_and_rewrites_the_snapshot(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        _seed(store)
        dropped = store.purge_unregistered_commands({"play": "play", "p": "play", "gay": "gay"})
        assert dropped == [
            {"name": "gay!gay!gay", "kind": "prefix", "weekly": 1, "all_time": 1},
            {"name": "playhttps", "kind": "prefix", "weekly": 2, "all_time": 5},
            {"name": "plworld", "kind": "prefix", "weekly": 3, "all_time": 3},
            {"name": "spot", "kind": "prefix", "weekly": 4, "all_time": 8},
        ]
        counts = {
            (row["command_name"], row["kind"]): (row["weekly"], row["all_time"])
            for row in store._conn.execute(
                "SELECT command_name, kind, weekly, all_time FROM command_counts"
            )
        }
        assert counts == {
            ("play", "prefix"): (11, 22),
            ("gay", "prefix"): (6, 7),
            ("imagine", "slash"): (9, 9),
        }
        buckets = {
            row["item_key"]: row["amount"]
            for row in store._conn.execute(
                "SELECT item_key, amount FROM backfill_week WHERE kind = 'command'"
            )
        }
        assert buckets == {"play": 22, "gay": 7}
        payload = json.loads(
            store._conn.execute("SELECT summary_json FROM weekly_snapshots").fetchone()["summary_json"]
        )
        assert payload["messages"] == 40
        assert payload["commands"] == 29
        assert payload["top_commands"] == [
            {"name": "play", "kind": "prefix", "weekly": 22},
            {"name": "gay", "kind": "prefix", "weekly": 7},
        ]
        for name in JUNK:
            assert name not in counts
            assert name not in buckets
    finally:
        store.close()


def test_build_stats_keeps_fake_names_until_a_map_is_passed(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        store.record_command(1, "playhttps", "prefix")
        store.record_command(1, "spot", "prefix")
        store.record_command(1, "play", "prefix")
        store.record_command(1, "p", "prefix")
        store.flush()
        unfiltered = store.build_stats(1, WHEN)
        names = {row.name for row in unfiltered.top_commands}
        assert {"playhttps", "spot", "play", "p"} <= names
        assert unfiltered.commands_weekly == 4
        filtered = store.build_stats(1, WHEN, command_map={"play": "play", "p": "play"})
        by_name = {row.name: row for row in filtered.top_commands}
        assert set(by_name) == {"play"}
        assert by_name["play"].weekly == 2
        assert by_name["play"].all_time == 2
        assert filtered.commands_weekly == 2
    finally:
        store.close()
