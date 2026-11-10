"""Hunt copy. Strings are built from the theme settings so dates and odds stay in config.

Nothing here mentions @everyone, @here, or a role. Names are passed through
``safe_label`` before they are interpolated.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cogs.hunt.theme import HuntItem, contains_role_ping

_PING = re.compile(r"@everyone|@here|<@&\d*>|<@!?\d*>", re.IGNORECASE)

DROP_MARKER = "A turkey waddled into the channel."
RECAP_MARKER = "Turkey tally"
KICKOFF_MARKER = "The turkey hunt is on."
WINNER_MARKER = "Turkey hunt champion"


def safe_label(name: object) -> str:
    """A display name that cannot ping a role, everyone, or here."""
    text = str(name or "").strip()
    text = _PING.sub("", text)
    text = text.replace("@", "")
    text = " ".join(text.split())
    return text or "someone"


def guard_copy(text: str) -> str:
    if contains_role_ping(text):
        raise ValueError("hunt copy contains a role ping")
    return text


def is_deletable_hunt_message(text: str) -> bool:
    """Drop prompts and daily recaps can be removed. The kickoff and the winner stay."""
    if not text:
        return False
    if WINNER_MARKER in text or KICKOFF_MARKER in text:
        return False
    return DROP_MARKER in text or RECAP_MARKER in text


def point_word(points: int) -> str:
    return "point" if int(points) == 1 else "points"


def _twelve_hour(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    suffix = "AM" if moment.hour < 12 else "PM"
    return f"{hour}:{moment.minute:02d} {suffix}"


def friendly_moment(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return f"{local.strftime('%A, %B')} {local.day} at {_twelve_hour(local)}"


def _percent(weight: float, total: float) -> str:
    if total <= 0:
        return "0%"
    pct = weight / total * 100
    rounded = round(pct)
    if abs(pct - rounded) < 0.05:
        return f"{int(rounded)}%"
    return f"{pct:.1f}%"


def _one_in(chance: float) -> str | None:
    if chance <= 0:
        return None
    count = max(1, int(round(1 / chance)))
    return f"1 in {count}"


def kickoff_text(settings) -> str:
    items = settings.items()
    total = sum(item.weight for item in items)
    zone = settings.zone
    lines = [
        KICKOFF_MARKER,
        "",
        "Grab a turkey in this channel. The first person to click catches it. "
        "There is no cooldown and no catch limit.",
        "",
        "What's waddling out:",
    ]
    for item in items:
        odds = _percent(item.weight, total)
        line = f"{item.emoji} {item.name} — {odds} — {item.points} {point_word(item.points)}."
        slip = _one_in(item.grease_chance)
        if slip:
            line += (
                f" About {slip} grabs slip on greased feathers. "
                "The turkey stays up, and the score does not move."
            )
        lines.append(line)
    life = int(settings.lifetime.total_seconds() // 60)
    thanksgiving_life = int(settings.thanksgiving_lifetime.total_seconds() // 60)
    lines.extend(
        [
            "",
            f"A turkey stays up for {life} minutes "
            f"({thanksgiving_life} minutes on Thanksgiving, {settings.thanksgiving_date.isoformat()}).",
            f"Drops run {_twelve_hour(datetime.combine(settings.event_start.date(), settings.window_start))} "
            f"to {_twelve_hour(datetime.combine(settings.event_start.date(), settings.window_end))} each day.",
            f"Drops end {friendly_moment(settings.drops_end, zone)}.",
            f"The winner is announced {friendly_moment(settings.final_at, zone)}.",
            "No role pings. Just turkeys.",
        ]
    )
    return guard_copy("\n".join(lines))


def drop_text(
    item: HuntItem,
    expires_at: datetime,
    *,
    test: bool,
    drops_end: datetime | None,
) -> str:
    stamp = int(expires_at.timestamp())
    lines = [
        "TEST TURKEY" if test else "TURKEY HUNT",
        DROP_MARKER,
        f"{item.emoji} {item.name} turkey — {item.points} {point_word(item.points)}.",
        "First grab catches it. No cooldown. No catch limit.",
        f"It sticks around until <t:{stamp}:t>.",
    ]
    if not test and drops_end is not None and expires_at > drops_end:
        close_stamp = int(drops_end.timestamp())
        lines.append(f"The hunt closes <t:{close_stamp}:t>. Uncaught turkeys leave then.")
    return guard_copy("\n".join(lines))


def catch_public(name: object, item: HuntItem, total: int, *, test: bool) -> str:
    who = safe_label(name)
    prefix = "TEST. " if test else ""
    text = (
        f"{prefix}{who} snatched a {item.emoji} {item.name} turkey "
        f"for {item.points} {point_word(item.points)}. "
        f"Running total: {total}."
    )
    return guard_copy(text)


def catch_dm(name: object, item: HuntItem, total: int, rank: int | None, *, test: bool) -> str:
    who = safe_label(name)
    where = "test tally" if test else "turkey hunt"
    rank_line = f" You are #{rank} on the board." if rank else ""
    text = (
        f"{item.emoji} {who}, you caught a {item.name} turkey!\n"
        f"That's {item.points} {point_word(item.points)} on the {where}.\n"
        f"Running total: {total}.{rank_line}\n"
        "The platter remembers."
    )
    return guard_copy(text)


def slip_text(item: HuntItem) -> str:
    text = (
        f"{item.emoji} Greased feathers! The {item.name} turkey squirms free "
        "and stays on the table."
    )
    return guard_copy(text)


def score_groups(scores: dict) -> list[tuple[int, list[str]]]:
    """Positive totals as (score, user ids), highest score first. Ties share a group."""
    buckets: dict[int, list[str]] = {}
    for key, value in scores.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        buckets.setdefault(int(value), []).append(str(key))
    groups: list[tuple[int, list[str]]] = []
    for score in sorted(buckets, reverse=True):
        groups.append((score, sorted(buckets[score], key=lambda uid: (len(uid), uid))))
    return groups


def _ordinal(rank: int) -> str:
    if 10 <= rank % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(rank % 10, "th")
    return f"{rank}{suffix}"


def _join_names(user_ids: list[str], names: dict | None) -> str:
    labels = [safe_label((names or {}).get(uid, uid)) for uid in user_ids]
    if not labels:
        return "someone"
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def tie_lines(scores: dict, names: dict | None, *, limit: int = 10) -> list[str]:
    """One sentence per tied score group that starts inside the top of the board."""
    lines: list[str] = []
    seen = 0
    for score, user_ids in score_groups(scores):
        rank = seen + 1
        if seen >= limit:
            break
        if len(user_ids) > 1:
            lines.append(
                f"Tie for {_ordinal(rank)}: {_join_names(user_ids, names)} with {score} {point_word(score)}."
            )
        seen += len(user_ids)
    return lines


def leaderboard_lines(scores: dict, names: dict | None, *, limit: int = 10) -> list[str]:
    lines: list[str] = []
    seen = 0
    for score, user_ids in score_groups(scores):
        if seen >= limit:
            break
        rank = seen + 1
        tie = " (tie)" if len(user_ids) > 1 else ""
        lines.append(f"#{rank}{tie} {_join_names(user_ids, names)} — {score}")
        seen += len(user_ids)
    return lines


def your_score_line(scores: dict, user_id: int) -> str:
    groups = score_groups(scores)
    seen = 0
    key = str(user_id)
    total = 0
    raw = scores.get(key, scores.get(user_id, 0))
    if isinstance(raw, int) and not isinstance(raw, bool):
        total = raw
    rank = None
    for score, user_ids in groups:
        if key in user_ids:
            rank = seen + 1
            break
        seen += len(user_ids)
    if rank is None or total <= 0:
        return "Your score: 0."
    return f"Your score: {total} · #{rank}."


def leaderboard_text(scores: dict, names: dict | None, user_id: int, *, test: bool) -> str:
    title = "Test turkeys" if test else "Turkey hunt"
    rows = leaderboard_lines(scores, names, limit=10)
    board = "\n".join(rows) if rows else "No turkeys have been caught yet."
    text = f"{title}\n\n{board}\n\n{your_score_line(scores, user_id)}"
    return guard_copy(text)


def recap_text(
    settings,
    day,
    scores: dict,
    names: dict | None,
    catches: list[dict],
) -> str:
    zone = settings.zone
    local_day = datetime(day.year, day.month, day.day, 12, 0, tzinfo=zone)
    rows = leaderboard_lines(scores, names, limit=5)
    board = "\n".join(rows) if rows else "Nobody has points yet."
    today = [row for row in catches if row.get("day") == day.isoformat()]
    if today:
        caught = "\n".join(
            f"{safe_label((names or {}).get(str(row.get('user_id')), row.get('user_id')))} — "
            f"{row.get('name')} — {row.get('points')}"
            for row in today
        )
    else:
        caught = "No turkeys were caught today."
    guarantee = settings.guarantee_key
    goldens = [row for row in today if row.get("type_key") == guarantee]
    if goldens:
        golden_line = "Goldens today:\n" + "\n".join(
            f"{safe_label((names or {}).get(str(row.get('user_id')), row.get('user_id')))} caught a "
            f"{row.get('name')} turkey."
            for row in goldens
        )
    else:
        golden_line = "No golden turkey was caught today."
    text = (
        f"{RECAP_MARKER} for {local_day.strftime('%A, %B')} {day.day}\n\n"
        f"Top 5\n{board}\n\n"
        f"Today's catches\n{caught}\n\n"
        f"{golden_line}"
    )
    return guard_copy(text)


def _count_catches(catches: list[dict], key: str | None = None) -> int:
    if key is None:
        return len(catches)
    return sum(1 for row in catches if row.get("type_key") == key)


def winner_text(settings, scores: dict, names: dict | None, catches: list[dict], slips: int) -> str:
    groups = score_groups(scores)
    if not groups:
        headline = "The flock wins this one. Nobody caught a turkey."
    else:
        top_score, top_ids = groups[0]
        joined = _join_names(top_ids, names)
        if len(top_ids) > 1:
            headline = (
                f"It's a tie! {joined} share first place with {top_score} {point_word(top_score)}."
            )
        else:
            headline = f"{joined} wins the turkey hunt with {top_score} {point_word(top_score)}!"
    board = leaderboard_lines(scores, names, limit=10)
    board_text = "\n".join(board) if board else "The board is empty."
    ties = tie_lines(scores, names, limit=10)
    tie_text = "\n".join(ties) if ties else ""
    prize = str(getattr(settings, "prize_line", "") or "").strip()
    if not prize:
        prize_line = "Prize: (not set yet)"
    else:
        prize_line = f"Prize: {safe_label(prize)}"
    guarantee = settings.guarantee_key
    player_ids = {uid for _score, ids in groups for uid in ids}
    stats = [
        f"Turkeys caught: {_count_catches(catches)}",
        f"Greased getaways: {int(slips)}",
        f"Golden turkeys caught: {_count_catches(catches, guarantee)}",
        f"Plump turkeys caught: {_count_catches(catches, settings.plump_key)}",
        f"Players: {len(player_ids)}",
    ]
    parts = [
        WINNER_MARKER,
        "",
        headline,
        "",
        "Top 10",
        board_text,
    ]
    if tie_text:
        parts.extend(["", tie_text])
    parts.extend(["", "The flock", *stats, "", prize_line])
    return guard_copy("\n".join(parts))


def format_schedule_summary(slots: list[dict], zone: ZoneInfo, *, cluster_minutes: int) -> str:
    """One line per day: count, first, last, and how many cluster-sized gaps."""
    by_day: dict[str, list[datetime]] = {}
    for slot in slots:
        moment = datetime.fromisoformat(str(slot["at"]))
        by_day.setdefault(str(slot["day"]), []).append(moment)
    lines = [f"{len(slots)} turkeys planned."]
    for day in sorted(by_day):
        moments = sorted(by_day[day])
        local = [moment.astimezone(zone) for moment in moments]
        gaps = [
            int((local[index] - local[index - 1]).total_seconds() // 60)
            for index in range(1, len(local))
        ]
        clusters = sum(1 for gap in gaps if gap <= cluster_minutes)
        lines.append(
            f"{day}: {len(local)} drops, {_twelve_hour(local[0])}–{_twelve_hour(local[-1])}, "
            f"clusters {clusters}"
        )
    return guard_copy("\n".join(lines))


def status_text(state: dict, settings, now: datetime, *, live_enabled: bool) -> str:
    pending = [slot for slot in state.get("schedule") or [] if slot.get("status") == "pending"]
    nxt = pending[0]["at"] if pending else "none"
    scores = state.get("scores") or {}
    lines = [
        f"live switch: {'on' if live_enabled else 'off'}",
        f"local pause: {'on' if state.get('event_disabled') else 'off'}",
        f"schedule: {'built' if state.get('schedule_built') else 'not built'} ({len(state.get('schedule') or [])})",
        f"next slot: {nxt}",
        f"open drops: {sum(1 for drop in (state.get('drops') or {}).values() if drop.get('status') == 'open')}",
        f"players: {len(scores)}",
        f"closed: {str(bool(state.get('closed'))).lower()}",
        f"final: {str(bool(state.get('final_done'))).lower()}",
        f"avatar apply: {str(bool(state.get('avatar_apply_attempted'))).lower()}",
        f"avatar revert: {str(bool(state.get('avatar_revert_attempted'))).lower()}",
        f"now: {now.isoformat()}",
        f"drops end: {settings.drops_end.isoformat()}",
        f"final at: {settings.final_at.isoformat()}",
    ]
    return guard_copy("\n".join(lines))


def lifetime_delta(seconds: int) -> timedelta:
    return timedelta(seconds=int(seconds))
