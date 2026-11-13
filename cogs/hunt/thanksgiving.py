"""Thanksgiving turkey hunt settings.

Defaults describe the 2026 Menaces to Sobriety event. Override them from
``server_configs/<guild_id>.json`` under ``turkey_hunt``. A missing key keeps
the default. A bad value is logged and ignored. ``guild_id`` is not overridable.

The drop channel default is MTS #general, the same id documented in
``cogs/candyhunt.py`` (``MAIN_DROP_CHANNEL_ID = 932057681307512924``).
The live cog looks that channel up by id only.

Raw daily ranges sum to 74–112. The planner nudges the biased draw onto
``target_total`` (100) so a saved schedule lands on that total whenever it
fits in the raw bound. A cluster is a gap at or under ``cluster_gap``
(30 minutes). Sunday Nov 22 is a regular day. The finale Sunday, the local
date of ``drops_end``, caps its last drop at ``sunday_last_drop`` (21:30).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cogs.candyhunt import (
    ANNOUNCEMENTS_CHANNEL_ID,
    MAIN_DROP_CHANNEL_ID,
    MTS_GUILD_ID,
    NICK_ID,
    SUPPORT_GUILD_ID,
)
from cogs.hunt.schedule import DayPlan, feasible_anchors
from cogs.hunt.theme import HuntItem, contains_role_ping
from cogs.utils.constants import DATA_DIR, PROJECT_ROOT

log = logging.getLogger(__name__)

# Documented for tests and operators. ``raw_bounds(day_plans())`` is the source of truth.
DOCUMENTED_RAW_TOTAL_BOUND = (74, 112)
DOCUMENTED_TARGET_TOTAL = 100


def _pt() -> ZoneInfo:
    return ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class TurkeySettings:
    guild_id: int = MTS_GUILD_ID
    support_guild_id: int = SUPPORT_GUILD_ID
    owner_id: int = NICK_ID
    timezone_name: str = "America/Los_Angeles"
    drop_channel_id: int = MAIN_DROP_CHANNEL_ID
    cleanup_skip_channel_ids: tuple[int, ...] = (ANNOUNCEMENTS_CHANNEL_ID,)
    event_start: datetime = field(default_factory=lambda: datetime(2026, 11, 20, 5, 0, tzinfo=_pt()))
    drops_end: datetime = field(default_factory=lambda: datetime(2026, 11, 29, 22, 0, tzinfo=_pt()))
    final_at: datetime = field(default_factory=lambda: datetime(2026, 11, 30, 10, 0, tzinfo=_pt()))
    window_start: time = time(5, 0)
    window_end: time = time(22, 0)
    early_start: time = time(5, 0)
    early_end: time = time(8, 0)
    late_start: time = time(20, 0)
    late_end: time = time(22, 0)
    sunday_last_drop: time = time(21, 30)
    min_gap: timedelta = timedelta(minutes=20)
    max_gap: timedelta = timedelta(hours=3)
    cluster_gap: timedelta = timedelta(minutes=30)
    regular_low: int = 6
    regular_high: int = 10
    regular_weights: tuple[float, ...] = (1, 2, 4, 6, 5)
    thanksgiving_date: date = date(2026, 11, 26)
    thanksgiving_low: int = 18
    thanksgiving_high: int = 22
    thanksgiving_weights: tuple[float, ...] = (1, 2, 5, 2, 1)
    finale_low: int = 8
    finale_high: int = 10
    finale_weights: tuple[float, ...] = (2, 5, 3)
    lifetime: timedelta = timedelta(minutes=45)
    thanksgiving_lifetime: timedelta = timedelta(minutes=30)
    target_total: int = DOCUMENTED_TARGET_TOTAL
    regular_key: str = "regular"
    plump_key: str = "plump"
    guarantee_key: str = "golden"
    regular_name: str = "Regular"
    plump_name: str = "Plump"
    golden_name: str = "Golden"
    regular_points: int = 10
    plump_points: int = 30
    golden_points: int = 100
    regular_weight: float = 0.75
    plump_weight: float = 0.20
    golden_weight: float = 0.05
    grease_chance: float = 0.10
    regular_emoji: str = "🦃"
    plump_emoji: str = "🍗"
    golden_emoji: str = "✨"
    recap_time: time = time(22, 30)
    prize_line: str = ""
    avatar_path: str = "assets/avatars/pilgrim_shepherd.png"
    avatar_backup_name: str = "turkey_hunt_avatar_backup.png"
    state_name: str = "turkey_hunt.json"
    test_lifetime: timedelta = timedelta(minutes=2)
    test_session_min_minutes: int = 1
    test_session_max_minutes: int = 240
    test_session_min_interval: int = 1
    test_session_max_interval: int = 60

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def items(self) -> tuple[HuntItem, ...]:
        return (
            HuntItem(
                self.regular_key,
                self.regular_name,
                self.regular_points,
                self.regular_weight,
                self.grease_chance,
                self.regular_emoji,
            ),
            HuntItem(
                self.plump_key,
                self.plump_name,
                self.plump_points,
                self.plump_weight,
                0.0,
                self.plump_emoji,
            ),
            HuntItem(
                self.guarantee_key,
                self.golden_name,
                self.golden_points,
                self.golden_weight,
                0.0,
                self.golden_emoji,
            ),
        )

    def item_by_key(self, key: str) -> HuntItem | None:
        for item in self.items():
            if item.key == key:
                return item
        return None

    def event_dates(self) -> list[date]:
        start = self.event_start.astimezone(self.zone).date()
        end = self.drops_end.astimezone(self.zone).date()
        days: list[date] = []
        cursor = start
        while cursor <= end and len(days) < 40:
            days.append(cursor)
            cursor += timedelta(days=1)
        return days

    def day_plans(self) -> tuple[DayPlan, ...]:
        finale = self.drops_end.astimezone(self.zone).date()
        plans: list[DayPlan] = []
        for day in self.event_dates():
            if day == self.thanksgiving_date:
                low, high, weights = self.thanksgiving_low, self.thanksgiving_high, self.thanksgiving_weights
                lifetime = self.thanksgiving_lifetime
                guarantee = self.guarantee_key
            elif day == finale:
                low, high, weights = self.finale_low, self.finale_high, self.finale_weights
                lifetime = self.lifetime
                guarantee = None
            else:
                low, high, weights = self.regular_low, self.regular_high, self.regular_weights
                lifetime = self.lifetime
                guarantee = None
            window_end = self.sunday_last_drop if day == finale else self.window_end
            late_end = self.sunday_last_drop if day == finale else self.late_end
            if _minutes(late_end) > _minutes(window_end):
                late_end = window_end
            span = high - low + 1
            if span <= 0 or len(weights) != span or any(weight <= 0 for weight in weights):
                log.warning("turkey hunt day %s is using even count weights", day.isoformat())
                weights = tuple(1.0 for _ in range(max(span, 0)))
            plans.append(
                DayPlan(
                    day=day,
                    low=low,
                    high=high,
                    weights=weights,
                    lifetime=lifetime,
                    window_start=self.window_start,
                    window_end=window_end,
                    early_start=self.early_start,
                    early_end=self.early_end,
                    late_start=self.late_start,
                    late_end=late_end,
                    min_gap=self.min_gap,
                    max_gap=self.max_gap,
                    cluster_gap=self.cluster_gap,
                    guarantee_key=guarantee,
                )
            )
        return tuple(plans)

    def validate(self) -> bool:
        try:
            zone = self.zone
            start = self.event_start.astimezone(zone)
            end = self.drops_end.astimezone(zone)
            final = self.final_at.astimezone(zone)
        except Exception:
            return False
        if not (start < end <= final):
            return False
        if self.thanksgiving_date not in set(self.event_dates()):
            return False
        if self.thanksgiving_date == end.date():
            return False
        if self.min_gap > self.cluster_gap or self.cluster_gap > self.max_gap:
            return False
        if self.lifetime <= timedelta(0) or self.thanksgiving_lifetime <= timedelta(0):
            return False
        if _minutes(self.early_start) > _minutes(self.early_end):
            return False
        if _minutes(self.late_start) > _minutes(self.sunday_last_drop):
            return False
        if _minutes(self.sunday_last_drop) > _minutes(self.window_end):
            return False
        if sum(item.weight for item in self.items()) <= 0:
            return False
        if self.regular_points < 0 or self.plump_points < 0 or self.golden_points < 0:
            return False
        if not 0 <= self.grease_chance <= 1:
            return False
        if self.target_total < 1:
            return False
        try:
            plans = self.day_plans()
        except Exception:
            return False
        if len(plans) < 2:
            return False
        for plan in plans:
            if plan.low < 2 or plan.low > plan.high:
                return False
            if not feasible_anchors(plan, plan.low) or not feasible_anchors(plan, plan.high):
                return False
        return True

    def avatar_file(self, root: Path | None = None) -> Path:
        path = Path(self.avatar_path)
        if not path.is_absolute():
            path = (root or PROJECT_ROOT) / path
        return path

    def state_path(self) -> Path:
        return DATA_DIR / self.state_name

    def archive_path(self) -> Path:
        year = self.final_at.astimezone(self.zone).year
        return DATA_DIR / f"turkey_hunt_archive_{year}.json"


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


@dataclass(frozen=True)
class ThanksgivingTheme:
    """Live Thanksgiving event. The turkey cog is the Discord adapter."""

    settings: TurkeySettings
    key: str = "thanksgiving"

    def handles_live_event(self) -> bool:
        return True

    @property
    def drop_channel_id(self) -> int:
        return self.settings.drop_channel_id

    @property
    def guild_id(self) -> int:
        return self.settings.guild_id

    @property
    def timezone_name(self) -> str:
        return self.settings.timezone_name


def _parse_hhmm(value: object) -> time | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 5 or text[2] != ":":
        return None
    try:
        hour = int(text[:2])
        minute = int(text[3:])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def _parse_int(value: object, low: int, high: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if low <= number <= high:
        return number
    return None


def _parse_unit(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if 0 <= number <= 1:
        return number
    return None


def _parse_moment(value: object, zone: ZoneInfo) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=zone)
    return moment


def _parse_weights(value: object, count: int) -> tuple[float, ...] | None:
    if not isinstance(value, list) or len(value) != count:
        return None
    weights: list[float] = []
    for item in value:
        if isinstance(item, bool):
            return None
        try:
            number = float(item)
        except (TypeError, ValueError):
            return None
        if number <= 0:
            return None
        weights.append(number)
    return tuple(weights)


def _clean_prize(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) > 200 or contains_role_ping(text):
        return None
    return text


def _clean_avatar(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 240:
        return None
    if ".." in Path(text).parts:
        return None
    return text


def apply_turkey_hunt_overrides(base: TurkeySettings, raw: object) -> TurkeySettings:
    """Return ``base`` unchanged when the section is missing, empty, or invalid."""
    if not isinstance(raw, dict) or not raw:
        return base
    if "guild_id" in raw:
        log.warning("turkey hunt config ignored key=guild_id")
    changes: dict = {}
    zone = base.zone
    if "timezone" in raw or "timezone_name" in raw:
        name = raw["timezone"] if "timezone" in raw else raw.get("timezone_name")
        if isinstance(name, str) and name.strip():
            try:
                zone = ZoneInfo(name.strip())
            except Exception:
                log.warning("turkey hunt config ignored key=timezone")
            else:
                changes["timezone_name"] = name.strip()
        else:
            log.warning("turkey hunt config ignored key=timezone")

    def take_clock(key: str, field_name: str) -> None:
        if key not in raw:
            return
        parsed = _parse_hhmm(raw[key])
        if parsed is None:
            log.warning("turkey hunt config ignored key=%s", key)
            return
        changes[field_name] = parsed

    take_clock("window_start", "window_start")
    take_clock("window_end", "window_end")
    take_clock("early_start", "early_start")
    take_clock("early_end", "early_end")
    take_clock("late_start", "late_start")
    take_clock("late_end", "late_end")
    take_clock("sunday_last_drop", "sunday_last_drop")
    take_clock("recap_time", "recap_time")

    def take_minutes(key: str, field_name: str, low: int, high: int) -> None:
        if key not in raw:
            return
        parsed = _parse_int(raw[key], low, high)
        if parsed is None:
            log.warning("turkey hunt config ignored key=%s", key)
            return
        changes[field_name] = timedelta(minutes=parsed)

    take_minutes("min_gap_minutes", "min_gap", 1, 24 * 60)
    take_minutes("max_gap_minutes", "max_gap", 1, 24 * 60)
    take_minutes("cluster_gap_minutes", "cluster_gap", 1, 24 * 60)
    take_minutes("lifetime_minutes", "lifetime", 1, 24 * 60)
    take_minutes("thanksgiving_lifetime_minutes", "thanksgiving_lifetime", 1, 24 * 60)

    for key, field_name, low, high in (
        ("regular_low", "regular_low", 2, 100),
        ("regular_high", "regular_high", 2, 100),
        ("thanksgiving_low", "thanksgiving_low", 2, 100),
        ("thanksgiving_high", "thanksgiving_high", 2, 100),
        ("finale_low", "finale_low", 2, 100),
        ("finale_high", "finale_high", 2, 100),
        ("target_total", "target_total", 1, 10_000),
        ("regular_points", "regular_points", 0, 1000),
        ("plump_points", "plump_points", 0, 1000),
        ("golden_points", "golden_points", 0, 1000),
        ("drop_channel_id", "drop_channel_id", 1, 2**63 - 1),
    ):
        if key not in raw:
            continue
        parsed = _parse_int(raw[key], low, high)
        if parsed is None:
            log.warning("turkey hunt config ignored key=%s", key)
            continue
        changes[field_name] = parsed

    for key, field_name in (
        ("regular_weight", "regular_weight"),
        ("plump_weight", "plump_weight"),
        ("golden_weight", "golden_weight"),
        ("grease_chance", "grease_chance"),
    ):
        if key not in raw:
            continue
        parsed = _parse_unit(raw[key])
        if parsed is None:
            log.warning("turkey hunt config ignored key=%s", key)
            continue
        changes[field_name] = parsed

    if "thanksgiving_date" in raw:
        try:
            changes["thanksgiving_date"] = date.fromisoformat(str(raw["thanksgiving_date"]))
        except ValueError:
            log.warning("turkey hunt config ignored key=thanksgiving_date")

    for key, field_name in (
        ("event_start", "event_start"),
        ("drops_end", "drops_end"),
        ("final_at", "final_at"),
    ):
        if key not in raw:
            continue
        parsed = _parse_moment(raw[key], zone)
        if parsed is None:
            log.warning("turkey hunt config ignored key=%s", key)
            continue
        changes[field_name] = parsed

    if "prize_line" in raw:
        prize = _clean_prize(raw["prize_line"])
        if prize is None:
            log.warning("turkey hunt config ignored key=prize_line")
        else:
            changes["prize_line"] = prize

    if "avatar_path" in raw:
        avatar = _clean_avatar(raw["avatar_path"])
        if avatar is None:
            log.warning("turkey hunt config ignored key=avatar_path")
        else:
            changes["avatar_path"] = avatar

    if "guarantee_key" in raw:
        key = raw["guarantee_key"]
        if isinstance(key, str) and key.strip() and len(key.strip()) <= 40 and key.strip() != base.plump_key:
            changes["guarantee_key"] = key.strip()
        else:
            log.warning("turkey hunt config ignored key=guarantee_key")

    if "cleanup_skip_channel_ids" in raw:
        values = raw["cleanup_skip_channel_ids"]
        if isinstance(values, list):
            skipped: list[int] = []
            valid = True
            for item in values:
                parsed = _parse_int(item, 1, 2**63 - 1)
                if parsed is None:
                    valid = False
                    break
                skipped.append(parsed)
            if valid:
                changes["cleanup_skip_channel_ids"] = tuple(skipped)
            else:
                log.warning("turkey hunt config ignored key=cleanup_skip_channel_ids")
        else:
            log.warning("turkey hunt config ignored key=cleanup_skip_channel_ids")

    candidate = replace(base, **changes) if changes else base
    # Weights are checked against the candidate ranges, then applied together.
    weight_changes: dict = {}
    range_pairs = (
        ("regular_weights", "regular_weights", candidate.regular_low, candidate.regular_high),
        ("thanksgiving_weights", "thanksgiving_weights", candidate.thanksgiving_low, candidate.thanksgiving_high),
        ("finale_weights", "finale_weights", candidate.finale_low, candidate.finale_high),
    )
    for key, field_name, low, high in range_pairs:
        if key not in raw:
            continue
        parsed = _parse_weights(raw[key], high - low + 1)
        if parsed is None:
            log.warning("turkey hunt config ignored key=%s", key)
            continue
        weight_changes[field_name] = parsed
    if weight_changes:
        candidate = replace(candidate, **weight_changes)
    if not candidate.validate():
        log.warning("turkey hunt config ignored because the combination is invalid")
        return base
    return candidate if candidate != base else base


def load_turkey_settings(guild_id: int = MTS_GUILD_ID) -> TurkeySettings:
    base = TurkeySettings()
    try:
        from cogs.utils.server_config import get_guild_config

        config = get_guild_config(guild_id)
    except Exception:
        log.exception("turkey hunt config load failed")
        return base
    if config is None:
        return base
    return apply_turkey_hunt_overrides(base, config.turkey_hunt)
