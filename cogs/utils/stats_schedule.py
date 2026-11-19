"""When the weekly stats card is due.

Weeks are Monday-start in America/Los_Angeles. The card posts Sunday at
21:00 local time. ZoneInfo supplies the offset, including the March and
November changes. Do not pin a fixed UTC offset.

Activity from Sunday 21:00 until Monday 00:00 still has this Monday as its
calendar week_start. The counters reset when the card is posted, so those
late events simply land in the fresh weekly columns and show up on the next
card. Label a posted card with the Monday of the week that was closed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LA_NAME = "America/Los_Angeles"
LA = ZoneInfo(LA_NAME)

# Monday is 0. Sunday 21:00 is the default post moment.
DEFAULT_WEEKDAY = 6
DEFAULT_HOUR = 21


class PostSchedule:
    """Which local weekday and hour a guild posts its card."""

    __slots__ = ("weekday", "hour", "timezone")

    def __init__(self, weekday: int = DEFAULT_WEEKDAY, hour: int = DEFAULT_HOUR, timezone: str = LA_NAME) -> None:
        self.weekday = int(weekday)
        self.hour = int(hour)
        self.timezone = timezone or LA_NAME


def _as_aware(moment: datetime) -> datetime:
    """Naive values are UTC. Callers that mean Pacific time should pass an aware datetime."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _schedule_of(schedule: PostSchedule | dict | object | None) -> PostSchedule:
    if schedule is None:
        return PostSchedule()
    if isinstance(schedule, PostSchedule):
        return schedule
    if isinstance(schedule, dict):
        return PostSchedule(
            weekday=int(schedule.get("weekday", DEFAULT_WEEKDAY)),
            hour=int(schedule.get("hour", DEFAULT_HOUR)),
            timezone=str(schedule.get("timezone") or LA_NAME),
        )
    return PostSchedule(
        weekday=int(getattr(schedule, "weekday", DEFAULT_WEEKDAY)),
        hour=int(getattr(schedule, "hour", DEFAULT_HOUR)),
        timezone=str(getattr(schedule, "timezone", None) or LA_NAME),
    )


def week_start_for(moment: datetime) -> date:
    """Monday (America/Los_Angeles) of the calendar week that contains ``moment``."""
    local = _as_aware(moment).astimezone(LA)
    return local.date() - timedelta(days=local.weekday())


def post_moment_for(
    week_start: date | datetime | str,
    weekday: int = DEFAULT_WEEKDAY,
    hour: int = DEFAULT_HOUR,
    timezone_name: str = LA_NAME,
) -> datetime:
    """Aware post time for that Monday's week. Default is Sunday 21:00 Pacific."""
    start = _as_date(week_start)
    day = start + timedelta(days=int(weekday))
    zone = ZoneInfo(timezone_name or LA_NAME)
    return datetime(day.year, day.month, day.day, int(hour), 0, 0, tzinfo=zone)


def due_week(
    now: datetime,
    last_posted_week_start: date | datetime | str | None,
    schedule: PostSchedule | dict | object | None = None,
) -> date | None:
    """Monday of the single week whose card should post now.

    Picks the latest post moment that is already due. If that week was not
    posted, return it. A missed Sunday therefore catches up once. Older missed
    weeks are not posted, and a week that was already posted is not posted again.
    Returns None before the post moment, and None after a successful post.
    """
    spec = _schedule_of(schedule)
    aware_now = _as_aware(now)
    local_now = aware_now.astimezone(ZoneInfo(spec.timezone))
    this_monday = local_now.date() - timedelta(days=local_now.weekday())
    this_post = post_moment_for(this_monday, spec.weekday, spec.hour, spec.timezone)
    candidate = this_monday if this_post <= aware_now else this_monday - timedelta(days=7)
    candidate_post = post_moment_for(candidate, spec.weekday, spec.hour, spec.timezone)
    if candidate_post > aware_now:
        return None
    if last_posted_week_start is not None and _as_date(last_posted_week_start) >= candidate:
        return None
    return candidate


def split_voice_minutes(start: datetime, end: datetime) -> list[tuple[int, int, int]]:
    """Whole minutes in each Pacific clock hour between ``start`` and ``end``.

    The total is real elapsed time, floored to minutes. A session under one
    minute is empty. The repeated 1 AM hour on a fall-back night shares one
    bucket. The missing 2 AM hour on a spring-forward morning is skipped.
    """
    start_utc = _as_aware(start).astimezone(timezone.utc)
    end_utc = _as_aware(end).astimezone(timezone.utc)
    if end_utc <= start_utc:
        return []

    buckets: dict[tuple[int, int], float] = {}
    cursor = start_utc
    steps = 0
    while cursor < end_utc and steps < 24 * 21:
        steps += 1
        local = cursor.astimezone(LA)
        next_local = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        next_utc = next_local.astimezone(timezone.utc)
        if next_utc <= cursor:
            next_utc = cursor + timedelta(minutes=1)
        chunk_end = end_utc if next_utc > end_utc else next_utc
        seconds = (chunk_end - cursor).total_seconds()
        if seconds > 0:
            key = (local.weekday(), local.hour)
            buckets[key] = buckets.get(key, 0.0) + seconds
        cursor = chunk_end

    total_minutes = int(sum(buckets.values()) // 60)
    if total_minutes <= 0:
        return []

    whole = {key: int(seconds // 60) for key, seconds in buckets.items()}
    fractions = sorted(
        ((seconds - int(seconds // 60) * 60, key) for key, seconds in buckets.items()),
        key=lambda item: item[0],
        reverse=True,
    )
    left = total_minutes - sum(whole.values())
    order = [key for _, key in fractions]
    for index in range(max(0, left)):
        whole[order[index % len(order)]] += 1

    rows = [(weekday, hour, minutes) for (weekday, hour), minutes in whole.items() if minutes > 0]
    rows.sort()
    return rows
