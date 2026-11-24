"""Closed-week card overlay, owner-only names, and history backfill helpers."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

import sqlite3
from types import SimpleNamespace

from cogs.utils.constants import POOPER_APP_ID
from cogs.utils.stats_card import CardNames
from cogs.utils.stats_card import _command_label as card_command_label
from cogs.utils.stats_commands import is_owner_only_name, owner_only_names_from_source
from cogs.utils.stats_designs._common import _command_label, prepare
from cogs.utils.stats_designs.design_a import fold_imagegen_models
from cogs.utils.stats_schedule import LA
from cogs.utils.stats_store import StatsStore, _apply_closed_card, _imagegen_from_payload
from scripts.backfill_imagegen_history import guild_allowed as image_guild_allowed
from scripts.backfill_imagegen_history import image_decision, model_from_caption
from scripts.backfill_slash_history import guild_allowed as slash_guild_allowed
from scripts.backfill_slash_history import (
    DEFAULT_DB,
    LIVE_RETAG_ERROR,
    _is_live_db,
    build_parser,
    classify_slash_use,
    live_retag_refusal,
    normalize_slash_name,
    resolve_own_application_id,
    slash_command_name,
    slash_event_name,
)
from cogs.weekly_stats import GuildStatsConfig, WeeklyStats, foreign_slash_from_message, interaction_is_foreign
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
    assert "imagegen status" not in ROOT_NAMES
    assert "reload_music" in ROOT_NAMES
    assert "sync_commands" in ROOT_NAMES
    assert "candyadmin" in ROOT_NAMES
    assert "candyadmin cleanup" in ROOT_NAMES
    assert "palworld is_empty" in ROOT_NAMES
    assert "imagegen generate" not in ROOT_NAMES
    assert "imagegen edit" not in ROOT_NAMES
    assert "imagegen show_default_prompts" not in ROOT_NAMES
    assert "grok" not in ROOT_NAMES
    assert "palworld" not in ROOT_NAMES
    assert "play" not in ROOT_NAMES
    assert "status" not in ROOT_NAMES
    assert "join" not in ROOT_NAMES
    assert "count" not in ROOT_NAMES
    assert "stats" not in ROOT_NAMES
    assert "about" not in ROOT_NAMES
    assert "uptime" not in ROOT_NAMES
    assert "played" not in ROOT_NAMES
    assert is_owner_only_name("imagegen status") is False
    assert is_owner_only_name("reload_music") is True
    assert is_owner_only_name("imagegen generate") is False
    assert is_owner_only_name("status") is False
    assert is_owner_only_name("play") is False


def test_record_command_drops_owner_only_names(store):
    store.record_command(1, "reload_music", "prefix")
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
    assert store.apply_history_slash(1, 9003, WEEK, "reload_music") is True
    assert store.apply_history_slash(1, 9003, WEEK, "reload_music") is False
    assert store.apply_history_slash(
        1,
        9004,
        WEEK,
        "play",
        application_id=222,
        own_application_id=111,
        bot_name="Jockie Music",
    ) is True
    assert store.apply_history_slash(
        1,
        9004,
        WEEK,
        "play",
        application_id=222,
        own_application_id=111,
        bot_name="Jockie Music",
    ) is False
    foreign = store._conn.execute(
        """
        SELECT weekly, all_time, app_id, bot_name FROM command_counts
        WHERE guild_id = 1 AND command_name = 'play' AND kind = 'slash'
        """
    ).fetchone()
    assert (foreign["weekly"], foreign["all_time"], foreign["app_id"], foreign["bot_name"]) == (
        0,
        1,
        222,
        "Jockie Music",
    )
    after = store._conn.execute(
        "SELECT SUM(all_time) FROM command_counts WHERE guild_id = 1"
    ).fetchone()[0]
    assert after == before + 1
    assert store.apply_history_slash(1, 9005, WEEK, "queue", application_id="nope") is True
    missing = store._conn.execute(
        """
        SELECT app_id, all_time FROM command_counts
        WHERE guild_id = 1 AND command_name = 'queue' AND kind = 'slash'
        """
    ).fetchone()
    assert (missing["app_id"], missing["all_time"]) == (0, 1)
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
    own = 111
    assert slash_command_name(
        {"interaction": {"type": 2, "name": "imagegen", "application_id": own}},
        own,
    ) == "imagegen generate"
    assert slash_command_name(
        {"application_id": str(own), "interaction": {"type": 2, "name": "imagegen"}},
        own,
    ) == "imagegen generate"
    foreign_image = {"interaction": {"type": 2, "name": "imagegen", "application_id": 222}}
    assert slash_event_name(foreign_image, own) == "imagegen generate"
    assert classify_slash_use(foreign_image, own) == ("imagegen generate", "foreign")
    assert slash_command_name(foreign_image, own) is None
    missing_app = {"interaction": {"type": 2, "name": "imagegen"}}
    assert slash_event_name(missing_app, own) == "imagegen generate"
    assert slash_command_name(missing_app, own) is None
    mismatch = {
        "application_id": own,
        "interaction": {"type": 2, "name": "imagegen", "application_id": 222},
    }
    assert slash_event_name(mismatch, own) == "imagegen generate"
    assert slash_command_name(mismatch, own) is None
    assert normalize_slash_name("imagegen") == "imagegen generate"
    edit = slash_command_name(
        {
            "interaction_metadata": {
                "type": 2,
                "name": "imagegen",
                "application_id": own,
                "options": [{"type": 1, "name": "edit"}],
            }
        },
        own,
    )
    assert edit == "imagegen edit"
    assert slash_command_name(
        {"interaction": {"type": 5, "name": "imagegen", "application_id": own}},
        own,
    ) is None
    assert classify_slash_use(
        {"interaction": {"type": 2, "name": "play", "application_id": 222}},
        own,
    ) == ("play", "foreign")
    assert resolve_own_application_id(own) == own
    assert resolve_own_application_id("111") == 111
    for bad in ("", "0", "-5", "nope", "12.5"):
        with pytest.raises(ValueError):
            resolve_own_application_id(bad)


def test_model_from_caption_accepts_current_and_older_labels():
    assert model_from_caption("p — `krea2_turbo` — u") == "krea2_turbo"
    assert model_from_caption("p - flux3_klein_int8 model - u") == "flux3_klein_int8"
    assert model_from_caption("p \u2013 z_image_turbo_fp8 model \u2013 u") == "z_image_turbo_fp8"
    assert model_from_caption("p \u2014 anime_wai_anima model \u2014 u") == "anime_wai_anima"
    assert model_from_caption("p mentions krea2_turbo only") == "unknown"
    assert model_from_caption("") == "unknown"


def test_replace_all_time_imagegen_models_is_idempotent(store):
    store.record_imagegen(1, model="unknown", success=True)
    store.record_imagegen(1, model="krea2_turbo", success=True)
    store.flush()
    before = {
        (row["metric"], row["weekly"], row["all_time"])
        for row in store._conn.execute("SELECT metric, weekly, all_time FROM imagegen_totals WHERE guild_id = 1")
    }
    weekly_models = {
        row["item_key"]: row["weekly"]
        for row in store._conn.execute(
            "SELECT item_key, weekly FROM imagegen_counts WHERE guild_id = 1 AND dimension = 'model'"
        )
    }
    assert store.replace_all_time_imagegen_models(1, {"krea2_turbo": 4, "flux3_klein_int8": 2}) is True
    counts = {
        row["item_key"]: (row["weekly"], row["all_time"])
        for row in store._conn.execute(
            "SELECT item_key, weekly, all_time FROM imagegen_counts WHERE guild_id = 1 AND dimension = 'model'"
        )
    }
    assert counts["krea2_turbo"][1] == 4
    assert counts["flux3_klein_int8"] == (0, 2)
    assert counts["krea2_turbo"][0] == weekly_models["krea2_turbo"]
    assert "unknown" not in counts or counts["unknown"][1] == 0
    after = {
        (row["metric"], row["weekly"], row["all_time"])
        for row in store._conn.execute("SELECT metric, weekly, all_time FROM imagegen_totals WHERE guild_id = 1")
    }
    assert after == before
    assert store.replace_all_time_imagegen_models(1, {"krea2_turbo": 4, "flux3_klein_int8": 2}) is False


def _snowflake(moment: datetime) -> int:
    millis = int(moment.timestamp() * 1000)
    return (millis - 1_420_070_400_000) << 22


def test_imagegen_since_is_separate_from_live_since(store):
    store._conn.execute(
        "INSERT INTO history_applied (guild_id, source, message_id) VALUES (1, 'imagegen', ?)",
        (_snowflake(datetime(2025, 5, 17, 19, 0, tzinfo=timezone.utc)),),
    )
    stats = store.build_stats(1, WHEN)
    assert stats.imagegen_since == date(2025, 5, 17)
    assert stats.live_since == date(2026, 11, 19)
    view = prepare(stats, CardNames({}, {}, {}), "Guild", scope="all_time")
    assert view.imagegen_since == "May 17, 2025"
    assert view.tracking_since == "Nov 19, 2026"
    empty = prepare(store.build_stats(2, WHEN), CardNames({}, {}, {}), "Guild", scope="all_time")
    assert empty.imagegen_since == ""
    assert "Nov 19" not in empty.imagegen_since
    assert empty.tracking_since == "Nov 19, 2026"


def test_prefix_and_slash_forms_stay_separate(store):
    for _ in range(2):
        store.record_command(1, "play", "prefix")
    for _ in range(3):
        store.record_command(1, "play", "slash")
    store.record_command(1, "onlypre", "prefix")
    store.record_command(1, "onlyslash", "slash")
    store.flush()
    command_map = {"play": "play", "onlypre": "onlypre", "onlyslash": "onlyslash"}
    stats = store.build_stats(1, WHEN, command_map=command_map)
    weekly = {(row.name, row.kind): row for row in stats.top_commands}
    assert weekly[("play", "prefix")].weekly == 2
    assert weekly[("play", "prefix")].all_time == 2
    assert weekly[("play", "slash")].weekly == 3
    assert weekly[("play", "slash")].all_time == 3
    assert weekly[("onlypre", "prefix")].kind == "prefix"
    assert weekly[("onlyslash", "slash")].kind == "slash"
    assert "both" not in {row.kind for row in stats.top_commands}
    all_time = {(row.name, row.kind): row for row in stats.top_commands_all_time}
    assert all_time[("play", "prefix")].all_time == 2
    assert all_time[("play", "slash")].all_time == 3
    assert "both" not in {row.kind for row in stats.top_commands_all_time}
    assert weekly[("play", "prefix")].app_id == POOPER_APP_ID
    assert weekly[("play", "prefix")].bot_name == ""
    assert weekly[("play", "slash")].app_id == POOPER_APP_ID
    assert weekly[("play", "slash")].bot_name == ""
    assert _command_label(weekly[("play", "prefix")]) == "!play"
    assert _command_label(weekly[("play", "slash")]) == "/play"
    assert _command_label(weekly[("onlypre", "prefix")]) == "!onlypre"
    assert _command_label(weekly[("onlyslash", "slash")]) == "/onlyslash"
    assert "·" not in _command_label(weekly[("play", "prefix")])
    assert "·" not in _command_label(weekly[("play", "slash")])
    assert card_command_label("play", "prefix") == "!play"
    assert card_command_label("play", "slash") == "/play"
    assert card_command_label(weekly[("play", "prefix")]) == "!play"
    assert card_command_label(weekly[("play", "slash")]) == "/play"
    assert card_command_label("onlypre", "prefix") == "!onlypre"
    assert card_command_label("onlyslash", "slash") == "/onlyslash"
    assert card_command_label("play", "slash", 222, "Jockie Music") == "/play · Jockie Music"
    assert stats.commands_weekly == 7


def test_command_counts_migration_tags_prefix_and_leaves_slash_untagged(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE command_counts (
            guild_id INTEGER NOT NULL,
            command_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            weekly INTEGER NOT NULL DEFAULT 0,
            all_time INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, command_name, kind)
        )
        """
    )
    conn.execute(
        "INSERT INTO command_counts VALUES (1, 'play', 'prefix', 2, 975)"
    )
    conn.execute(
        "INSERT INTO command_counts VALUES (1, 'play', 'slash', 0, 460)"
    )
    conn.commit()
    conn.close()
    store = StatsStore(path)
    rows = {
        (row["command_name"], row["kind"]): (row["weekly"], row["all_time"], row["app_id"], row["bot_name"])
        for row in store._conn.execute(
            "SELECT command_name, kind, weekly, all_time, app_id, bot_name FROM command_counts"
        )
    }
    assert rows[("play", "prefix")] == (2, 975, POOPER_APP_ID, "")
    assert rows[("play", "slash")] == (0, 460, 0, "")
    store.close()
    again = StatsStore(path)
    second = {
        (row["command_name"], row["kind"]): (row["weekly"], row["all_time"], row["app_id"], row["bot_name"])
        for row in again._conn.execute(
            "SELECT command_name, kind, weekly, all_time, app_id, bot_name FROM command_counts"
        )
    }
    assert second == rows
    info = again._conn.execute("PRAGMA table_info(command_counts)").fetchall()
    assert sum(1 for row in info if row["name"] == "app_id") == 1
    again.close()


def test_top_commands_label_foreign_bots_and_least_used_stays_own(store):
    for _ in range(20):
        store.record_command(1, "play", "slash", app_id=222, bot_name="Jockie Music")
    for _ in range(10):
        store.record_command(1, "imagegen generate", "slash", app_id=POOPER_APP_ID)
    for _ in range(8):
        store.record_command(1, "play", "prefix")
    for _ in range(7):
        store.record_command(1, "skip", "prefix")
    for _ in range(6):
        store.record_command(1, "stop", "prefix")
    store.record_command(1, "quiet", "prefix")
    store.flush()
    command_map = {"play": "play", "quiet": "quiet", "skip": "skip", "stop": "stop", "zzzzunused": "zzzzunused"}
    stats = store.build_stats(1, WHEN, command_map=command_map)
    top = {(row.name, row.kind, row.app_id): row for row in stats.top_commands}
    assert ("play", "slash", 222) in top
    assert ("play", "prefix", POOPER_APP_ID) in top or any(
        row.name == "play" and row.kind == "prefix" for row in stats.top_commands
    )
    assert _command_label(top[("play", "slash", 222)]) == "/play · Jockie Music"
    own_slash = next(row for row in list(stats.top_commands) + list(stats.bottom_commands) if row.name == "imagegen generate")
    assert "·" not in _command_label(own_slash)
    own_prefix = next(row for row in list(stats.top_commands) + list(stats.bottom_commands) if row.name == "play" and row.kind == "prefix")
    assert _command_label(own_prefix) == "!play"
    least = list(stats.bottom_commands)
    assert all(row.app_id == POOPER_APP_ID or row.kind == "prefix" for row in least)
    assert all(row.app_id != 222 for row in least)
    assert all("Jockie" not in _command_label(row) for row in least)
    assert any(row.name == "quiet" for row in least)
    assert any(row.name == "zzzzunused" and row.all_time == 0 for row in least)


def test_tag_history_bots_is_idempotent(store):
    store.record_command(1, "play", "prefix")
    store.flush()
    store.close_week(1, WEEK, SUNDAY)
    assert store.apply_history_slash(1, 9101, WEEK, "play") is True
    assert store.apply_history_slash(1, 9102, WEEK, "play") is True
    assert store.apply_history_slash(1, 9103, WEEK, "imagegen generate") is True
    store._conn.execute(
        """
        CREATE TABLE IF NOT EXISTS slash_history_span (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            low_id INTEGER NOT NULL,
            high_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, channel_id, low_id)
        )
        """
    )
    store._conn.execute(
        "INSERT INTO slash_history_span (guild_id, channel_id, low_id, high_id) VALUES (1, 8, 1, 50)"
    )
    claims = store._conn.execute("SELECT COUNT(*) FROM history_applied").fetchone()[0]
    prefix = store._conn.execute(
        """
        SELECT weekly, all_time, app_id FROM command_counts
        WHERE guild_id = 1 AND command_name = 'play' AND kind = 'prefix'
        """
    ).fetchone()
    assert store.tag_history_bots(
        1,
        {"imagegen generate": 1},
        {("play", 222, "Jockie Music"): 2},
    ) is True
    play = store._conn.execute(
        """
        SELECT weekly, all_time, app_id, bot_name FROM command_counts
        WHERE guild_id = 1 AND command_name = 'play' AND kind = 'slash'
        """
    ).fetchall()
    assert len(play) == 1
    assert (play[0]["weekly"], play[0]["all_time"], play[0]["app_id"], play[0]["bot_name"]) == (
        0,
        2,
        222,
        "Jockie Music",
    )
    own = store._conn.execute(
        """
        SELECT weekly, all_time, app_id, bot_name FROM command_counts
        WHERE guild_id = 1 AND command_name = 'imagegen generate' AND kind = 'slash'
        """
    ).fetchone()
    assert (own["weekly"], own["all_time"], own["app_id"], own["bot_name"]) == (0, 1, POOPER_APP_ID, "")
    kept_prefix = store._conn.execute(
        """
        SELECT weekly, all_time, app_id FROM command_counts
        WHERE guild_id = 1 AND command_name = 'play' AND kind = 'prefix'
        """
    ).fetchone()
    assert (kept_prefix["weekly"], kept_prefix["all_time"], kept_prefix["app_id"]) == (
        prefix["weekly"],
        prefix["all_time"],
        prefix["app_id"],
    )
    zero = store._conn.execute(
        """
        SELECT COUNT(*) FROM command_counts
        WHERE guild_id = 1 AND kind = 'slash' AND app_id = 0
        """
    ).fetchone()[0]
    assert zero == 0
    assert store._conn.execute("SELECT COUNT(*) FROM history_applied").fetchone()[0] == claims
    assert store._conn.execute("SELECT COUNT(*) FROM slash_history_span").fetchone()[0] == 1
    snapshot = {
        (row["command_name"], row["kind"], row["app_id"], row["guild_id"]): (row["weekly"], row["all_time"], row["bot_name"])
        for row in store._conn.execute(
            "SELECT guild_id, command_name, kind, weekly, all_time, app_id, bot_name FROM command_counts"
        )
    }
    assert store.tag_history_bots(
        1,
        {"imagegen generate": 1},
        {("play", 222, "Jockie Music"): 2},
    ) is False
    after = {
        (row["command_name"], row["kind"], row["app_id"], row["guild_id"]): (row["weekly"], row["all_time"], row["bot_name"])
        for row in store._conn.execute(
            "SELECT guild_id, command_name, kind, weekly, all_time, app_id, bot_name FROM command_counts"
        )
    }
    assert after == snapshot
    assert store._conn.execute("SELECT COUNT(*) FROM history_applied").fetchone()[0] == claims


def test_retag_refuses_the_live_default_db(tmp_path):
    copy = tmp_path / "weekly_stats.sqlite3"
    copy.write_bytes(b"")
    assert _is_live_db(DEFAULT_DB) is True
    assert _is_live_db(copy) is False
    assert live_retag_refusal(DEFAULT_DB) == LIVE_RETAG_ERROR
    assert live_retag_refusal(copy) is None
    args = build_parser().parse_args(["--retag-bots", "--db", str(copy)])
    assert args.retag_bots is True
    alias = build_parser().parse_args(["--purge-foreign", "--db", str(copy)])
    assert alias.purge_foreign is True


def test_live_slash_guard_skips_a_different_application():
    interaction = type("Interaction", (), {"application_id": 222})()
    bot = type("Bot", (), {"application_id": 111, "client_id": "111"})()
    assert interaction_is_foreign(interaction, bot) is True
    same = type("Interaction", (), {"application_id": "111"})()
    assert interaction_is_foreign(same, bot) is False
    missing = type("Interaction", (), {"application_id": None})()
    assert interaction_is_foreign(missing, bot) is False
    client_only = type("Bot", (), {"application_id": None, "client_id": "111"})()
    assert interaction_is_foreign(interaction, client_only) is True
    unknown = type("Bot", (), {"application_id": None, "client_id": None})()
    assert interaction_is_foreign(interaction, unknown) is False

    bot = type("Bot", (), {"application_id": POOPER_APP_ID, "client_id": str(POOPER_APP_ID)})()
    metadata = SimpleNamespace(type=2, name="play", application_id=222)
    foreign_msg = SimpleNamespace(
        application_id=222,
        interaction=None,
        interaction_metadata=metadata,
        author=SimpleNamespace(bot=True, display_name="Jockie Music", global_name=None, name="Jockie Music"),
    )
    assert foreign_slash_from_message(foreign_msg, bot) == ("play", 222, "Jockie Music")
    own_msg = SimpleNamespace(
        application_id=POOPER_APP_ID,
        interaction=None,
        interaction_metadata=SimpleNamespace(type=2, name="imagegen generate", application_id=POOPER_APP_ID),
        author=SimpleNamespace(bot=True, display_name="PooperScooper", global_name=None, name="PooperScooper"),
    )
    assert foreign_slash_from_message(own_msg, bot) is None
    component = SimpleNamespace(
        application_id=222,
        interaction=None,
        interaction_metadata=SimpleNamespace(type=3, name="play", application_id=222),
        author=SimpleNamespace(bot=True, display_name="Jockie Music", global_name=None, name="Jockie Music"),
    )
    assert foreign_slash_from_message(component, bot) is None


def test_fold_imagegen_models_top_five_other_and_unknown():
    labels = [f"model-{index}" for index in range(7)]
    counts = [70, 60, 50, 40, 30, 20, 10]
    folded = fold_imagegen_models(list(zip(labels, counts)))
    assert folded == [
        ("model-0", 70),
        ("model-1", 60),
        ("model-2", 50),
        ("model-3", 40),
        ("model-4", 30),
        ("other", 30),
    ]
    in_top = fold_imagegen_models(
        [
            ("krea2 edit", 4),
            ("unknown", 100),
            ("flux", 3),
            ("klein", 2),
            ("anime", 1),
            ("extra", 1),
            ("tail", 1),
        ]
    )
    assert in_top[0] == ("unknown", 100)
    assert ("other", 2) in in_top
    assert [label for label, _count in in_top].count("unknown") == 1
    outside = fold_imagegen_models(
        [
            ("a", 50),
            ("b", 40),
            ("c", 30),
            ("d", 20),
            ("e", 10),
            ("f", 6),
            ("unknown", 1),
        ]
    )
    assert outside[-1] == ("unknown", 1)
    assert ("other", 6) in outside
    other_count = next(count for label, count in outside if label == "other")
    assert other_count == 6
    assert fold_imagegen_models([]) == []
    assert fold_imagegen_models(None) == []


def test_imagegen_summary_keeps_models_past_top_n(store):
    names = [f"model_{index}" for index in range(7)]
    for name in names:
        store.record_imagegen(1, model=name, success=True)
    store.flush()
    stats = store.build_stats(1, WHEN)
    assert len(stats.imagegen.models) == 7
    assert {row.key for row in stats.imagegen.models} == set(names)
    weekly = prepare(stats, CardNames({}, {}, {}), "Guild", scope="weekly")
    all_time = prepare(stats, CardNames({}, {}, {}), "Guild", scope="all_time")
    assert len(weekly.models) == 5
    assert len(all_time.models) == 7


def test_closed_card_keeps_live_all_time_models_when_payload_models_empty(store):
    for name in ("a", "b", "c", "d", "e", "f"):
        store.record_imagegen(1, model=name, success=True)
    store.flush()
    live = store.build_stats(1, WHEN)
    merged = _imagegen_from_payload({"models": []}, live.imagegen)
    assert len(merged.models) == 6
    assert all(row.weekly == 0 and row.all_time > 0 for row in merged.models)
    assert merged.generation_count_all_time == live.imagegen.generation_count_all_time
    assert merged.success_count_all_time == live.imagegen.success_count_all_time
    assert merged.error_count_all_time == live.imagegen.error_count_all_time
    applied = _apply_closed_card(live, {"imagegen": {"models": []}})
    assert {row.key for row in applied.imagegen.models} == {row.key for row in live.imagegen.models}
    assert all(row.all_time > 0 for row in applied.imagegen.models)
