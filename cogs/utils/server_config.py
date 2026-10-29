"""Load per-guild configuration from server_configs/<guild_id>.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parents[2]
SERVER_CONFIGS_DIR = _BASE_DIR / "server_configs"
DEFAULT_BIRTHDAY_TIMEZONE = "America/Chicago"


class ServerConfig:
    """Guild-scoped settings loaded from server_configs/<guild_id>.json."""

    def __init__(self, guild_id: int, data: dict[str, Any]):
        self.guild_id = guild_id
        self._data = data

    @property
    def enabled(self) -> bool:
        return self._data.get("enabled", True)

    @property
    def guild_name(self) -> str:
        return str(self._data.get("guild_name", f"Guild {self.guild_id}"))

    @property
    def image_diffusion_channel_ids(self) -> list[int]:
        channels = self._data.get("image_diffusion", {}).get("allowed_channel_ids", [])
        return [int(c) for c in channels]

    @property
    def image_diffusion_moderator_role_id(self) -> Optional[int]:
        role_id = self._data.get("image_diffusion", {}).get("moderator_role_id")
        return int(role_id) if role_id is not None else None

    @property
    def reaction_roles_channel_id(self) -> Optional[int]:
        channel_id = self._data.get("reaction_roles", {}).get("channel_id")
        return int(channel_id) if channel_id is not None else None

    @property
    def reaction_role_map(self) -> dict[str, int]:
        raw = self._data.get("reaction_roles", {}).get("emoji_map", {})
        return {str(emoji): int(role_id) for emoji, role_id in raw.items()}

    @property
    def birthday_announce_channel_id(self) -> Optional[int]:
        channel_id = self._data.get("birthday", {}).get("announce_channel_id")
        return int(channel_id) if channel_id is not None else None

    @property
    def birthday_timezone(self) -> str:
        return str(self._data.get("birthday", {}).get("timezone", DEFAULT_BIRTHDAY_TIMEZONE))

    def get_birthday_zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.birthday_timezone)
        except ZoneInfoNotFoundError:
            log.warning("Unknown birthday timezone %s for guild %s", self.birthday_timezone, self.guild_id)
            return ZoneInfo(DEFAULT_BIRTHDAY_TIMEZONE)

    @property
    def bingo_card_title(self) -> str:
        return str(self._data.get("bingo", {}).get("card_title", "BINGO"))

    @property
    def music_dj_role_id(self) -> Optional[int]:
        role_id = self._data.get("music", {}).get("dj_role_id")
        return int(role_id) if role_id is not None else None

    @property
    def palworld_enabled(self) -> bool:
        return bool(self._data.get("palworld", {}).get("enabled", False))

    @property
    def palworld_status_channel_id(self) -> Optional[int]:
        channel_id = self._data.get("palworld", {}).get("status_channel_id")
        return int(channel_id) if channel_id is not None else None

    @property
    def _easter_eggs(self) -> dict[str, Any]:
        raw = self._data.get("easter_eggs", {})
        return raw if isinstance(raw, dict) else {}

    def _egg(self, name: str) -> dict[str, Any]:
        raw = self._easter_eggs.get(name) or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def hooty_hoo_enabled(self) -> bool:
        return bool(self._egg("hooty_hoo").get("enabled", False))

    @property
    def hooty_hoo_emoji(self) -> str:
        return str(self._egg("hooty_hoo").get("emoji", ""))

    @property
    def hooty_hoo_skip_channel_ids(self) -> set[int]:
        raw = self._egg("hooty_hoo").get("skip_channel_ids", [])
        return {int(channel_id) for channel_id in raw}

    @property
    def hooty_hoo_channel_ids(self) -> set[int]:
        raw = self._egg("hooty_hoo").get("channel_ids", [])
        return {int(channel_id) for channel_id in raw}

    @property
    def easter_eggs_allow_webhooks(self) -> bool:
        return bool(self._easter_eggs.get("allow_webhooks", False))

    @property
    def do_not_redeem_enabled(self) -> bool:
        return bool(self._egg("do_not_redeem").get("enabled", False))

    @property
    def do_not_redeem_channel_id(self) -> Optional[int]:
        channel_id = self._egg("do_not_redeem").get("channel_id")
        return int(channel_id) if channel_id is not None else None

    @property
    def do_not_redeem_reply(self) -> str:
        return str(self._egg("do_not_redeem").get("reply", "DO NOT REDEEM"))

    @property
    def announcement_party_enabled(self) -> bool:
        return bool(self._egg("announcement_party").get("enabled", False))

    @property
    def announcement_party_channel_ids(self) -> set[int]:
        raw = self._egg("announcement_party").get("channel_ids", [])
        return {int(channel_id) for channel_id in raw}

    @property
    def announcement_party_timezone(self) -> str:
        return str(self._egg("announcement_party").get("timezone", "America/Chicago"))

    @property
    def announcement_party_reaction_count(self) -> int:
        raw = self._egg("announcement_party").get("reaction_count", 5)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 5

    @property
    def announcement_party_emojis(self) -> list[str]:
        raw = self._egg("announcement_party").get("emojis", [])
        return [str(emoji) for emoji in raw if emoji]

    @property
    def candy_hunt(self) -> dict[str, Any]:
        """Optional candy-hunt overrides. Missing section means use cog defaults."""
        raw = self._data.get("candy_hunt", {})
        return raw if isinstance(raw, dict) else {}

    def is_image_diffusion_channel(self, channel_id: int) -> bool:
        return channel_id in self.image_diffusion_channel_ids

    def resolve_birthday_channel(self, guild: "discord.Guild") -> Optional["discord.TextChannel"]:
        import discord

        channel_id = self.birthday_announce_channel_id
        if channel_id is not None:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel

        fallback_name = self._data.get("birthday", {}).get("announce_channel_name")
        if fallback_name:
            channel = discord.utils.get(guild.text_channels, name=fallback_name)
            if channel is not None:
                return channel

        if guild.system_channel is not None:
            return guild.system_channel
        return None


class ServerConfigManager:
    def __init__(self, configs_dir: Path = SERVER_CONFIGS_DIR):
        self.configs_dir = configs_dir
        self._cache: dict[int, Optional[ServerConfig]] = {}

    def _config_path(self, guild_id: int) -> Path:
        return self.configs_dir / f"{guild_id}.json"

    def load(self, guild_id: int, *, reload: bool = False) -> Optional[ServerConfig]:
        if not reload and guild_id in self._cache:
            return self._cache[guild_id]

        path = self._config_path(guild_id)
        if not path.is_file():
            self._cache[guild_id] = None
            return None

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.error("Failed to load server config %s: %s", path, e)
            self._cache[guild_id] = None
            return None

        config = ServerConfig(guild_id, data)
        self._cache[guild_id] = config
        return config

    def clear_cache(self, guild_id: Optional[int] = None) -> None:
        if guild_id is None:
            self._cache.clear()
        else:
            self._cache.pop(guild_id, None)

    def list_configured_guild_ids(self) -> list[int]:
        if not self.configs_dir.is_dir():
            return []
        guild_ids = []
        for path in self.configs_dir.glob("*.json"):
            if path.name == "example.json":
                continue
            try:
                guild_ids.append(int(path.stem))
            except ValueError:
                continue
        return guild_ids


_server_config_manager: Optional[ServerConfigManager] = None


def get_server_config_manager() -> ServerConfigManager:
    global _server_config_manager
    if _server_config_manager is None:
        _server_config_manager = ServerConfigManager()
    return _server_config_manager


def get_guild_config(guild_id: int) -> Optional[ServerConfig]:
    return get_server_config_manager().load(guild_id)


def list_palworld_enabled_guild_ids() -> list[int]:
    """Guild IDs with palworld.enabled in server_configs."""
    enabled: list[int] = []
    for guild_id in get_server_config_manager().list_configured_guild_ids():
        config = get_guild_config(guild_id)
        if config is not None and config.enabled and config.palworld_enabled:
            enabled.append(guild_id)
    return enabled