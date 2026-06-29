"""Load Palworld watcher settings from palworld_config.json."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cogs.utils.config import PalworldConfig, load_palworld_config

log = logging.getLogger(__name__)


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
    def from_config(cls, config: PalworldConfig) -> "PalworldSettings":
        return cls(
            automatic_restart=config.automatic_restart,
            wait_before_restart_seconds=config.wait_before_restart_seconds,
            automatic_restart_every_x_hours=config.automatic_restart_every_x_hours,
            backup_on_restart=config.backup_on_restart,
            backup_every_x_hours=config.backup_every_x_hours,
            rotate_after_x_backups=config.rotate_after_x_backups,
            rotate_logs_every_x_runs=config.rotate_logs_every_x_runs,
            log_level=config.log_level,
            operating_system=config.operating_system,
            loop_sleep=config.loop_sleep,
            status_channel_id=config.status_channel_id,
        )


def load_palworld_settings() -> PalworldSettings:
    return PalworldSettings.from_config(load_palworld_config())