"""Halloween sunset switch: cog loading and /halloweenify. No network, no command sync."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PooperScooper import PooperScooper, extensions_to_load, initial_extensions
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


def _bot() -> tuple[PooperScooper, AsyncMock]:
    bot = PooperScooper(client_id="123456789012345678")
    bot.application_info = AsyncMock(
        return_value=SimpleNamespace(team=None, owner=SimpleNamespace(id=7))
    )
    bot.load_extension = AsyncMock()
    # ``tree`` is a read-only property. Swap the sync path on the live tree.
    sync = AsyncMock(return_value=[])
    bot.tree.sync = sync
    return bot, sync


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


def test_setup_hook_off_skips_halloween_cogs(monkeypatch):
    _patch_config(monkeypatch, False)
    bot, _sync = _bot()

    async def run():
        await bot.setup_hook()
        loaded = [call.args[0] for call in bot.load_extension.await_args_list]
        assert loaded == _off_names()
        assert "cogs.candyhunt" not in loaded
        assert "cogs.costume" not in loaded
        assert "cogs.malfoyd" in loaded

    asyncio.run(run())


def test_setup_hook_on_loads_halloween_cogs(monkeypatch):
    _patch_config(monkeypatch, True)
    bot, _sync = _bot()

    async def run():
        await bot.setup_hook()
        loaded = [call.args[0] for call in bot.load_extension.await_args_list]
        assert loaded == list(initial_extensions)
        assert "cogs.candyhunt" in loaded
        assert "cogs.costume" in loaded

    asyncio.run(run())


def test_unreadable_config_stays_off(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("PooperScooper.load_config", boom)
    bot, _sync = _bot()

    async def run():
        with caplog.at_level(logging.ERROR, logger="PooperScooper"):
            await bot.setup_hook()
        loaded = [call.args[0] for call in bot.load_extension.await_args_list]
        assert loaded == _off_names()
        assert "cogs.candyhunt" not in loaded
        assert "cogs.costume" not in loaded
        assert any(
            "Could not read config while choosing extensions" in record.message
            for record in caplog.records
        )

    asyncio.run(run())


def test_setup_hook_does_not_call_tree_sync(monkeypatch):
    """Startup never publishes app commands. The owner syncs those by hand."""

    async def run():
        for enabled in (False, True):
            _patch_config(monkeypatch, enabled)
            bot, sync = _bot()
            await bot.setup_hook()
            sync.assert_not_called()
            assert not hasattr(bot, "_halloween_sunset_synced")
            assert not hasattr(bot, "_retire_halloween_commands_once")

        def boom(*args, **kwargs):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("PooperScooper.load_config", boom)
        bot, sync = _bot()
        await bot.setup_hook()
        sync.assert_not_called()

    asyncio.run(run())


def test_example_config_and_next_year_note():
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["halloween_enabled"] is False
    readme = (ROOT / "docs" / "README_candyhunt.md").read_text(encoding="utf-8")
    assert "Next year: set halloween_enabled to true in config.json and restart." in readme
    assert (
        'After turning Halloween off, the owner syncs manually with "!sync_commands ~" '
        'in each candy guild and "!sync_commands" for global.'
    ) in readme
    server_docs = (ROOT / "docs" / "SERVER_CONFIGS.md").read_text(encoding="utf-8")
    assert "halloween_enabled" in server_docs
