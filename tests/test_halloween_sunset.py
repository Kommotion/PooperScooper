"""Halloween sunset switch: cog loading, /halloweenify, and stale command sync. No network."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from PooperScooper import (
    PooperScooper,
    extensions_to_load,
    initial_extensions,
    sync_retired_halloween_commands,
)
from cogs.candyhunt import CANDY_GUILD_IDS
from cogs.imagediffusion import ImageDiffusion, app_commands_for_tree, halloweenify_enabled_from_config
from cogs.utils.config import BotConfig

ROOT = Path(__file__).resolve().parents[1]
_HALLOWEEN_COGS = frozenset({"cogs.candyhunt", "cogs.costume"})


def _cfg(**extra) -> BotConfig:
    data = {"token": "test-token", "client_id": "123456789012345678"}
    data.update(extra)
    return BotConfig.model_validate(data)


def _off_names() -> list[str]:
    return [name for name in initial_extensions if name not in _HALLOWEEN_COGS]


class RecordingTree:
    """Stand-in command tree. Records syncs and refuses a wipe or a global copy."""

    def __init__(self, *, fail_guilds: set[int] | None = None):
        self.guild_ids: list[int] = []
        self.global_syncs = 0
        self.cleared: list = []
        self.copied: list[int] = []
        self.fail_guilds = set(fail_guilds or ())
        self.guild_commands = {
            gid: [SimpleNamespace(name="copied")] for gid in CANDY_GUILD_IDS
        }
        self.global_commands = [SimpleNamespace(name="imagegen")]

    async def sync(self, *, guild=None):
        if guild is None:
            self.global_syncs += 1
            return list(self.global_commands)
        gid = int(guild.id)
        self.guild_ids.append(gid)
        if gid in self.fail_guilds:
            raise RuntimeError("sync failed")
        return list(self.guild_commands.get(gid, []))

    def clear_commands(self, guild=None):
        self.cleared.append(None if guild is None else int(getattr(guild, "id", 0)))
        raise AssertionError("clear_commands")

    def copy_global_to(self, *, guild):
        self.copied.append(int(guild.id))
        raise AssertionError("copy_global_to")


def _bot(*, fail_guilds: set[int] | None = None) -> tuple[PooperScooper, RecordingTree]:
    bot = PooperScooper(client_id="123456789012345678")
    bot.application_info = AsyncMock(
        return_value=SimpleNamespace(team=None, owner=SimpleNamespace(id=7))
    )
    bot.load_extension = AsyncMock()
    # ``tree`` is a read-only property. Swap the sync path on the live tree.
    recorder = RecordingTree(fail_guilds=fail_guilds)
    bot.tree.sync = recorder.sync
    bot.tree.clear_commands = recorder.clear_commands
    bot.tree.copy_global_to = recorder.copy_global_to
    return bot, recorder


def _patch_config(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(
        "PooperScooper.load_config",
        lambda *args, **kwargs: SimpleNamespace(halloween_enabled=enabled),
    )


def _apply_halloweenify(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(
        "cogs.imagediffusion.load_config",
        lambda *args, **kwargs: SimpleNamespace(halloween_enabled=enabled),
    )
    value = halloweenify_enabled_from_config()
    assert value is enabled
    monkeypatch.setattr("cogs.imagediffusion.HALLOWEENIFY_ENABLED", value)


def _command_tree():
    connection = SimpleNamespace(_command_tree=None)
    client = SimpleNamespace(http=SimpleNamespace(), _connection=connection)
    return discord.app_commands.CommandTree(client)


def test_missing_key_leaves_halloween_cogs_unloaded():
    loaded = extensions_to_load(_cfg())
    assert _cfg().halloween_enabled is False
    assert "cogs.candyhunt" not in loaded
    assert "cogs.costume" not in loaded
    assert "cogs.malfoyd" in loaded
    assert loaded == _off_names()
    assert extensions_to_load(None) == _off_names()
    assert extensions_to_load(SimpleNamespace()) == _off_names()


def test_explicit_off_and_on_extension_lists():
    assert extensions_to_load(_cfg(halloween_enabled=False)) == _off_names()
    assert extensions_to_load(_cfg(halloween_enabled=True)) == list(initial_extensions)
    assert "cogs.candyhunt" in initial_extensions
    assert "cogs.costume" in initial_extensions


def test_switch_off_filters_halloweenify(monkeypatch):
    _apply_halloweenify(monkeypatch, False)
    kept = app_commands_for_tree([ImageDiffusion.imagegen_group, ImageDiffusion.halloweenify])
    assert [command.name for command in kept] == ["imagegen"]

    async def run():
        holder = SimpleNamespace(
            __cog_app_commands__=[
                SimpleNamespace(name="imagegen"),
                SimpleNamespace(name="halloweenify"),
            ]
        )
        await ImageDiffusion.cog_load(holder)
        assert [command.name for command in holder.__cog_app_commands__] == ["imagegen"]

    asyncio.run(run())


def test_switch_on_enables_halloweenify(monkeypatch):
    _apply_halloweenify(monkeypatch, True)
    kept = app_commands_for_tree([ImageDiffusion.imagegen_group, ImageDiffusion.halloweenify])
    assert [command.name for command in kept] == ["imagegen", "halloweenify"]

    async def run():
        holder = SimpleNamespace(
            __cog_app_commands__=[
                SimpleNamespace(name="imagegen"),
                SimpleNamespace(name="halloweenify"),
            ]
        )
        await ImageDiffusion.cog_load(holder)
        assert [command.name for command in holder.__cog_app_commands__] == [
            "imagegen",
            "halloweenify",
        ]

    asyncio.run(run())


def test_halloweenify_config_error_hides_the_command(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("config unreadable")

    monkeypatch.setattr("cogs.imagediffusion.load_config", boom)
    assert halloweenify_enabled_from_config() is False
    monkeypatch.setattr("cogs.imagediffusion.HALLOWEENIFY_ENABLED", False)
    assert app_commands_for_tree([ImageDiffusion.halloweenify]) == []


def test_setup_hook_off_skips_cogs_and_syncs_each_candy_guild_once(monkeypatch):
    _patch_config(monkeypatch, False)
    bot, tree = _bot()

    async def run():
        await bot.setup_hook()
        loaded = [call.args[0] for call in bot.load_extension.await_args_list]
        assert loaded == _off_names()
        assert "cogs.candyhunt" not in loaded
        assert "cogs.costume" not in loaded
        assert "cogs.malfoyd" in loaded
        assert tree.guild_ids == list(CANDY_GUILD_IDS)
        assert tree.global_syncs == 1
        assert tree.cleared == []
        assert tree.copied == []
        assert tree.guild_commands[CANDY_GUILD_IDS[0]][0].name == "copied"
        assert tree.global_commands[0].name == "imagegen"

        await bot.setup_hook()
        assert tree.guild_ids == list(CANDY_GUILD_IDS)
        assert tree.global_syncs == 1
        assert bot._halloween_sunset_synced is True

    asyncio.run(run())


def test_setup_hook_on_loads_halloween_cogs_and_does_not_sunset_sync(monkeypatch):
    _patch_config(monkeypatch, True)
    bot, tree = _bot()

    async def run():
        await bot.setup_hook()
        loaded = [call.args[0] for call in bot.load_extension.await_args_list]
        assert loaded == list(initial_extensions)
        assert tree.guild_ids == []
        assert tree.global_syncs == 0
        assert tree.cleared == []
        assert tree.copied == []
        assert bot._halloween_sunset_synced is False

    asyncio.run(run())


def test_unreadable_config_stays_off_and_still_syncs(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("PooperScooper.load_config", boom)
    bot, tree = _bot(fail_guilds={CANDY_GUILD_IDS[0]})

    async def run():
        with caplog.at_level(logging.ERROR, logger="PooperScooper"):
            await bot.setup_hook()
        loaded = [call.args[0] for call in bot.load_extension.await_args_list]
        assert loaded == _off_names()
        assert tree.guild_ids == list(CANDY_GUILD_IDS)
        assert tree.global_syncs == 1
        assert tree.cleared == []
        assert any(
            f"halloween command sync failed guild={CANDY_GUILD_IDS[0]}" in record.message
            for record in caplog.records
        )

    asyncio.run(run())


def test_stale_cleanup_preserves_tree_commands_and_does_not_clear_global():
    async def run():
        tree = _command_tree()

        @discord.app_commands.command(name="copied", description="guild command that must stay")
        async def copied(interaction: discord.Interaction) -> None:
            return None

        @discord.app_commands.command(name="imagegen", description="global command that must stay")
        async def imagegen(interaction: discord.Interaction) -> None:
            return None

        guilds = [discord.Object(id=gid) for gid in CANDY_GUILD_IDS]
        tree.add_command(copied, guild=guilds[0])
        tree.add_command(imagegen)
        calls: list[int | None] = []

        async def fake_sync(*, guild=None):
            calls.append(None if guild is None else int(guild.id))
            if guild is None:
                return list(tree.get_commands())
            return list(tree.get_commands(guild=guild))

        def forbid_clear(*args, **kwargs):
            raise AssertionError("clear_commands")

        def forbid_copy(*args, **kwargs):
            raise AssertionError("copy_global_to")

        tree.sync = fake_sync
        tree.clear_commands = forbid_clear
        tree.copy_global_to = forbid_copy

        await sync_retired_halloween_commands(SimpleNamespace(tree=tree))

        assert calls == [*CANDY_GUILD_IDS, None]
        assert tree.get_command("imagegen") is not None
        assert tree.get_command("imagegen").name == "imagegen"
        kept = [command.name for command in tree.get_commands(guild=guilds[0])]
        assert kept == ["copied"]
        assert tree.get_commands(guild=guilds[1]) == []
        assert tree.get_command("candy", guild=guilds[0]) is None
        assert tree.get_command("halloweenify") is None

    asyncio.run(run())


def test_missing_tree_does_not_raise():
    async def run():
        await sync_retired_halloween_commands(SimpleNamespace(tree=None))
        await sync_retired_halloween_commands(SimpleNamespace(tree=SimpleNamespace()))

    asyncio.run(run())


def test_example_config_and_next_year_note():
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["halloween_enabled"] is False
    readme = (ROOT / "docs" / "README_candyhunt.md").read_text(encoding="utf-8")
    assert "Next year: set halloween_enabled to true in config.json and restart." in readme
    server_docs = (ROOT / "docs" / "SERVER_CONFIGS.md").read_text(encoding="utf-8")
    assert "halloween_enabled" in server_docs
