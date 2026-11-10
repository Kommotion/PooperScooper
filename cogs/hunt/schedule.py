"""Day-by-day hunt schedule.

Gaps are whole minutes. A cluster is a gap at or under the day's ``cluster_gap``
(the Thanksgiving default is 30 minutes, next to the 20-minute minimum). Every
day with two or more drops gets at least one cluster. The count draw is biased,
then nudged until the total equals the target when that target sits inside the
raw low/high bound.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class DayPlan:
    day: date
    low: int
    high: int
    weights: tuple[float, ...]
    lifetime: timedelta
    window_start: time
    window_end: time
    early_start: time
    early_end: time
    late_start: time
    late_end: time
    min_gap: timedelta
    max_gap: timedelta
    cluster_gap: timedelta
    guarantee_key: str | None = None


def clock_minute(value: time) -> int:
    return value.hour * 60 + value.minute


def at_minute(day: date, minute: int, zone: ZoneInfo) -> datetime:
    hour, mins = divmod(minute, 60)
    return datetime(day.year, day.month, day.day, hour, mins, tzinfo=zone)


def _whole_minutes(delta: timedelta) -> int:
    seconds = int(delta.total_seconds())
    if seconds <= 0 or seconds % 60 != 0:
        raise ValueError("hunt gaps and lifetimes must be positive whole minutes")
    return seconds // 60


def raw_bounds(days: tuple[DayPlan, ...] | list[DayPlan]) -> tuple[int, int]:
    """Inclusive sum of each day's low and high. The raw total always lands here."""
    if not days:
        return (0, 0)
    return sum(day.low for day in days), sum(day.high for day in days)


def _count_weights(day: DayPlan) -> list[float]:
    span = day.high - day.low + 1
    weights = list(day.weights)
    if span <= 0 or len(weights) != span or any(weight <= 0 for weight in weights):
        return [1.0] * max(span, 0)
    return weights


def draw_counts(days: tuple[DayPlan, ...] | list[DayPlan], rng: random.Random) -> list[int]:
    """One biased count per day, before the target nudge."""
    drawn: list[int] = []
    for day in days:
        weights = _count_weights(day)
        population = list(range(day.low, day.high + 1))
        drawn.append(rng.choices(population, weights=weights, k=1)[0])
    return drawn


def plan_counts(
    days: tuple[DayPlan, ...] | list[DayPlan],
    rng: random.Random,
    target: int,
) -> list[int]:
    """Biased counts nudged onto ``target`` when the target is inside the raw bound."""
    counts = draw_counts(days, rng)
    low, high = raw_bounds(days)
    goal = min(high, max(low, int(target)))
    total = sum(counts)
    guard = 100_000
    while total != goal and guard:
        guard -= 1
        if total < goal:
            options = [index for index, day in enumerate(days) if counts[index] < day.high]
            if not options:
                break
            counts[rng.choice(options)] += 1
            total += 1
            continue
        options = [index for index, day in enumerate(days) if counts[index] > day.low]
        if not options:
            break
        counts[rng.choice(options)] -= 1
        total -= 1
    return counts


def feasible_anchors(day: DayPlan, count: int) -> list[tuple[int, int, int]]:
    """(early minute, earliest late, latest late) pairs that can host ``count`` drops."""
    if count < 2:
        return []
    gaps = count - 1
    min_gap = _whole_minutes(day.min_gap)
    max_gap = _whole_minutes(day.max_gap)
    cluster = _whole_minutes(day.cluster_gap)
    if min_gap > max_gap or cluster < min_gap or cluster > max_gap:
        return []
    min_span = gaps * min_gap
    max_span = (gaps - 1) * max_gap + cluster
    early_lo = clock_minute(day.early_start)
    early_hi = clock_minute(day.early_end)
    late_lo = clock_minute(day.late_start)
    late_hi = min(clock_minute(day.late_end), clock_minute(day.window_end))
    window_lo = clock_minute(day.window_start)
    window_hi = clock_minute(day.window_end)
    early_lo = max(early_lo, window_lo)
    early_hi = min(early_hi, window_hi)
    late_lo = max(late_lo, window_lo)
    late_hi = min(late_hi, window_hi)
    if early_lo > early_hi or late_lo > late_hi:
        return []
    options: list[tuple[int, int, int]] = []
    for early in range(early_lo, early_hi + 1):
        late_min = max(late_lo, early + min_span)
        late_max = min(late_hi, early + max_span)
        if late_min <= late_max:
            options.append((early, late_min, late_max))
    return options


def place_minutes(day: DayPlan, count: int, rng: random.Random) -> list[int]:
    """Minute-of-day timestamps. First is early, last is late, one gap is a cluster."""
    options = feasible_anchors(day, count)
    if not options:
        raise RuntimeError(f"no feasible drop times for {day.day.isoformat()} count={count}")
    early, late_min, late_max = options[rng.randrange(len(options))]
    late = rng.randint(late_min, late_max)
    span = late - early
    gaps = count - 1
    min_gap = _whole_minutes(day.min_gap)
    max_gap = _whole_minutes(day.max_gap)
    cluster = _whole_minutes(day.cluster_gap)
    sizes = [min_gap] * gaps
    room = [max_gap - min_gap] * gaps
    pin = rng.randrange(gaps)
    room[pin] = cluster - min_gap
    extra = span - gaps * min_gap
    if extra < 0 or extra > sum(room):
        raise RuntimeError(f"gap math failed for {day.day.isoformat()} count={count}")
    while extra:
        choices = [index for index, left in enumerate(room) if left > 0]
        if not choices:
            raise RuntimeError(f"could not place a cluster on {day.day.isoformat()}")
        index = choices[rng.randrange(len(choices))]
        sizes[index] += 1
        room[index] -= 1
        extra -= 1
    cursor = early
    times = [early]
    for size in sizes:
        cursor += size
        times.append(cursor)
    if cursor != late or min(sizes) > cluster:
        raise RuntimeError(f"placed an illegal day on {day.day.isoformat()}")
    if times[0] < clock_minute(day.window_start) or times[-1] > clock_minute(day.window_end):
        raise RuntimeError(f"placed a drop outside the window on {day.day.isoformat()}")
    return times


def new_drop_id(rng: random.Random, existing: set[str]) -> str:
    for _ in range(100):
        value = f"{rng.getrandbits(32):08x}"
        if value not in existing:
            existing.add(value)
            return value
    raise RuntimeError("could not mint a hunt id")


def build_schedule(
    days: tuple[DayPlan, ...] | list[DayPlan],
    rng: random.Random,
    *,
    zone: ZoneInfo,
    target_total: int,
) -> list[dict]:
    """Persistable slots. Types are not rolled here; they are rolled when a drop is posted."""
    counts = plan_counts(days, rng, target_total)
    existing: set[str] = set()
    slots: list[dict] = []
    for day, count in zip(days, counts):
        minutes = place_minutes(day, count, rng)
        last_index = len(minutes) - 1
        for index, minute in enumerate(minutes):
            moment = at_minute(day.day, minute, zone)
            slots.append(
                {
                    "id": new_drop_id(rng, existing),
                    "at": moment.isoformat(),
                    "day": day.day.isoformat(),
                    "lifetime_seconds": int(day.lifetime.total_seconds()),
                    "status": "pending",
                    "guarantee_key": day.guarantee_key if index == last_index else None,
                }
            )
    slots.sort(key=lambda slot: slot["at"])
    return slots
