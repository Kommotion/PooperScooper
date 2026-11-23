"""Closed-week card overlay, owner-only names, and history backfill helpers."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from cogs.utils.stats_card import CardNames
from cogs.utils.stats_commands import is_owner_only_name, owner_only_names_from_source
from cogs.utils.stats_designs._common import prepare
from cogs.utils.stats_schedule import LA
from cogs.utils.stats_store import StatsStore
from scripts.backfill_imagegen_history import guild_allowed as image_guild_allowed
from scripts.backfill_imagegen_history import image_decision, model_from_caption
from scripts.backfill_slash_history import guild_allowed as slash_guild_allowed
from scripts.backfill_slash_history import normalize_slash_name, slash_command_name
from scripts.rebuild_closed_week_card import (
    guild_allowed,
    message_in_closed_window,
    overlay_snapshot_totals,
)

ROOT_NAMES = owner_only_names_from_source(".")
WEEK = date(2026, 11, 16)
WHEN = datetime(2026, 11, 18, 15, 5, tzinfo=LA)
SUNDAY = datetime(2026, 11, 22, 21, 0, tzinfo=LA)
MONDAY = datetime(2026, 11, 23, 0, 0, tzinfo=LA)


@pytest.fixture
def store(tmp_path):
    opened = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


def test_owner_set_includes_imagegen_status_and_skips_public_commands():
    assert "imagegen status" in ROOT_NAMES
    assert "sync_commands" in ROOT_NAMES
    assert "candyadmin" in ROOT_NAMES
    assert "candyadmin cleanup" in ROOT_NAMES
    assert "palworld is_empty" in ROOT_NAMES
    assert "imagegen generate" not in ROOT_NAMES
    assert "imagegen edit" not in ROOT_NAMES
    assert "imagegen show_default_prompts" not in ROOT_NAMES
    assert "grok" not in ROOT_NAMES
    assert "palworld" not in ROOT_NAMES
    assert is_owner_only_name("imagegen status") is True
    assert is_owner_only_name("imagegen generate") is False


def test_record_command_drops_owner_only_names(store):
    store.record_command(1, "imagegen status", "slash")
    store.record_command(1, "statscard", "prefix")
    store.record_command(1, "imagegen generate", "slash")
    store.flush()
    stats = store.build_stats(1, WHEN)
    names = {row.name for row in stats.top_commands}
    assert names == {"imagegen generate"}
    assert stats.commands_weekly == 1


def test_sunday_window_uses_closed_card_and_monday_is_live(store):
    assert store.record_message(1, 10, 100, WHEN) is True
    store.record_reaction(1, 100, "111", name="spark", is_custom=True, when=WHEN)
    store.record_command(1, "imagegen generate", "slash")
    store.close_week(1, WEEK, SUNDAY)

    closed = store.build_stats(1, WHEN)
    assert closed.messages_weekly == 1
    assert closed.reactions_weekly == 1
    assert closed.commands_weekly == 1
    assert closed.messages_all_time == 1

    live = store.build_stats(1, MONDAY)
    assert live.messages_weekly == 0
    assert live.reactions_weekly == 0
    assert live.commands_weekly == 0
    assert live.voice_minutes == 0
    assert live.messages_all_time == 1


def test_weekly_top_commands_exclude_zeros(store):
    command_map = {name: name for name in ("used", "unused")}
    store.record_command(1, "used", "prefix")
    store.record_command(1, "used", "prefix")
    store.flush()
    stats = store.build_stats(1, WHEN, command_map=command_map)
    assert [row.name for row in stats.top_commands] == ["used"]
    assert all(row.weekly > 0 for row in stats.top_commands)


def test_weekly_model_mix_does_not_borrow_all_time(store):
    store.record_imagegen(1, model="krea2_turbo", success=True)
    store.flush()
    store.close_week(1, date(2026, 11, 9), datetime(2026, 11, 15, 21, tzinfo=LA))
    stats = store.build_stats(1, WHEN)
    assert any(row.key == "krea2_turbo" and row.all_time > 0 for row in stats.imagegen.models)
    assert all(row.weekly == 0 for row in stats.imagegen.models)
    view = prepare(stats, CardNames({}, {}, {}), "Guild", scope="weekly")
    assert view.models == []
    all_time = prepare(stats, CardNames({}, {}, {}), "Guild", scope="all_time")
    assert any(count > 0 for _label, count in all_time.models)


def test_history_imagegen_and_slash_are_idempotent(store):
    store.record_message(1, 8, 10, WHEN)
    store.close_week(1, WEEK, SUNDAY)
    assert store.apply_history_imagegen(1, 9001, WEEK, "krea2_turbo") is True
    assert store.apply_history_imagegen(1, 9001, WEEK, "krea2_turbo") is False
    model = store._conn.execute(
        """
        SELECT weekly, all_time FROM imagegen_counts
        WHERE guild_id = 1 AND dimension = 'model' AND item_key = 'krea2_turbo'
        """
    ).fetchone()
    assert (model["weekly"], model["all_time"]) == (0, 1)
    totals = store._conn.execute(
        "SELECT weekly, all_time FROM imagegen_totals WHERE guild_id = 1 AND metric = 'generations'"
    ).fetchone()
    assert (totals["weekly"], totals["all_time"]) == (0, 1)
    snap = json.loads(
        store._conn.execute(
            "SELECT summary_json FROM weekly_snapshots WHERE guild_id = 1 AND week_start = ?",
            (WEEK.isoformat(),),
        ).fetchone()["summary_json"]
    )
    assert snap["imagegen_success"] == 1
    card = json.loads(
        store._conn.execute(
            "SELECT payload FROM weekly_card_state WHERE guild_id = 1 AND week_start = ?",
            (WEEK.isoformat(),),
        ).fetchone()["payload"]
    )
    assert card["imagegen"]["success_count"] == 1

    assert store.apply_history_slash(1, 9002, WEEK, "imagegen") is True
    assert store.apply_history_slash(1, 9002, WEEK, "imagegen") is False
    row = store._conn.execute(
        """
        SELECT command_name, weekly, all_time FROM command_counts
        WHERE guild_id = 1 AND kind = 'slash'
        """
    ).fetchone()
    assert row["command_name"] == "imagegen generate"
    assert (row["weekly"], row["all_time"]) == (0, 1)
    before = row["all_time"]
    assert store.apply_history_slash(1, 9003, WEEK, "imagegen status") is True
    assert store.apply_history_slash(1, 9003, WEEK, "imagegen status") is False
    after = store._conn.execute(
        "SELECT SUM(all_time) FROM command_counts WHERE guild_id = 1"
    ).fetchone()[0]
    assert after == before
    assert card["commands"] >= 0


def test_rebuild_helpers_stay_local():
    assert guild_allowed(932057681307512922) is True
    assert guild_allowed(1045144253627637830) is False
    assert image_guild_allowed(932057681307512922) is True
    assert image_guild_allowed(1045144253627637830) is False
    assert slash_guild_allowed(1) is False
    start = datetime(2026, 11, 16, 0, 0, tzinfo=LA)
    end = datetime(2026, 11, 22, 21, 0, tzinfo=LA)
    inside = datetime(2026, 11, 18, 12, 0, tzinfo=LA)
    assert message_in_closed_window(inside, start, end) is True
    assert message_in_closed_window(datetime(2026, 11, 23, 0, 0, tzinfo=LA), start, end) is False
    naive = datetime(2026, 11, 18, 20, 0)
    assert message_in_closed_window(naive, start, end) is True
    payload = overlay_snapshot_totals({"active_members": 0}, {"voice_minutes": 12, "imagegen_success": 2, "imagegen_error": 1})
    assert payload["voice_minutes"] == 12
    assert payload["imagegen"]["success_count"] == 2
    assert payload["imagegen"]["error_count"] == 1
    assert payload["imagegen"]["generation_count"] == 3


def test_image_and_slash_classifiers_drop_text():
    assert model_from_caption("`krea2_turbo`") == "krea2_turbo"
    assert model_from_caption("`not-a-model`") == "unknown"
    kept = {
        "author": {"id": "394432249598312459"},
        "attachments": [{"filename": "generated.png"}],
        "content": "`flux3_klein_int8`",
    }
    assert image_decision(kept) == "keep"
    assert model_from_caption(kept["content"]) == "flux3_klein_int8"
    inbox = dict(kept)
    inbox["content"] = "MTS Party Prompts"
    assert image_decision(inbox) == "inbox"
    other = dict(kept)
    other["attachments"] = [{"filename": "halloweenified.png"}]
    assert image_decision(other) == "skip"
    upload = dict(kept)
    upload["attachments"] = [{"filename": "photo.png"}]
    assert image_decision(upload) == "skip"
    assert slash_command_name({"interaction": {"type": 2, "name": "imagegen"}}) == "imagegen generate"
    assert normalize_slash_name("imagegen") == "imagegen generate"
    edit = slash_command_name(
        {"interaction_metadata": {"type": 2, "name": "imagegen", "options": [{"type": 1, "name": "edit"}]}}
    )
    assert edit == "imagegen edit"
    assert slash_command_name({"interaction": {"type": 5, "name": "imagegen"}}) is None
