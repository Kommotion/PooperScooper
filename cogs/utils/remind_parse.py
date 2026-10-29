"""Pure reminder time parsing. No Discord imports."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dateparser

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Los_Angeles"
MAX_WHEN_LENGTH = 200
DEFAULT_CLOCK = (9, 0)

EXAMPLES = (
    "Examples: `Friday 8pm`, `tomorrow at 7`, `in 2h`, `in 30 minutes`, "
    "`Oct 30 at noon`, `every Monday at 9am`, `every day at 8pm`, `every week`."
)
PAST_REASON = "That time is already in the past."
BAD_REASON = "I couldn't understand that time."

_DAY_NAMES = (
    "monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    "thurs|tues|mon|tue|wed|thu|fri|sat|sun"
)
_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tues": 1,
    "tue": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thurs": 3,
    "thur": 3,
    "thu": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_DURATION_TOKEN = re.compile(
    r"(\d+)\s*(weeks?|days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|[wdhms])",
    re.IGNORECASE,
)
_BARE_UNIT = re.compile(
    r"(weeks?|days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)",
    re.IGNORECASE,
)
_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)
_NAMED_DAY = re.compile(
    rf"^(?P<which>tomorrow|today)(?:\s+at)?(?:\s+(?P<time>.+))?$",
    re.IGNORECASE,
)
_WEEKDAY_TIME = re.compile(
    rf"^(?:on\s+)?(?P<day>{_DAY_NAMES})(?:\s+at)?\s+(?P<time>.+)$",
    re.IGNORECASE,
)
_WEEKDAY_BARE = re.compile(rf"^(?:on\s+)?(?P<day>{_DAY_NAMES})$", re.IGNORECASE)
_EVERY_WEEKDAY = re.compile(
    rf"^(?P<day>{_DAY_NAMES})(?:\s+(?:at\s+)?(?P<time>.+))?$",
    re.IGNORECASE,
)
_EVERY_DAY = re.compile(r"^days?(?:\s+(?:at\s+)?(?P<time>.+))?$", re.IGNORECASE)
_EVERY_WEEK_AT = re.compile(r"^weeks?(?:\s+(?:at\s+)?(?P<time>.+))$", re.IGNORECASE)
_PAST_WORDS = re.compile(r"\b(yesterday|ago)\b", re.IGNORECASE)


class ParseError(Exception):
    """The phrase was empty, in the past, or not a time we can schedule."""

    def __init__(self, reason: str):
        self.reason = reason
        self.message = f"{reason} {EXAMPLES}"
        super().__init__(self.message)


@dataclass(frozen=True)
class RepeatRule:
    kind: str
    hour: int | None = None
    minute: int | None = None
    weekday: int | None = None
    interval_seconds: int | None = None

    def validate(self) -> None:
        if self.kind == "daily":
            _require_clock(self.hour, self.minute)
            return
        if self.kind == "weekly":
            _require_clock(self.hour, self.minute)
            if self.weekday is None or not 0 <= self.weekday <= 6:
                raise ValueError("weekly repeat needs a weekday")
            return
        if self.kind == "interval":
            if self.interval_seconds is None or self.interval_seconds <= 0:
                raise ValueError("interval repeat needs a positive length")
            return
        raise ValueError(f"unknown repeat kind {self.kind}")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "hour": self.hour,
            "minute": self.minute,
            "weekday": self.weekday,
            "interval_seconds": self.interval_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RepeatRule:
        if not isinstance(data, dict):
            raise ValueError("repeat must be an object")
        rule = cls(
            kind=str(data.get("kind", "")),
            hour=_optional_int(data.get("hour")),
            minute=_optional_int(data.get("minute")),
            weekday=_optional_int(data.get("weekday")),
            interval_seconds=_optional_int(data.get("interval_seconds")),
        )
        rule.validate()
        return rule


@dataclass(frozen=True)
class ParsedWhen:
    next_fire_at: datetime
    repeat: RepeatRule | None
    timezone: str


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return moment.astimezone(timezone.utc)


def zone_or_default(name: str) -> tuple[ZoneInfo, str]:
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown timezone %s; using %s.", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE), DEFAULT_TIMEZONE


def resolve_timezone(name: str) -> str:
    """Return a canonical IANA name, or raise ZoneInfoNotFoundError."""
    cleaned = " ".join(name.strip().split()).replace(" ", "_")
    if not cleaned:
        raise ZoneInfoNotFoundError(name)
    try:
        ZoneInfo(cleaned)
        return cleaned
    except ZoneInfoNotFoundError:
        pass
    folded = cleaned.lower()
    for candidate in sorted(available_timezones_safe()):
        if candidate.lower() == folded:
            return candidate
    raise ZoneInfoNotFoundError(name)


def parse_when(text: str, *, now: datetime, timezone_name: str) -> ParsedWhen:
    """Parse a reminder phrase in ``timezone_name`` and return a future UTC time."""
    raw = " ".join(text.strip().split())
    if not raw or len(raw) > MAX_WHEN_LENGTH:
        raise ParseError(BAD_REASON)

    zone, tz_name = zone_or_default(timezone_name)
    now_utc = as_utc(now)
    local_now = now_utc.astimezone(zone)
    lowered = raw.lower()

    if lowered == "every" or lowered.startswith("every "):
        return _parse_every(lowered.removeprefix("every").strip(), now_utc, local_now, zone, tz_name)

    if _PAST_WORDS.search(lowered):
        raise ParseError(PAST_REASON)

    relative = _parse_in(lowered, now_utc)
    if relative is not None:
        _ensure_future(relative, now_utc)
        return ParsedWhen(relative, None, tz_name)

    named = _parse_named_day(lowered, now_utc, zone)
    if named is not None:
        _ensure_future(named, now_utc)
        return ParsedWhen(named, None, tz_name)

    absolute = _parse_absolute(raw, local_now, zone)
    if absolute is None:
        raise ParseError(BAD_REASON)
    _ensure_future(absolute, now_utc)
    return ParsedWhen(absolute, None, tz_name)


def next_occurrence(
    rule: RepeatRule,
    *,
    now: datetime,
    timezone_name: str,
    scheduled: datetime | None = None,
) -> datetime:
    """Next run strictly after ``now``, aligned to the series. UTC."""
    now_utc = as_utc(now)
    zone, _tz_name = zone_or_default(timezone_name)
    if rule.kind == "daily":
        assert rule.hour is not None and rule.minute is not None
        return _next_daily(now_utc, rule.hour, rule.minute, zone)
    if rule.kind == "weekly":
        assert rule.hour is not None and rule.minute is not None and rule.weekday is not None
        return _next_weekday(now_utc, rule.weekday, rule.hour, rule.minute, zone)
    if rule.kind == "interval":
        assert rule.interval_seconds is not None
        anchor = as_utc(scheduled) if scheduled is not None else now_utc
        return _advance_interval(anchor, now_utc, rule.interval_seconds)
    raise ValueError(f"unknown repeat kind {rule.kind}")


def describe_repeat(rule: RepeatRule) -> str:
    if rule.kind == "daily":
        assert rule.hour is not None and rule.minute is not None
        return f"every day at {format_clock(rule.hour, rule.minute)}"
    if rule.kind == "weekly":
        assert rule.hour is not None and rule.minute is not None and rule.weekday is not None
        day = WEEKDAY_NAMES[rule.weekday]
        return f"every {day} at {format_clock(rule.hour, rule.minute)}"
    if rule.kind == "interval" and rule.interval_seconds is not None:
        return _describe_interval(rule.interval_seconds)
    return "on a schedule"


def format_clock(hour: int, minute: int) -> str:
    suffix = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    if minute:
        return f"{hour12}:{minute:02d}{suffix}"
    return f"{hour12}{suffix}"


def parse_clock(text: str) -> tuple[int, int] | None:
    cleaned = text.strip().lower().replace(".", "")
    if cleaned in {"noon", "12 noon"}:
        return 12, 0
    if cleaned in {"midnight", "12 midnight"}:
        return 0, 0
    match = _CLOCK.fullmatch(cleaned)
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    ampm = match.group(3)
    if minute > 59:
        return None
    if ampm:
        if hour < 1 or hour > 12:
            return None
        if ampm == "am":
            hour = 0 if hour == 12 else hour
        elif hour != 12:
            hour += 12
        return hour, minute
    if hour > 23:
        return None
    return hour, minute


def parse_duration_seconds(text: str) -> int | None:
    """Seconds when ``text`` is only a duration like ``2h30m`` or ``30 minutes``.

    Returns None when the text is not a duration. Raises ParseError for a
    duration that is zero.
    """
    body = text.strip()
    bare = _BARE_UNIT.fullmatch(body)
    if bare is not None:
        return _unit_seconds(bare.group(1))
    matches = list(_DURATION_TOKEN.finditer(body))
    if not matches:
        return None
    cursor = 0
    gaps: list[str] = []
    for match in matches:
        gaps.append(body[cursor:match.start()])
        cursor = match.end()
    gaps.append(body[cursor:])
    leftover = " ".join(part.strip(" ,") for part in gaps)
    leftover = re.sub(r"\band\b", " ", leftover, flags=re.IGNORECASE)
    leftover = " ".join(leftover.split())
    if leftover:
        return None
    total = 0
    for match in matches:
        total += int(match.group(1)) * _unit_seconds(match.group(2))
    if total <= 0:
        raise ParseError(PAST_REASON)
    return total


def available_timezones_safe() -> set[str]:
    from zoneinfo import available_timezones

    try:
        return available_timezones()
    except Exception:
        log.warning("Could not list IANA timezones.", exc_info=True)
        return set()


def _parse_every(
    body: str,
    now_utc: datetime,
    local_now: datetime,
    zone: ZoneInfo,
    tz_name: str,
) -> ParsedWhen:
    if not body:
        raise ParseError(BAD_REASON)

    weekday_match = _EVERY_WEEKDAY.fullmatch(body)
    if weekday_match is not None:
        weekday = _WEEKDAYS.get(weekday_match.group("day").lower())
        if weekday is None:
            raise ParseError(BAD_REASON)
        clock = _clock_or_default(weekday_match.group("time"))
        if clock is None:
            raise ParseError(BAD_REASON)
        hour, minute = clock
        rule = RepeatRule(kind="weekly", weekday=weekday, hour=hour, minute=minute)
        when = _next_weekday(now_utc, weekday, hour, minute, zone)
        _ensure_future(when, now_utc)
        return ParsedWhen(when, rule, tz_name)

    day_match = _EVERY_DAY.fullmatch(body)
    if day_match is not None:
        clock = _clock_or_default(day_match.group("time"))
        if clock is None:
            raise ParseError(BAD_REASON)
        hour, minute = clock
        rule = RepeatRule(kind="daily", hour=hour, minute=minute)
        when = _next_daily(now_utc, hour, minute, zone)
        _ensure_future(when, now_utc)
        return ParsedWhen(when, rule, tz_name)

    week_at = _EVERY_WEEK_AT.fullmatch(body)
    if week_at is not None:
        clock = parse_clock(week_at.group("time"))
        if clock is None:
            raise ParseError(BAD_REASON)
        hour, minute = clock
        rule = RepeatRule(kind="weekly", weekday=local_now.weekday(), hour=hour, minute=minute)
        when = _next_weekday(now_utc, local_now.weekday(), hour, minute, zone)
        _ensure_future(when, now_utc)
        return ParsedWhen(when, rule, tz_name)

    seconds = parse_duration_seconds(body)
    if seconds is None:
        raise ParseError(BAD_REASON)
    rule = RepeatRule(kind="interval", interval_seconds=seconds)
    when = now_utc + timedelta(seconds=seconds)
    _ensure_future(when, now_utc)
    return ParsedWhen(when, rule, tz_name)


def _parse_in(lowered: str, now_utc: datetime) -> datetime | None:
    match = re.fullmatch(r"in\s+(.+)", lowered)
    if match is None:
        return None
    seconds = parse_duration_seconds(match.group(1))
    if seconds is None:
        return None
    return now_utc + timedelta(seconds=seconds)


def _parse_named_day(lowered: str, now_utc: datetime, zone: ZoneInfo) -> datetime | None:
    named = _NAMED_DAY.fullmatch(lowered)
    if named is not None:
        clock = _clock_or_default(named.group("time"))
        if clock is None:
            return None
        local = now_utc.astimezone(zone)
        day = local.date() + (timedelta(days=1) if named.group("which") == "tomorrow" else timedelta())
        return _combine(day, clock[0], clock[1], zone)

    weekday = _WEEKDAY_TIME.fullmatch(lowered)
    if weekday is not None:
        clock = parse_clock(weekday.group("time"))
        if clock is None:
            return None
        day_index = _WEEKDAYS[weekday.group("day").lower()]
        return _next_weekday(now_utc, day_index, clock[0], clock[1], zone)

    bare = _WEEKDAY_BARE.fullmatch(lowered)
    if bare is not None:
        day_index = _WEEKDAYS[bare.group("day").lower()]
        return _next_weekday(now_utc, day_index, DEFAULT_CLOCK[0], DEFAULT_CLOCK[1], zone)

    clock = parse_clock(lowered)
    if clock is None:
        return None
    return _next_daily(now_utc, clock[0], clock[1], zone)


def _parse_absolute(text: str, local_now: datetime, zone: ZoneInfo) -> datetime | None:
    settings = {
        "TIMEZONE": getattr(zone, "key", DEFAULT_TIMEZONE),
        "TO_TIMEZONE": getattr(zone, "key", DEFAULT_TIMEZONE),
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": local_now,
    }
    try:
        parsed = dateparser.parse(text, languages=["en"], settings=settings)
    except Exception:
        log.warning("dateparser failed on %r", text, exc_info=True)
        return None
    if parsed is None:
        return None
    return _closer_future(parsed, local_now, zone)


def _closer_future(parsed: datetime, now_local: datetime, zone: ZoneInfo) -> datetime:
    """Pull a dateparser result back one year when it skipped a still-future date."""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    local = parsed.astimezone(zone)
    now_utc = now_local.astimezone(timezone.utc)
    if local.year > now_local.year:
        try:
            sooner_local = local.replace(year=local.year - 1)
        except ValueError:
            sooner_local = None
        if sooner_local is not None and sooner_local.astimezone(timezone.utc) > now_utc:
            return sooner_local.astimezone(timezone.utc)
    return local.astimezone(timezone.utc)


def _next_daily(now_utc: datetime, hour: int, minute: int, zone: ZoneInfo) -> datetime:
    local = now_utc.astimezone(zone)
    candidate = _combine(local.date(), hour, minute, zone)
    if candidate <= now_utc:
        candidate = _combine(local.date() + timedelta(days=1), hour, minute, zone)
    return candidate


def _next_weekday(now_utc: datetime, weekday: int, hour: int, minute: int, zone: ZoneInfo) -> datetime:
    local = now_utc.astimezone(zone)
    days_ahead = (weekday - local.weekday()) % 7
    day = local.date() + timedelta(days=days_ahead)
    candidate = _combine(day, hour, minute, zone)
    if candidate <= now_utc:
        candidate = _combine(day + timedelta(days=7), hour, minute, zone)
    return candidate


def _advance_interval(scheduled: datetime, now_utc: datetime, seconds: int) -> datetime:
    if now_utc < scheduled:
        return scheduled
    delta = now_utc - scheduled
    elapsed = delta.days * 86400 + delta.seconds
    steps = elapsed // seconds + 1
    nxt = scheduled + timedelta(seconds=steps * seconds)
    if nxt <= now_utc:
        nxt = scheduled + timedelta(seconds=(steps + 1) * seconds)
    return nxt


def _combine(day: date, hour: int, minute: int, zone: ZoneInfo) -> datetime:
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    return local.astimezone(timezone.utc)


def _clock_or_default(text: str | None) -> tuple[int, int] | None:
    if text is None or not text.strip():
        return DEFAULT_CLOCK
    return parse_clock(text)


def _ensure_future(moment: datetime, now_utc: datetime) -> None:
    if moment <= now_utc:
        raise ParseError(PAST_REASON)


def _require_clock(hour: int | None, minute: int | None) -> None:
    if hour is None or minute is None or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("repeat needs a clock time")


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)  # type: ignore[arg-type]


def _unit_seconds(unit: str) -> int:
    cleaned = unit.lower()
    if cleaned in {"w", "week", "weeks"}:
        return 7 * 24 * 3600
    if cleaned in {"d", "day", "days"}:
        return 24 * 3600
    if cleaned in {"h", "hr", "hrs", "hour", "hours"}:
        return 3600
    if cleaned in {"m", "min", "mins", "minute", "minutes"}:
        return 60
    if cleaned in {"s", "sec", "secs", "second", "seconds"}:
        return 1
    raise ValueError(unit)


def _describe_interval(seconds: int) -> str:
    units = (
        (7 * 86400, "week"),
        (86400, "day"),
        (3600, "hour"),
        (60, "minute"),
        (1, "second"),
    )
    for size, name in units:
        if seconds % size == 0:
            count = seconds // size
            if count == 1:
                return f"every {name}"
            return f"every {count} {name}s"
    return f"every {seconds} seconds"
