"""Load Palworld watcher settings from palworld_config.json."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parents[2]
PALWORLD_CONFIG_PATH = _BASE_DIR / "palworld_config.json"
PALWORLD_CONFIG_EXAMPLE_PATH = _BASE_DIR / "palworld_config.example.json"


@dataclass(frozen=True)
class PalworldSettings:
    automatic_restart: bool = True
    wait_before_restart_seconds: int = 60
    automatic_restart_every_x_hours: int = 6
    backup_on_restart: bool = False
    backup_every_x_hours: int = 4
    rotate_after_x_backups: int = 20
    rotate_logs_every_x_runs: int = 10
    log_level: str = "INFO"
    operating_system: str = "windows"
    loop_sleep: int = 30
    status_channel_id: int | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "PalworldSettings":
        return cls(
            automatic_restart=bool(data.get("automatic_restart", True)),
            wait_before_restart_seconds=int(data.get("wait_before_restart_seconds", 60)),
            automatic_restart_every_x_hours=int(data.get("automatic_restart_every_x_hours", 6)),
            backup_on_restart=bool(data.get("backup_on_restart", False)),
            backup_every_x_hours=int(data.get("backup_every_x_hours", 4)),
            rotate_after_x_backups=int(data.get("rotate_after_x_backups", 20)),
            rotate_logs_every_x_runs=int(data.get("rotate_logs_every_x_runs", 10)),
            log_level=str(data.get("log_level", "INFO")),
            operating_system=str(data.get("operating_system", "windows")),
            loop_sleep=int(data.get("loop_sleep", 30)),
            status_channel_id=(
                int(data["status_channel_id"])
                if data.get("status_channel_id") is not None
                else None
            ),
        )


def load_palworld_settings(path: Path = PALWORLD_CONFIG_PATH) -> PalworldSettings:
    if not path.is_file():
        log.warning("Missing %s; using default Palworld settings.", path)
        return PalworldSettings()

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("Failed to load %s (%s); using defaults.", path, e)
        return PalworldSettings()

    return PalworldSettings.from_dict(data)