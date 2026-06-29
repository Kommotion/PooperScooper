"""Lightweight smoke tests for infrastructure changes."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from cogs.utils.config import BotConfig, LavalinkConfig, PalworldConfig, _apply_env_overrides
from cogs.utils.json_store import LockedJsonFile, migrate_legacy_data_files


def test_lavalink_config_defaults():
    config = LavalinkConfig()
    assert config.port == 2333
    assert config.enabled is True


def test_bot_config_validation():
    config = BotConfig.model_validate({
        "token": "test-token",
        "client_id": "123456789012345678",
    })
    assert config.token.get_secret_value() == "test-token"
    assert config.lavalink.host == "localhost"


def test_env_overrides_take_precedence(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "env-token")
    monkeypatch.setenv("LAVALINK_PORT", "2400")
    merged = _apply_env_overrides({"token": "file-token", "lavalink": {"port": 2333}})
    assert merged["token"] == "env-token"
    assert merged["lavalink"]["port"] == 2400


def test_palworld_config_defaults():
    config = PalworldConfig()
    assert config.automatic_restart_every_x_hours == 6


def test_locked_json_file_async_roundtrip():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.json"
            store = LockedJsonFile(path, default=dict, indent=2)
            await store.awrite({"hello": "world"})
            data = await store.aread()
            assert data == {"hello": "world"}

            result = await store.aupdate(lambda current: {**current, "count": 1})
            assert result["count"] == 1

    asyncio.run(run())


def test_migrate_legacy_data_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        legacy = root / "gametime.json"
        legacy.write_text(json.dumps({"1": {"Game": 60}}), encoding="utf-8")
        target = root / "data" / "gametime.json"

        migrate_legacy_data_files(root, {"gametime.json": target})

        assert not legacy.exists()
        assert target.is_file()
        assert json.loads(target.read_text(encoding="utf-8")) == {"1": {"Game": 60}}