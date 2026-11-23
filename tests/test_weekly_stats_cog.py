"""Weekly stats cog behavior without a Discord login."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import discord

from PooperScooper import initial_extensions
from cogs.imagediffusion import (
    ImageDiffusion,
    PromptDetailButton,
    _imagegen_stats_kwargs,
    _record_image_result,
    _record_stats,
)
from cogs.utils.config import WeeklyStatsConfig
from cogs.utils.image_models import ImageCreation, Models, workflow_sample_facts
from cogs.utils.stats_card import _sections, _week_label
from cogs.utils.stats_schedule import LA
from cogs.utils.stats_store import StatsStore
from cogs.weekly_stats import (
    DEFAULT_GUILDS,
    MTS_GUILD_ID,
    MTS_ILLUMINAUGHTY_CHANNEL_ID,
    MTS_REPORT_CHANNEL_ID,
    GuildStatsConfig,
    WeeklyStats,
    apply_config_overrides,
)

ENABLED = 4242
DISABLED = 1111
REPORT_CHANNEL_ID = 99
WHEN = datetime(2026, 11, 18, 15, 5, tzinfo=LA)
POST_AT = datetime(2026, 11, 15, 21, 0, tzinfo=LA)


class _Emoji:
    def __init__(self, name: str, emoji_id: int | None = None) -> None:
        self.name = name
        self.id = emoji_id

    def __str__(self) -> str:
        return self.name


def _guilds() -> dict[int, GuildStatsConfig]:
    return {
        ENABLED: GuildStatsConfig(enabled=True, report_channel_id=REPORT_CHANNEL_ID),
        DISABLED: GuildStatsConfig(enabled=False, report_channel_id=REPORT_CHANNEL_ID),
    }


def _cog(store: StatsStore, bot=None) -> WeeklyStats:
    return WeeklyStats(bot or SimpleNamespace(), store=store, guilds=_guilds())


def _message(guild_id, user_id, channel_id, *, bot=False, when=WHEN, channel=None):
    return SimpleNamespace(
        guild=None if guild_id is None else SimpleNamespace(id=guild_id),
        author=SimpleNamespace(id=user_id, bot=bot),
        channel=channel if channel is not None else SimpleNamespace(id=channel_id),
        created_at=when,
    )


class _ForumPost(discord.Thread):
    """Thread without a Discord connection. Forum posts use this type too."""

    def __init__(self, thread_id: int, parent_id: int) -> None:
        self.id = thread_id
        self.parent_id = parent_id


def _mentions_are_off(allowed) -> bool:
    return (
        allowed.everyone is False
        and allowed.users is False
        and allowed.roles is False
        and allowed.replied_user is False
    )


def test_extension_and_default_guild_config():
    assert "cogs.weekly_stats" in initial_extensions
    costume = initial_extensions.index("cogs.costume")
    assert initial_extensions[costume + 1] == "cogs.malfoyd"
    assert MTS_GUILD_ID == 932057681307512922
    default = DEFAULT_GUILDS[MTS_GUILD_ID]
    assert default.enabled is True
    assert default.report_channel_id == MTS_REPORT_CHANNEL_ID == 1045149015756521493
    assert MTS_ILLUMINAUGHTY_CHANNEL_ID == 1082770598981283880
    assert default.report_channel_id != MTS_ILLUMINAUGHTY_CHANNEL_ID
    assert not hasattr(default, "report_channel_name")
    assert default.weekday == 6
    assert default.hour == 21
    assert default.timezone == "America/Los_Angeles"
    assert "report_channel_name" not in inspect.getsource(WeeklyStats._report_channel)
    assert "text_channels" not in inspect.getsource(WeeklyStats._report_channel)

    merged = apply_config_overrides(
        DEFAULT_GUILDS,
        {"guilds": {str(MTS_GUILD_ID): {"enabled": False, "report_channel_id": 55}}},
    )
    assert merged[MTS_GUILD_ID].enabled is False
    assert merged[MTS_GUILD_ID].report_channel_id == 55
    assert not hasattr(merged[MTS_GUILD_ID], "report_channel_name")

    kept = apply_config_overrides(
        DEFAULT_GUILDS,
        {"guilds": {str(MTS_GUILD_ID): {"enabled": False, "report_channel_name": "nope"}}},
    )
    assert kept[MTS_GUILD_ID].enabled is False
    assert kept[MTS_GUILD_ID].report_channel_id == MTS_REPORT_CHANNEL_ID

    section = WeeklyStatsConfig.model_validate(
        {
            "guilds": {
                str(MTS_GUILD_ID): {
                    "enabled": False,
                    "hour": 20,
                    "report_channel_name": "illuminaughty",
                }
            }
        }
    )
    from_model = apply_config_overrides(DEFAULT_GUILDS, section)
    assert from_model[MTS_GUILD_ID].enabled is False
    assert from_model[MTS_GUILD_ID].hour == 20
    assert from_model[MTS_GUILD_ID].weekday == 6
    assert from_model[MTS_GUILD_ID].report_channel_id == MTS_REPORT_CHANNEL_ID

    cleared = WeeklyStatsConfig.model_validate(
        {"guilds": {str(MTS_GUILD_ID): {"report_channel_id": None}}}
    )
    assert apply_config_overrides(DEFAULT_GUILDS, cleared)[MTS_GUILD_ID].report_channel_id is None


def test_loops_listeners_and_commands_are_wired(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        cog = _cog(store)
        assert cog.flush_loop.seconds == 30
        assert cog.scheduler_loop.seconds == 60
        assert cog.flush_loop.is_running() is False
        assert cog.scheduler_loop.is_running() is False
        listener_names = {name for name, _callback in cog.get_listeners()}
        assert {
            "on_message",
            "on_raw_reaction_add",
            "on_voice_state_update",
            "on_app_command_completion",
            "on_command_completion",
        } <= listener_names
        assert cog.stats.name == "stats"
        assert cog.stats.guild_only is True
        assert {command.name for command in cog.stats.commands} == {"optout", "optin"}
        assert cog.stats_optout.guild_only is True
        assert cog.stats_optin.guild_only is True
        assert any("is_owner" in getattr(check, "__qualname__", "") for check in cog.statscard.checks)
        signature = inspect.signature(WeeklyStats.record_imagegen)
        assert "prompt" not in signature.parameters
        source = inspect.getsource(WeeklyStats._send_card)
        assert "AllowedMentions.none()" in source
    finally:
        store.close()


def test_tracking_ignores_disabled_guilds_dms_and_bots(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")

    async def scenario():
        cog = _cog(store)
        await cog.on_message(_message(DISABLED, 5, 10))
        await cog.on_message(_message(None, 5, 10))
        await cog.on_message(_message(ENABLED, 5, 10, bot=True))
        await cog.on_message(_message(ENABLED, 5, 10))
        await cog.on_raw_reaction_add(
            SimpleNamespace(
                guild_id=ENABLED,
                user_id=9,
                member=SimpleNamespace(id=9, bot=True),
                emoji=_Emoji("botface", 333),
            )
        )
        await cog.on_raw_reaction_add(
            SimpleNamespace(
                guild_id=DISABLED,
                user_id=5,
                member=SimpleNamespace(id=5, bot=False),
                emoji=_Emoji("nope"),
            )
        )
        await cog.on_raw_reaction_add(
            SimpleNamespace(
                guild_id=ENABLED,
                user_id=5,
                member=None,
                emoji=_Emoji("spark", 111),
            )
        )
        await cog.on_voice_state_update(
            SimpleNamespace(bot=True, id=5, guild=SimpleNamespace(id=ENABLED)),
            SimpleNamespace(channel=None),
            SimpleNamespace(channel=SimpleNamespace(id=70)),
        )
        assert cog._voice == {}
        await cog.on_app_command_completion(
            SimpleNamespace(guild_id=DISABLED, user=SimpleNamespace(id=5, bot=False)),
            SimpleNamespace(qualified_name="imagegen", name="imagegen"),
        )
        await cog.on_app_command_completion(
            SimpleNamespace(guild_id=ENABLED, user=SimpleNamespace(id=5, bot=False)),
            SimpleNamespace(qualified_name="imagegen pose", name="pose"),
        )
        await cog.on_command_completion(
            SimpleNamespace(
                guild=SimpleNamespace(id=ENABLED),
                author=SimpleNamespace(id=5, bot=True),
                command=SimpleNamespace(qualified_name="ignored"),
            )
        )
        await cog.on_command_completion(
            SimpleNamespace(
                guild=SimpleNamespace(id=ENABLED),
                author=SimpleNamespace(id=5, bot=False),
                command=SimpleNamespace(qualified_name="statscard"),
            )
        )
        cog.record_imagegen(DISABLED, model="secret-model", success=True)
        cog.record_imagegen(ENABLED, model="krea2_turbo", seconds=2.0, success=True)

    try:
        asyncio.run(scenario())
        stats = store.build_stats(ENABLED, WHEN)
        assert stats.messages_weekly == 1
        assert stats.reactions_weekly == 1
        assert stats.top_reactions[0].emoji_key == "111"
        assert {(row.name, row.kind) for row in stats.top_commands} == {
            ("imagegen pose", "slash"),
            ("statscard", "prefix"),
        }
        assert stats.imagegen.models[0].key == "krea2_turbo"
        assert store.build_stats(DISABLED, WHEN).messages_weekly == 0
        assert store.build_stats(DISABLED, WHEN).commands_weekly == 0
    finally:
        store.close()


def test_opt_out_and_opt_in_are_ephemeral_and_per_user(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")

    async def scenario():
        cog = _cog(store)
        await cog.on_message(_message(ENABLED, 5, 10))
        sent = {}

        async def send_message(content, *, ephemeral=False):
            sent["content"] = content
            sent["ephemeral"] = ephemeral

        interaction = SimpleNamespace(
            guild_id=ENABLED,
            user=SimpleNamespace(id=5, voice=None),
            response=SimpleNamespace(send_message=send_message),
        )
        await cog.stats_optout.callback(cog, interaction)
        assert sent["ephemeral"] is True
        assert sent["content"] == (
            "You're opted out in this server. You won't be counted as an active member from now on."
        )
        assert "counts" not in sent["content"].lower()
        assert "delete" not in sent["content"].lower()
        assert cog.stats_optout.description == "Stop counting you as an active member in this server."
        assert store.is_opted_out(ENABLED, 5) is True

        await cog.on_message(_message(ENABLED, 5, 10, when=WHEN + timedelta(seconds=5)))
        await cog.on_message(_message(ENABLED, 6, 10, when=WHEN))
        hidden = store.build_stats(ENABLED, WHEN)
        assert hidden.messages_weekly == 3
        assert hidden.active_members == 1
        hidden_ids = {
            row["user_id"]
            for row in store._conn.execute(
                "SELECT user_id FROM active_members WHERE guild_id = ?",
                (ENABLED,),
            )
        }
        assert hidden_ids == {6}

        await cog.stats_optin.callback(cog, interaction)
        assert sent["ephemeral"] is True
        assert "opted back in" in sent["content"].lower()
        assert store.is_opted_out(ENABLED, 5) is False
        await cog.on_message(_message(ENABLED, 5, 10, when=WHEN + timedelta(seconds=40)))
        resumed = store.build_stats(ENABLED, WHEN)
        assert resumed.active_members == 2
        resumed_ids = {
            row["user_id"]
            for row in store._conn.execute(
                "SELECT user_id FROM active_members WHERE guild_id = ?",
                (ENABLED,),
            )
        }
        assert resumed_ids == {5, 6}

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def _report_guild(send, channel_id=REPORT_CHANNEL_ID):
    """Card channel is resolved from the bot cache, not from this guild's names."""
    channel = SimpleNamespace(id=channel_id, name="test-channel", send=send)

    async def named_send(**_kwargs):
        raise AssertionError("report channel was resolved by name")

    decoy = SimpleNamespace(id=77, name="🎃illuminaughty", send=named_send)

    def bot_get_channel(requested):
        return channel if int(requested) == int(channel_id) else None

    def get_member(user_id):
        if user_id == 5:
            return SimpleNamespace(id=5, display_name="Ada", name="ada")
        return None

    guild = SimpleNamespace(
        id=ENABLED,
        name="Menaces to Sobriety",
        text_channels=[decoy],
        emojis=[SimpleNamespace(id=222, name="quiet")],
        get_channel=lambda _requested: None,
        get_member=get_member,
    )
    bot = SimpleNamespace(
        get_guild=lambda guild_id: guild if guild_id == ENABLED else None,
        get_channel=bot_get_channel,
    )
    return bot, channel


def _snapshots(store, guild_id):
    rows = store._conn.execute(
        """
        SELECT week_start, summary_json
        FROM weekly_snapshots
        WHERE guild_id = ?
        ORDER BY week_start
        """,
        (guild_id,),
    ).fetchall()
    return [(row["week_start"], json.loads(row["summary_json"])) for row in rows]


def test_post_failure_does_not_mark_the_week_posted(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")

    called = []

    async def boom(**_kwargs):
        called.append(True)
        raise RuntimeError("discord down")

    store.ensure_baseline(ENABLED, date(2026, 11, 2))

    async def scenario():
        bot, _channel = _report_guild(boom)
        cog = _cog(store, bot)
        await cog.on_message(_message(ENABLED, 5, 10, when=datetime(2026, 11, 12, 12, tzinfo=LA)))
        await cog.post_due_guilds(POST_AT)

    try:
        asyncio.run(scenario())
        assert called == [True]
        assert store.last_posted_week_start(ENABLED) == date(2026, 11, 2)
        stats = store.build_stats(ENABLED, POST_AT)
        assert stats.messages_weekly == 1
        assert stats.messages_all_time == 1
    finally:
        store.close()


def test_successful_post_uses_no_mentions_and_does_not_repeat(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)

    store.ensure_baseline(ENABLED, date(2026, 11, 2))

    async def scenario():
        bot, _channel = _report_guild(send)
        cog = _cog(store, bot)
        await cog.on_message(_message(ENABLED, 5, 10, when=datetime(2026, 11, 12, 12, tzinfo=LA)))
        await cog.post_due_guilds(POST_AT)
        await cog.post_due_guilds(POST_AT + timedelta(hours=1))
        ctx_sent = {}

        async def ctx_send(**kwargs):
            ctx_sent.update(kwargs)

        ctx = SimpleNamespace(guild=SimpleNamespace(id=ENABLED), send=ctx_send)
        await cog.statscard.callback(cog, ctx)
        return ctx_sent

    try:
        ctx_sent = asyncio.run(scenario())
        assert len(calls) == 1
        posted = calls[0]
        assert _mentions_are_off(posted["allowed_mentions"])
        assert [item.filename for item in posted["files"]] == ["weekly-stats.png", "alltime-stats.png"]
        assert "2026-11-09" in posted["content"]
        assert "all time" in posted["content"].lower()
        assert store.last_posted_week_start(ENABLED) == date(2026, 11, 9)
        assert _mentions_are_off(ctx_sent["allowed_mentions"])
        assert [item.filename for item in ctx_sent["files"]] == ["weekly-stats.png", "alltime-stats.png"]
    finally:
        store.close()


def test_missing_report_channel_skips_the_post(tmp_path, caplog):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    named_calls = []

    async def named_send(**_kwargs):
        named_calls.append(True)

    named = SimpleNamespace(id=77, name="🎃illuminaughty", send=named_send)
    guild = SimpleNamespace(
        id=ENABLED,
        name="Menaces",
        text_channels=[named],
        forums=[],
        emojis=[],
        get_channel=lambda _channel_id: None,
        get_member=lambda _user_id: None,
    )
    bot = SimpleNamespace(
        get_guild=lambda guild_id: guild if guild_id == ENABLED else None,
        get_channel=lambda _channel_id: None,
    )

    async def scenario():
        cog = _cog(store, bot)
        await cog.post_due_guilds(POST_AT)
        await cog.post_due_guilds(POST_AT + timedelta(minutes=1))
        return cog

    try:
        caplog.set_level(logging.WARNING, logger="cogs.weekly_stats")
        asyncio.run(scenario())
        # First sight only records the baseline. It does not warn or post.
        assert store.last_posted_week_start(ENABLED) == date(2026, 11, 9)
        assert "was not found" not in caplog.text
        assert named_calls == []

        store._conn.execute(
            "UPDATE guild_state SET last_posted_week_start = ? WHERE guild_id = ?",
            ("2026-11-02", ENABLED),
        )

        async def due_again():
            cog = _cog(store, bot)
            await cog.post_due_guilds(POST_AT)
            await cog.post_due_guilds(POST_AT + timedelta(minutes=1))
            assert store.last_posted_week_start(ENABLED) == date(2026, 11, 2)
            store._conn.execute(
                "UPDATE guild_state SET last_posted_week_start = ? WHERE guild_id = ?",
                ("2026-11-09", ENABLED),
            )
            next_sunday = datetime(2026, 11, 22, 21, 0, tzinfo=LA)
            await cog.post_due_guilds(next_sunday)
            await cog.post_due_guilds(next_sunday + timedelta(minutes=1))

        caplog.clear()
        asyncio.run(due_again())
        warnings = [record for record in caplog.records if "was not found" in record.getMessage()]
        assert len(warnings) == 2
        assert all(str(REPORT_CHANNEL_ID) in record.getMessage() for record in warnings)
        assert named_calls == []
        assert store.last_posted_week_start(ENABLED) == date(2026, 11, 9)
    finally:
        store.close()


def test_unload_flushes_without_closing_a_borrowed_store(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        cog = _cog(store)
        store.record_message(ENABLED, 10, 5, WHEN)
        assert store._conn.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0] == 0
        cog.cog_unload()
        assert store._conn.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0] == 1
    finally:
        store.close()


def test_imagegen_hook_is_optional_and_never_receives_the_prompt():
    _record_stats(SimpleNamespace(get_cog=lambda _name: None), "record_imagegen", 1, model="krea2_turbo")
    _record_stats(SimpleNamespace(), "record_imagegen", 1, model="krea2_turbo")

    class Broken:
        def record_imagegen(self, *_args, **_kwargs):
            raise RuntimeError("stats broke")

    _record_stats(SimpleNamespace(get_cog=lambda _name: Broken()), "record_imagegen", 1, model="krea2_turbo")

    image = ImageCreation("super secret prompt", Models.KREA2_TURBO, output_width=832, output_height=1216)
    kwargs = _imagegen_stats_kwargs(image, seconds=1.5, success=True)
    assert "prompt" not in kwargs
    assert all("super secret" not in str(value) for value in kwargs.values())
    assert kwargs["model"] == "krea2_turbo"
    assert kwargs["steps"] == 8
    assert kwargs["sampler"] == "euler"
    assert kwargs["loras"] == ()
    assert kwargs["width"] == 832
    assert kwargs["height"] == 1216
    edit = ImageCreation("super secret prompt", Models.KREA2_IDENTITY_EDIT)
    edit_kwargs = _imagegen_stats_kwargs(edit, seconds=1.5, success=True)
    assert edit_kwargs["edit_mode"] == "identity_edit"
    assert edit_kwargs["loras"] == ("krea2_identity_edit_v2",)
    assert edit_kwargs["steps"] == 8
    assert edit_kwargs["sampler"] == "euler"
    assert "super secret" not in str(edit_kwargs)
    import cogs.imagediffusion as image_cog

    assert not hasattr(image_cog, "_DOCUMENTED_SAMPLE")

    captured = {}

    class Recorder:
        def record_imagegen(self, guild_id, **called):
            captured["guild_id"] = guild_id
            captured["kwargs"] = called

    bot = SimpleNamespace(get_cog=lambda name: Recorder() if name == "WeeklyStats" else None)
    _record_image_result(bot, image, seconds=1.5, success=True)
    assert captured == {}
    image.interaction = SimpleNamespace(guild_id=ENABLED)
    _record_image_result(bot, image, seconds=1.5, success=True)
    assert captured["guild_id"] == ENABLED
    assert "prompt" not in captured["kwargs"]
    assert captured["kwargs"]["model"] == "krea2_turbo"

    captured.clear()
    background = ImageCreation("super secret prompt", Models.KREA2_TURBO, guild_id=ENABLED)
    assert background.interaction is None
    _record_image_result(bot, background, seconds=2.0, success=True)
    assert captured["guild_id"] == ENABLED
    assert "prompt" not in captured["kwargs"]
    assert "super secret" not in str(captured["kwargs"])
    captured.clear()
    missing = ImageCreation("super secret prompt", Models.KREA2_TURBO, interaction=SimpleNamespace(guild_id=None))
    _record_image_result(bot, missing, seconds=1.0, success=False)
    assert captured == {}
    zero = ImageCreation("super secret prompt", Models.KREA2_TURBO, guild_id=0)
    assert zero.guild_id is None
    _record_image_result(bot, zero, seconds=1.0, success=False)
    assert captured == {}
    background_source = inspect.getsource(ImageDiffusion._complete_background_generation)
    assert "_record_image_result" in background_source
    assert "success=True" in background_source
    assert "success=False" in background_source
    assert "prompt" not in background_source

    regenerate = inspect.getsource(PromptDetailButton.regenerate_button)
    edit = inspect.getsource(PromptDetailButton.edit_regenerate_button)
    generation = inspect.getsource(ImageDiffusion._image_generation_interaction)
    assert "record_imagegen_followup" in regenerate
    assert '"regenerate"' in regenerate
    assert "record_imagegen_followup" in edit
    assert '"edit"' in edit
    assert "_record_image_result" in generation
    silent = discord.AllowedMentions.none()
    assert silent.everyone is False
    assert silent.users is False
    assert silent.roles is False
    assert silent.replied_user is False


def test_workflow_sample_facts_come_from_the_graph():
    workflow = {
        "1": {
            "class_type": "KSampler",
            "inputs": {
                "steps": 8,
                "sampler_name": "euler",
                "positive": ["9", 0],
            },
        },
        "2": {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"lora_name": r"loras\detail_xl.safetensors", "strength_model": 1},
        },
        "3": {
            "class_type": "LoraLoader",
            "inputs": {"lora_name": "skip/me.safetensors"},
            "mode": 4,
        },
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "super secret prompt"},
        },
        "5": {
            "class_type": "KSamplerAdvanced",
            "inputs": {"steps": ["8", 0], "sampler_name": "euler"},
            "mode": 4,
        },
    }
    steps, sampler, loras = workflow_sample_facts(workflow)
    assert steps == 8
    assert sampler == "euler"
    assert loras == ("detail_xl",)
    assert "super secret" not in str((steps, sampler, loras))

    linked = {
        "1": {
            "class_type": "KSampler",
            "inputs": {"steps": ["9", 0], "sampler_name": "dpmpp_2m"},
        }
    }
    assert workflow_sample_facts(linked) == (None, "dpmpp_2m", ())

    conflict = {
        "1": {"class_type": "KSampler", "inputs": {"steps": 8, "sampler_name": "euler"}},
        "2": {"class_type": "KSamplerAdvanced", "inputs": {"steps": 16, "sampler_name": "euler"}},
    }
    assert workflow_sample_facts(conflict) == (None, "euler", ())

    fallback = {
        "1": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "uni_pc"}},
        "2": {"class_type": "BasicScheduler", "inputs": {"steps": 12, "scheduler": "simple"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "do not store this prompt"}},
    }
    assert workflow_sample_facts(fallback) == (12, "uni_pc", ())
    assert workflow_sample_facts({}) == (None, None, ())


def test_workflow_lookup_failure_does_not_break_or_leak_the_prompt(monkeypatch, caplog):
    def boom(_image):
        raise RuntimeError("super secret prompt")

    monkeypatch.setattr("cogs.imagediffusion.sample_facts_for_image", boom)
    image = ImageCreation("super secret prompt", Models.KREA2_TURBO)
    with caplog.at_level(logging.DEBUG, logger="cogs.imagediffusion"):
        kwargs = _imagegen_stats_kwargs(image, seconds=1, success=False)
    assert kwargs["steps"] is None
    assert kwargs["sampler"] is None
    assert kwargs["loras"] == ()
    assert "prompt" not in kwargs
    assert "super secret" not in str(kwargs)
    assert "super secret" not in caplog.text


def test_every_model_workflow_supplies_sampler_facts_without_the_prompt():
    for model in Models:
        kwargs = _imagegen_stats_kwargs(
            ImageCreation("super secret prompt", model),
            seconds=1,
            success=True,
        )
        assert isinstance(kwargs["steps"], int) and kwargs["steps"] > 0
        assert isinstance(kwargs["sampler"], str) and kwargs["sampler"]
        assert "super secret" not in str(kwargs)
        assert all("/" not in item and "\\" not in item and item for item in kwargs["loras"])


def test_first_deploy_on_wednesday_posts_the_following_sunday(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)

    wednesday = datetime(2026, 11, 18, 15, 5, tzinfo=LA)
    sunday = datetime(2026, 11, 22, 21, 0, tzinfo=LA)
    seen = {}

    async def scenario():
        bot, _channel = _report_guild(send)
        cog = _cog(store, bot)
        await cog.on_message(_message(ENABLED, 5, 10, when=wednesday))
        await cog.post_due_guilds(wednesday)
        seen["after_wednesday"] = (
            len(calls),
            store.last_posted_week_start(ENABLED),
            store.build_stats(ENABLED, wednesday).messages_weekly,
        )
        await cog.post_due_guilds(sunday - timedelta(minutes=1))
        seen["before_sunday"] = len(calls)
        await cog.post_due_guilds(sunday)

    try:
        asyncio.run(scenario())
        assert seen["after_wednesday"] == (0, date(2026, 11, 9), 1)
        assert seen["before_sunday"] == 0
        assert len(calls) == 1
        assert "2026-11-16" in calls[0]["content"]
        assert store.last_posted_week_start(ENABLED) == date(2026, 11, 16)
        posted = store.build_stats(ENABLED, sunday)
        assert posted.messages_weekly == 0
        assert posted.messages_all_time == 1
    finally:
        store.close()


def test_missed_sunday_still_catches_up_once_state_exists(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)

    wednesday = datetime(2026, 11, 18, 12, 0, tzinfo=LA)

    async def scenario():
        bot, _channel = _report_guild(send)
        cog = _cog(store, bot)
        await cog.on_message(_message(ENABLED, 5, 10, when=datetime(2026, 11, 12, 12, tzinfo=LA)))
        store.ensure_baseline(ENABLED, date(2026, 11, 2))
        await cog.post_due_guilds(wednesday)
        await cog.post_due_guilds(wednesday + timedelta(hours=1))

    try:
        asyncio.run(scenario())
        assert len(calls) == 1
        assert "2026-11-09" in calls[0]["content"]
        assert store.last_posted_week_start(ENABLED) == date(2026, 11, 9)
    finally:
        store.close()


def test_thread_messages_count_on_the_parent_channel(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")

    async def scenario():
        cog = _cog(store)
        await cog.on_message(_message(ENABLED, 5, 555, channel=_ForumPost(555, 10)))

    try:
        asyncio.run(scenario())
        stats = store.build_stats(ENABLED, WHEN, known_channel_ids=[10, 11])
        assert [row.channel_id for row in stats.top_channels] == [10]
        assert stats.top_channels[0].weekly == 1
        assert 555 not in [row.channel_id for row in stats.dead_channels]
        assert [row.channel_id for row in stats.dead_channels] == [11]
    finally:
        store.close()


def test_known_channel_ids_include_forums_and_skip_voice(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    try:
        cog = _cog(store)
        guild = SimpleNamespace(
            text_channels=[SimpleNamespace(id=1), SimpleNamespace(id=2)],
            forums=[SimpleNamespace(id=3)],
            voice_channels=[SimpleNamespace(id=4)],
            categories=[SimpleNamespace(id=5)],
        )
        assert cog._known_channel_ids(guild) == [1, 2, 3]
        assert cog._known_channel_ids(None) is None
    finally:
        store.close()


def test_card_post_does_not_resolve_member_names(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    captured = {}

    async def send(**kwargs):
        captured["sent"] = kwargs

    def get_member(user_id):
        raise AssertionError(f"resolved member {user_id}")

    channel = SimpleNamespace(id=99, name="illuminaughty", send=send)
    guild = SimpleNamespace(
        id=ENABLED,
        name="Menaces",
        text_channels=[channel],
        emojis=[],
        get_channel=lambda channel_id: channel if channel_id == 99 else None,
        get_member=get_member,
    )
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == ENABLED else None)

    async def scenario():
        cog = _cog(store, bot)
        for user_id in range(1, 13):
            await cog.on_message(_message(ENABLED, user_id, 10, when=datetime(2026, 11, 12, 12, tzinfo=LA)))
        store.ensure_baseline(ENABLED, date(2026, 11, 2))
        import cogs.weekly_stats as weekly_mod

        real_render = weekly_mod.render_stats_card

        def wrapped(stats, names, guild_name):
            captured["names"] = dict(names.users)
            captured["active"] = stats.active_members
            captured["sections"] = dict(_sections(stats, names))
            return real_render(stats, names, guild_name)

        weekly_mod.render_stats_card = wrapped
        try:
            await cog.post_due_guilds(POST_AT)
        finally:
            weekly_mod.render_stats_card = real_render

    try:
        asyncio.run(scenario())
        assert captured["names"] == {}
        assert captured["active"] == 12
        assert "Leaderboard" not in captured["sections"]
        rendered = "\n".join(line for lines in captured["sections"].values() for line in lines)
        assert "Member" not in rendered
        assert "<@" not in rendered
        card_source = inspect.getsource(WeeklyStats._card_names)
        assert "get_member" not in card_source
        assert "top_channels_all_time" in card_source
        assert "bottom_channels_all_time" in card_source
        assert "top_voice_channels_all_time" in card_source
        assert "leaderboard" not in card_source
        assert not hasattr(WeeklyStats, "_named_leaderboard")
    finally:
        store.close()


def test_card_names_cover_all_time_lists_and_guild_channels():
    stats = SimpleNamespace(
        top_channels=[SimpleNamespace(channel_id=1)],
        bottom_channels=[],
        dead_channels=[SimpleNamespace(channel_id=6)],
        top_voice_channels=[],
        top_channels_all_time=[SimpleNamespace(channel_id=2)],
        bottom_channels_all_time=[SimpleNamespace(channel_id=3)],
        top_voice_channels_all_time=[SimpleNamespace(channel_id=4)],
        top_reactions=[],
        least_custom_emojis=[],
    )

    def get_channel(channel_id):
        if channel_id == 1:
            return SimpleNamespace(id=1, name="weekly-room")
        if channel_id == 6:
            return None
        raise AssertionError(channel_id)

    guild = SimpleNamespace(
        text_channels=[SimpleNamespace(id=2, name="busy")],
        forums=[SimpleNamespace(id=3, name="forum-room")],
        forum_channels=[SimpleNamespace(id=5, name="other-forum")],
        voice_channels=[SimpleNamespace(id=4, name="voice-room")],
        get_channel=get_channel,
        get_member=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("member")),
    )
    names = WeeklyStats._card_names(SimpleNamespace(), guild, stats)
    assert names.channels[1] == "weekly-room"
    assert names.channels[2] == "busy"
    assert names.channels[3] == "forum-room"
    assert names.channels[4] == "voice-room"
    assert names.channels[5] == "other-forum"
    assert 6 not in names.channels


def test_voice_restarts_seed_open_sessions_and_unload_flushes_them(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    store.opt_out(ENABLED, 8)
    channel = SimpleNamespace(id=70)
    members = {
        5: SimpleNamespace(id=5, bot=False),
        8: SimpleNamespace(id=8, bot=False),
        9: SimpleNamespace(id=9, bot=True),
    }
    enabled = SimpleNamespace(
        id=ENABLED,
        _voice_states={
            5: SimpleNamespace(channel=channel, member=members[5]),
            8: SimpleNamespace(channel=channel, member=members[8]),
            9: SimpleNamespace(channel=channel, member=members[9]),
        },
        voice_channels=[SimpleNamespace(id=71, members=[SimpleNamespace(id=3, bot=False)])],
        get_member=members.get,
    )
    disabled = SimpleNamespace(
        id=DISABLED,
        voice_states={4: SimpleNamespace(channel=SimpleNamespace(id=80), member=SimpleNamespace(id=4, bot=False))},
        get_member=lambda user_id: SimpleNamespace(id=user_id, bot=False),
    )
    guilds = {ENABLED: enabled, DISABLED: disabled}

    async def ready():
        return None

    bot = SimpleNamespace(
        wait_until_ready=ready,
        get_guild=lambda guild_id: guilds.get(guild_id),
    )
    end = datetime(2026, 11, 18, 12, 0, tzinfo=LA)

    try:
        cog = _cog(store, bot)
        asyncio.run(cog.before_scheduler_loop())
        assert set(cog._voice) == {(ENABLED, 5), (ENABLED, 8)}
        assert cog._voice[(ENABLED, 5)][0] == 70
        joined = cog._voice[(ENABLED, 5)][1]
        cog._voice[(ENABLED, 5)] = (70, joined - timedelta(hours=1))
        asyncio.run(cog.before_scheduler_loop())
        assert cog._voice[(ENABLED, 5)][1] == joined - timedelta(hours=1)

        cog._now = lambda: end
        cog._voice[(ENABLED, 5)] = (70, end - timedelta(minutes=5))
        cog._voice[(ENABLED, 8)] = (70, end - timedelta(minutes=5))
        cog.cog_unload()
        stats = store.build_stats(ENABLED, end)
        assert stats.voice_minutes == 10
        assert stats.active_members == 0
        assert stats.top_voice_channels[0].channel_id == 70
        assert stats.top_voice_channels[0].weekly == 10
        assert store._conn.execute(
            "SELECT COUNT(*) FROM active_members WHERE guild_id = ? AND user_id = ?",
            (ENABLED, 8),
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_voice_seed_falls_back_to_channel_members(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    human = SimpleNamespace(id=5, bot=False)
    bot_member = SimpleNamespace(id=9, bot=True)
    guild = SimpleNamespace(
        id=ENABLED,
        voice_channels=[SimpleNamespace(id=70, members=[human, bot_member])],
        stage_channels=[SimpleNamespace(id=71, members=[SimpleNamespace(id=6, bot=False)])],
        get_member=lambda user_id: None,
    )
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == ENABLED else None)
    start = datetime(2026, 11, 18, 11, 0, tzinfo=LA)
    try:
        cog = _cog(store, bot)
        cog._seed_open_voice(now=start)
        assert set(cog._voice) == {(ENABLED, 5), (ENABLED, 6)}
        assert cog._voice[(ENABLED, 5)] == (70, start)
        assert cog._voice[(ENABLED, 6)] == (71, start)
    finally:
        store.close()


def test_thursday_deploy_posts_this_sunday_and_not_before(tmp_path):
    """Deploy Thu Nov 19 2026 10:00 PT. First card is Sun Nov 22 21:00 for week Nov 16."""
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    calls = []
    rendered = []

    async def send(**kwargs):
        calls.append(kwargs)

    deploy = datetime(2026, 11, 19, 10, 0, tzinfo=LA)
    friday = datetime(2026, 11, 20, 18, 0, tzinfo=LA)
    before_sunday = datetime(2026, 11, 22, 20, 59, tzinfo=LA)
    sunday = datetime(2026, 11, 22, 21, 0, tzinfo=LA)
    just_after = datetime(2026, 11, 22, 21, 1, tzinfo=LA)
    next_before = datetime(2026, 11, 29, 20, 59, tzinfo=LA)
    next_sunday = datetime(2026, 11, 29, 21, 0, tzinfo=LA)
    seen = {}

    async def scenario():
        import cogs.weekly_stats as weekly_mod

        bot, _channel = _report_guild(send)
        cog = _cog(store, bot)
        real_render = weekly_mod.render_stats_card

        def wrapped(stats, names, guild_name):
            rendered.append((stats, guild_name, _week_label(stats)))
            return real_render(stats, names, guild_name)

        weekly_mod.render_stats_card = wrapped
        try:
            await cog.on_message(_message(ENABLED, 5, 10, when=deploy))
            await cog.post_due_guilds(deploy)
            seen["after_deploy"] = (len(calls), store.last_posted_week_start(ENABLED))
            await cog.on_message(_message(ENABLED, 5, 10, when=friday))
            await cog.post_due_guilds(before_sunday)
            seen["before_sunday"] = (len(calls), store.last_posted_week_start(ENABLED))
            await cog.post_due_guilds(sunday)
            seen["sunday"] = (len(calls), store.last_posted_week_start(ENABLED))
            await cog.post_due_guilds(just_after)
            seen["just_after"] = (len(calls), store.last_posted_week_start(ENABLED))
            await cog.on_message(_message(ENABLED, 6, 10, when=datetime(2026, 11, 23, 12, tzinfo=LA)))
            await cog.post_due_guilds(next_before)
            seen["next_before"] = (len(calls), store.last_posted_week_start(ENABLED))
            await cog.post_due_guilds(next_sunday)
            await cog.post_due_guilds(next_sunday + timedelta(minutes=1))
        finally:
            weekly_mod.render_stats_card = real_render

    try:
        asyncio.run(scenario())
        assert seen["after_deploy"] == (0, date(2026, 11, 9))
        assert seen["before_sunday"] == (0, date(2026, 11, 9))
        assert seen["sunday"] == (1, date(2026, 11, 16))
        assert seen["just_after"] == (1, date(2026, 11, 16))
        assert seen["next_before"] == (1, date(2026, 11, 16))
        assert len(calls) == 2
        assert store.last_posted_week_start(ENABLED) == date(2026, 11, 23)

        first_stats, first_guild, first_label = rendered[0]
        assert first_guild == "Menaces to Sobriety"
        assert first_stats.week_start == date(2026, 11, 16)
        assert first_stats.week_end == date(2026, 11, 22)
        assert first_label == "Nov 16 - Nov 22, 2026"
        assert first_stats.messages_weekly == 2
        assert first_stats.messages_last_week is None
        assert first_stats.active_members_last_week is None
        first_post = calls[0]
        assert first_post["content"] == "This week and all time for Menaces to Sobriety · week of 2026-11-16"
        assert "2026-11-09" not in first_post["content"]
        assert "full week" not in first_post["content"].lower()
        assert _mentions_are_off(first_post["allowed_mentions"])

        second_stats, _second_guild, second_label = rendered[1]
        assert second_stats.week_start == date(2026, 11, 23)
        assert second_label == "Nov 23 - Nov 29, 2026"
        assert second_stats.messages_weekly == 1
        assert calls[1]["content"] == "This week and all time for Menaces to Sobriety · week of 2026-11-23"

        snapshots = _snapshots(store, ENABLED)
        assert [week for week, _payload in snapshots] == ["2026-11-16", "2026-11-23"]
        assert snapshots[0][1]["messages"] == 2
        assert snapshots[1][1]["messages"] == 1
        closed = store.build_stats(ENABLED, next_sunday)
        assert closed.messages_weekly == 0
        assert closed.messages_all_time == 3
    finally:
        store.close()


def test_restart_before_sunday_keeps_the_baseline_and_still_posts(tmp_path):
    path = tmp_path / "weekly_stats.sqlite3"
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)

    deploy = datetime(2026, 11, 19, 10, 0, tzinfo=LA)
    before_sunday = datetime(2026, 11, 22, 20, 59, tzinfo=LA)
    sunday = datetime(2026, 11, 22, 21, 0, tzinfo=LA)

    store = StatsStore(path)
    try:
        async def boot():
            bot, _channel = _report_guild(send)
            cog = _cog(store, bot)
            await cog.on_message(_message(ENABLED, 5, 10, when=deploy))
            await cog.post_due_guilds(deploy)

        asyncio.run(boot())
        assert calls == []
        assert store.last_posted_week_start(ENABLED) == date(2026, 11, 9)
    finally:
        store.close()

    restarted = StatsStore(path)
    try:
        async def again():
            bot, _channel = _report_guild(send)
            cog = _cog(restarted, bot)
            await cog.post_due_guilds(deploy + timedelta(hours=5))
            await cog.post_due_guilds(before_sunday)
            assert calls == []
            assert restarted.last_posted_week_start(ENABLED) == date(2026, 11, 9)
            await cog.post_due_guilds(sunday)
            await cog.post_due_guilds(sunday + timedelta(minutes=1))

        asyncio.run(again())
        assert len(calls) == 1
        assert "2026-11-16" in calls[0]["content"]
        assert "2026-11-09" not in calls[0]["content"]
        assert restarted.last_posted_week_start(ENABLED) == date(2026, 11, 16)
        snapshots = _snapshots(restarted, ENABLED)
        assert [week for week, _payload in snapshots] == ["2026-11-16"]
        assert snapshots[0][1]["messages"] == 1
        fresh = restarted.build_stats(ENABLED, sunday)
        assert fresh.messages_weekly == 0
        assert fresh.messages_all_time == 1
    finally:
        restarted.close()


def test_report_channel_id_resolves_outside_the_stats_guild(tmp_path):
    store = StatsStore(tmp_path / "weekly_stats.sqlite3")
    calls = []
    named_calls = []

    async def send(**kwargs):
        calls.append(kwargs)

    async def named_send(**_kwargs):
        named_calls.append(True)

    named = SimpleNamespace(id=77, name="🎃illuminaughty", send=named_send)
    report = SimpleNamespace(id=MTS_REPORT_CHANNEL_ID, name="test-channel", send=send)
    guild = SimpleNamespace(
        id=MTS_GUILD_ID,
        name="Menaces to Sobriety",
        text_channels=[named],
        emojis=[],
        get_channel=lambda _channel_id: None,
        get_member=lambda _user_id: None,
    )
    bot = SimpleNamespace(
        get_guild=lambda guild_id: guild if guild_id == MTS_GUILD_ID else None,
        get_channel=lambda channel_id: report if channel_id == MTS_REPORT_CHANNEL_ID else None,
    )
    guilds = {
        MTS_GUILD_ID: GuildStatsConfig(enabled=True, report_channel_id=MTS_REPORT_CHANNEL_ID),
    }

    async def scenario():
        cog = WeeklyStats(bot, store=store, guilds=guilds)
        await cog.on_message(_message(MTS_GUILD_ID, 5, 10, when=datetime(2026, 11, 12, 12, tzinfo=LA)))
        store.ensure_baseline(MTS_GUILD_ID, date(2026, 11, 2))
        await cog.post_due_guilds(POST_AT)

    try:
        asyncio.run(scenario())
        assert named_calls == []
        assert len(calls) == 1
        assert calls[0]["content"] == "This week and all time for Menaces to Sobriety · week of 2026-11-09"
        assert store.last_posted_week_start(MTS_GUILD_ID) == date(2026, 11, 9)
        posted = store.build_stats(MTS_GUILD_ID, POST_AT)
        assert posted.messages_all_time == 1
        assert posted.guild_id == MTS_GUILD_ID
        assert MTS_ILLUMINAUGHTY_CHANNEL_ID != MTS_REPORT_CHANNEL_ID
    finally:
        store.close()
