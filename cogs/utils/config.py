"""Validated bot configuration from config.json with environment variable overrides."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.json"
PALWORLD_CONFIG_PATH = PROJECT_ROOT / "palworld_config.json"


class LavalinkConfig(BaseModel):
    enabled: bool = True
    managed: bool = True
    auto_start: bool = True
    kill_existing: bool = True
    host: str = "localhost"
    port: int = Field(default=2333, ge=1, le=65535)
    password: str = "changeme"
    java_executable: str = "java"
    directory: str = "lavalink"
    ytdlp_executable: str = ""
    spotify_country_code: str = "US"
    deezer_arl: str = ""
    startup_timeout_seconds: int = Field(default=90, ge=1)

    @field_validator("directory", mode="before")
    @classmethod
    def _strip_directory(cls, value: Any) -> str:
        return str(value).strip() or "lavalink"


class ComfyUIConfig(BaseModel):
    """Single ComfyUI backend shared by the Discord bot and Comfy Desktop UI."""

    host: str = "127.0.0.1"
    port: int = Field(default=8188, ge=1, le=65535)
    # Relative to project root, or absolute. Default is the unified install.
    root: str = "ComfyUI_New"
    auto_start: bool = True

    @field_validator("root", mode="before")
    @classmethod
    def _strip_root(cls, value: Any) -> str:
        return str(value).strip() or "ComfyUI_New"

    def resolved_root(self) -> Path:
        path = Path(self.root)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()


class BotConfig(BaseModel):
    token: SecretStr
    client_id: str
    spotify_client_id: str = ""
    spotify_secret: SecretStr = SecretStr("")
    lavalink: LavalinkConfig = Field(default_factory=LavalinkConfig)
    comfyui: ComfyUIConfig = Field(default_factory=ComfyUIConfig)

    def to_credentials_dict(self) -> dict[str, Any]:
        """Legacy dict shape used by Lavalink helpers and Wavelink bootstrap."""
        data = self.model_dump(mode="json")
        data["token"] = self.token.get_secret_value()
        data["spotify_secret"] = self.spotify_secret.get_secret_value()
        return data


class PalworldConfig(BaseModel):
    automatic_restart: bool = True
    wait_before_restart_seconds: int = Field(default=60, ge=0)
    automatic_restart_every_x_hours: int = Field(default=6, ge=1)
    backup_on_restart: bool = False
    backup_every_x_hours: int = Field(default=4, ge=0)
    rotate_after_x_backups: int = Field(default=20, ge=0)
    rotate_logs_every_x_runs: int = Field(default=10, ge=0)
    log_level: str = "INFO"
    operating_system: str = "windows"
    loop_sleep: int = Field(default=30, ge=1)
    status_channel_id: int | None = None


def _read_json_file(path: Path) -> dict[str, Any]:
    # utf-8-sig tolerates a BOM (common when editors save as "UTF-8 with BOM" on Windows)
    with open(path, encoding="utf-8-sig") as handle:
        return json.load(handle)


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Overlay known environment variables onto config.json values."""
    merged = dict(data)

    env_map = {
        "DISCORD_TOKEN": ("token", None),
        "POOPER_DISCORD_TOKEN": ("token", None),
        "DISCORD_CLIENT_ID": ("client_id", None),
        "POOPER_DISCORD_CLIENT_ID": ("client_id", None),
        "SPOTIFY_CLIENT_ID": ("spotify_client_id", None),
        "SPOTIFY_SECRET": ("spotify_secret", None),
        "LAVALINK_ENABLED": ("lavalink", "enabled"),
        "LAVALINK_MANAGED": ("lavalink", "managed"),
        "LAVALINK_AUTO_START": ("lavalink", "auto_start"),
        "LAVALINK_KILL_EXISTING": ("lavalink", "kill_existing"),
        "LAVALINK_HOST": ("lavalink", "host"),
        "LAVALINK_PORT": ("lavalink", "port"),
        "LAVALINK_PASSWORD": ("lavalink", "password"),
        "LAVALINK_JAVA": ("lavalink", "java_executable"),
        "LAVALINK_DIRECTORY": ("lavalink", "directory"),
        "LAVALINK_YTDLP_EXECUTABLE": ("lavalink", "ytdlp_executable"),
        "LAVALINK_SPOTIFY_COUNTRY_CODE": ("lavalink", "spotify_country_code"),
        "LAVALINK_DEEZER_ARL": ("lavalink", "deezer_arl"),
        "LAVALINK_STARTUP_TIMEOUT_SECONDS": ("lavalink", "startup_timeout_seconds"),
        "COMFYUI_HOST": ("comfyui", "host"),
        "COMFYUI_PORT": ("comfyui", "port"),
        "COMFYUI_ROOT": ("comfyui", "root"),
        "COMFYUI_AUTO_START": ("comfyui", "auto_start"),
    }

    for env_name, location in env_map.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue

        if location[1] is None:
            merged[location[0]] = _coerce_env_value(location[0], raw)
            continue

        nested = merged.setdefault(location[0], {})
        if not isinstance(nested, dict):
            nested = {}
            merged[location[0]] = nested
        nested[location[1]] = _coerce_env_value(location[1], raw)

    return merged


def _coerce_env_value(field_name: str, raw: str) -> Any:
    if field_name in {"enabled", "managed", "auto_start", "kill_existing", "automatic_restart", "backup_on_restart"}:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if field_name in {"port", "startup_timeout_seconds", "wait_before_restart_seconds", "automatic_restart_every_x_hours",
                      "backup_every_x_hours", "rotate_after_x_backups", "rotate_logs_every_x_runs", "loop_sleep",
                      "status_channel_id"}:
        return int(raw)
    return raw


_config: BotConfig | None = None
_palworld_config: PalworldConfig | None = None


def load_config(*, reload: bool = False) -> BotConfig:
    global _config
    if _config is not None and not reload:
        return _config

    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"Missing {CONFIG_PATH.name}. Copy config.example.json to config.json or set DISCORD_TOKEN."
        )

    file_data = _read_json_file(CONFIG_PATH)
    merged = _apply_env_overrides(file_data)
    _config = BotConfig.model_validate(merged)
    return _config


def load_palworld_config(*, reload: bool = False) -> PalworldConfig:
    global _palworld_config
    if _palworld_config is not None and not reload:
        return _palworld_config

    if not PALWORLD_CONFIG_PATH.is_file():
        log.warning("Missing %s; using default Palworld settings.", PALWORLD_CONFIG_PATH.name)
        _palworld_config = PalworldConfig()
        return _palworld_config

    try:
        file_data = _read_json_file(PALWORLD_CONFIG_PATH)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to load %s (%s); using defaults.", PALWORLD_CONFIG_PATH, exc)
        _palworld_config = PalworldConfig()
        return _palworld_config

    merged = _apply_env_overrides(file_data)
    _palworld_config = PalworldConfig.model_validate(merged)
    return _palworld_config