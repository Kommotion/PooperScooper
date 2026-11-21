"""Backfill MTS weekly stats from Discord history. REST only.

This does not open a gateway, so it will not disconnect the live bot.
Message text is counted in memory and never written. Reaction totals use
``message.reactions`` counts, which can include bot reactions. Reactors are
not marked active and reaction times are not added to the heatmap. Slash
commands, voice, and image generation cannot be recovered from history.

The default cutoff is ``backfill_meta.live_at`` for the guild when that row
exists, otherwise 2026-11-19 10:02:40 America/Los_Angeles (when live tracking
started). Messages at or after the cutoff are left to the live cog.

Rerunning the same window is a no-op for counts: each channel stores the
inclusive snowflake spans already applied. A later, earlier window fetches
only the uncovered gap. Dry-run ignores checkpoints and writes nothing.

Examples:
  python scripts/backfill_weekly_stats.py --days 7 --dry-run
  python scripts/backfill_weekly_stats.py --days 7 --preview C:\\path\\card.png
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord
from discord.http import Route
from discord.utils import parse_time, time_snowflake

from cogs.utils.stats_commands import alias_map_from_source
from cogs.utils.stats_schedule import LA, week_start_for
from cogs.utils.stats_store import BackfillPart, StatsStore

GUILD_ID = 932057681307512922
DEFAULT_CUTOFF = datetime(2026, 11, 19, 10, 2, 40, tzinfo=LA)
DEFAULT_DB = ROOT / "data" / "weekly_stats.sqlite3"
PAGE_SLEEP_S = 0.35
CHANNEL_SLEEP_S = 0.75
TEXT_CHANNEL_TYPES = {0, 5, 15}

# Reaction totals use the count on each message. That count can include bots.
REACTION_COUNTS_INCLUDE_BOTS = True


def _as_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def parse_cutoff(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LA)
    return parsed.astimezone(LA)


def resolve_window(
    *,
    days: int | None,
    since: str | None,
    cutoff: datetime | None,
) -> tuple[datetime, datetime]:
    """Inclusive Pacific start, snapped back to Monday, and the exclusive cutoff."""
    end = _as_aware(cutoff or DEFAULT_CUTOFF).astimezone(LA)
    anchors: list[datetime] = []
    if days is not None:
        anchors.append(end - timedelta(days=int(days)))
    if since:
        day = date.fromisoformat(since)
        anchors.append(datetime(day.year, day.month, day.day, tzinfo=LA))
    if not anchors:
        raise ValueError("pass days or since")
    raw = min(anchors)
    monday = week_start_for(raw)
    start = datetime(monday.year, monday.month, monday.day, tzinfo=LA)
    return start, end


def current_week_start(cutoff: datetime) -> date:
    """Monday of the week still in progress at ``cutoff``."""
    return week_start_for(_as_aware(cutoff) - timedelta(seconds=1))


def classify_message(when: datetime, cutoff: datetime) -> str:
    """``skip``, ``current``, or ``historical``.

    Sunday 21:00 through Monday 00:00 stays on the calendar Monday from
    ``week_start_for``. Nothing at or after the cutoff is classified as current.
    """
    moment = _as_aware(when)
    end = _as_aware(cutoff)
    if moment >= end:
        return "skip"
    current = current_week_start(end)
    week = week_start_for(moment)
    if week == current:
        return "current"
    if week < current:
        return "historical"
    return "skip"


def completed_week_starts(start: datetime, cutoff: datetime) -> list[date]:
    current = current_week_start(cutoff)
    cursor = week_start_for(start)
    weeks: list[date] = []
    while cursor < current:
        weeks.append(cursor)
        cursor += timedelta(days=7)
    return weeks


def window_bounds(start: datetime, cutoff: datetime) -> tuple[int, int]:
    """Exclusive ``after`` snowflake and exclusive ``before`` snowflake."""
    after = time_snowflake(_as_aware(start), high=False) - 1
    before = time_snowflake(_as_aware(cutoff), high=False)
    return after, before


def fetch_gaps(
    spans: list[tuple[int, int]] | tuple[tuple[int, int], ...],
    start: datetime,
    cutoff: datetime,
) -> list[tuple[int, int]]:
    """Uncovered ``(after, before)`` exclusive bounds inside the window.

    Spans are inclusive message ids already applied. Adjacent integers are
    covered; a later window that starts earlier yields only the earlier gap.
    """
    after, before = window_bounds(start, cutoff)
    clipped: list[tuple[int, int]] = []
    for lo, hi in spans:
        lo = int(lo)
        hi = int(hi)
        if hi < lo or hi <= after or lo >= before:
            continue
        clipped.append((max(lo, after + 1), min(hi, before - 1)))
    clipped.sort()
    gaps: list[tuple[int, int]] = []
    cursor = after
    for lo, hi in clipped:
        if lo > cursor + 1:
            gaps.append((cursor, lo))
        if hi > cursor:
            cursor = hi
    if before > cursor + 1:
        gaps.append((cursor, before))
    return gaps


def uncovered_ranges(
    low: int,
    high: int,
    spans: list[tuple[int, int]] | tuple[tuple[int, int], ...],
) -> list[tuple[int, int]]:
    """Inclusive pieces of ``[low, high]`` that no span already covers."""
    if high < low:
        return []
    cursor = low
    pieces: list[tuple[int, int]] = []
    for lo, hi in sorted((int(lo), int(hi)) for lo, hi in spans):
        if hi < cursor:
            continue
        if lo > high:
            break
        if lo > cursor:
            pieces.append((cursor, min(high, lo - 1)))
        if hi + 1 > cursor:
            cursor = hi + 1
        if cursor > high:
            break
    if cursor <= high:
        pieces.append((cursor, high))
    return pieces


def prefix_command_name(raw: str | None) -> str | None:
    """First token after a leading ``!``. The raw string is not retained."""
    if not raw:
        return None
    stripped = raw.lstrip()
    if not stripped.startswith("!"):
        return None
    token = stripped.split(None, 1)[0]
    name = token[1:81]
    if not name:
        return None
    return name


def reaction_parts(payload: dict) -> tuple[str, str, bool, int] | None:
    emoji = payload.get("emoji") or {}
    count = int(payload.get("count") or 0)
    if count <= 0:
        return None
    emoji_id = emoji.get("id")
    if emoji_id:
        key = str(emoji_id)
        name = str(emoji.get("name") or key)
        return key, name, True, count
    name = str(emoji.get("name") or "?")
    return name, name, False, count


def message_created_at(payload: dict) -> datetime:
    parsed = parse_time(payload.get("timestamp"))
    if parsed is None:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class _WeekAcc:
    messages: int = 0
    reactions: dict[str, list] = field(default_factory=dict)
    commands: dict[str, int] = field(default_factory=dict)
    heatmap: dict[tuple[int, int], int] = field(default_factory=dict)
    authors: set[int] = field(default_factory=set)


@dataclass
class WindowTally:
    messages: int = 0
    channels: set[int] = field(default_factory=set)
    reactions: int = 0
    custom_reactions: int = 0
    commands: int = 0
    authors: set[int] = field(default_factory=set)
    heatmap: int = 0
    requests: int = 0
    limited: int = 0


def _author_is_bot(payload: dict) -> bool:
    author = payload.get("author") or {}
    if author.get("bot") or author.get("system"):
        return True
    if payload.get("webhook_id"):
        return True
    return False


def _tally_message(
    payload: dict,
    parent_id: int,
    cutoff: datetime,
    opted: set[int],
    tally: WindowTally,
    weeks: dict[date, _WeekAcc],
    command_map: dict[str, str] | None = None,
) -> str:
    """Fold one history message into week buckets. Returns the classification.

    Bot authors and messages at or after the cutoff return ``skip`` and are not
    counted. The body is read only for a prefix name and is not retained.
    """
    when = message_created_at(payload)
    kind = classify_message(when, cutoff)
    if kind == "skip" or _author_is_bot(payload):
        return "skip"
    week = week_start_for(when)
    bucket = weeks.setdefault(week, _WeekAcc())
    bucket.messages += 1
    tally.messages += 1
    tally.channels.add(int(parent_id))
    tally.heatmap += 1
    local = when.astimezone(LA)
    heat_key = (local.weekday(), local.hour)
    bucket.heatmap[heat_key] = bucket.heatmap.get(heat_key, 0) + 1
    author = payload.get("author") or {}
    author_id = author.get("id")
    if author_id is not None:
        user_id = int(author_id)
        if user_id not in opted:
            bucket.authors.add(user_id)
            tally.authors.add(user_id)
    command = prefix_command_name(payload.get("content"))
    if command and command_map is not None:
        command = command_map.get(command.casefold())
    if command:
        bucket.commands[command] = bucket.commands.get(command, 0) + 1
        tally.commands += 1
    for raw_reaction in payload.get("reactions") or ():
        parsed = reaction_parts(raw_reaction)
        if parsed is None:
            continue
        key, name, is_custom, count = parsed
        slot = bucket.reactions.get(key)
        if slot is None:
            bucket.reactions[key] = [count, name, is_custom]
        else:
            slot[0] += count
            slot[1] = name
            slot[2] = is_custom
        tally.reactions += count
        if is_custom:
            tally.custom_reactions += count
    return kind


def _parts_from_weeks(
    weeks: dict[date, _WeekAcc],
    cutoff: datetime,
    seen_historical: dict[date, set[int]],
) -> list[BackfillPart]:
    current = current_week_start(cutoff)
    parts: list[BackfillPart] = []
    for week, bucket in weeks.items():
        into_current = week == current
        active_ids: tuple[int, ...] = ()
        member_delta = 0
        if into_current:
            active_ids = tuple(sorted(bucket.authors))
        else:
            seen = seen_historical.setdefault(week, set())
            fresh = bucket.authors - seen
            seen.update(fresh)
            member_delta = len(fresh)
        reactions = tuple(
            (key, slot[1], bool(slot[2]), int(slot[0]))
            for key, slot in sorted(bucket.reactions.items())
        )
        commands = tuple(sorted(bucket.commands.items()))
        heatmap = tuple((day, hour, count) for (day, hour), count in sorted(bucket.heatmap.items()))
        parts.append(
            BackfillPart(
                into_current_week=into_current,
                week_start=week,
                messages=bucket.messages,
                reactions=reactions,
                commands=commands,
                heatmap=heatmap,
                active_ids=active_ids,
                member_delta=member_delta,
            )
        )
    return parts


def _backup_db(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = Path(tempfile.gettempdir()) / f"pre_backfill_{stamp}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "weekly_stats.sqlite3"
    source = sqlite3.connect(path, timeout=30)
    copied = sqlite3.connect(dest)
    try:
        source.backup(copied)
    finally:
        copied.close()
        source.close()
    return dest


def _read_live_at(path: Path, guild_id: int) -> datetime | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(path, timeout=30)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'backfill_meta'"
        ).fetchone()
        if not found:
            return None
        row = conn.execute(
            "SELECT live_at FROM backfill_meta WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    return parse_cutoff(str(row[0]))


def _read_opt_outs(path: Path, guild_id: int) -> set[int]:
    if not path.exists():
        return set()
    conn = sqlite3.connect(path, timeout=30)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'opt_outs'"
        ).fetchone()
        if not found:
            return set()
        rows = conn.execute(
            "SELECT user_id FROM opt_outs WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        conn.close()
    return {int(row[0]) for row in rows}


def _print_tally(tally: WindowTally, elapsed_s: float, *, dry_run: bool) -> None:
    label = "dry-run window" if dry_run else "applied"
    print(f"{label} messages: {tally.messages}")
    print(f"channels with messages: {len(tally.channels)}")
    print(f"reaction total: {tally.reactions}")
    print(f"custom-emoji reaction total: {tally.custom_reactions}")
    print(f"prefix-command total: {tally.commands}")
    print(f"active-member count: {len(tally.authors)}")
    print(f"heatmap message count: {tally.heatmap}")
    print(f"request count: {tally.requests}")
    print(f"429 count: {tally.limited}")
    print(f"wall seconds: {elapsed_s:.1f}")
    if dry_run:
        print("dry-run wrote nothing; checkpoints were not updated")
    print(
        "reaction counts include every reaction on the message, including bots; "
        "reactors were not marked active"
    )


async def _request_trace(tally: WindowTally):
    import aiohttp

    trace = aiohttp.TraceConfig()

    async def on_end(_session, _context, params):
        tally.requests += 1
        if getattr(params.response, "status", None) == 429:
            tally.limited += 1

    trace.on_request_end.append(on_end)
    return trace


async def _archive(http, method, channel_id: int, tally_sleep: bool) -> list[dict]:
    found: list[dict] = []
    before = None
    while True:
        try:
            payload = await method(channel_id, before=before, limit=100)
        except (discord.Forbidden, discord.NotFound):
            return found
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) in {403, 404}:
                return found
            raise
        batch = list(payload.get("threads") or [])
        found.extend(batch)
        if not payload.get("has_more") or not batch:
            return found
        meta = batch[-1].get("thread_metadata") or {}
        before = meta.get("archive_timestamp")
        if not before:
            return found
        if tally_sleep:
            await asyncio.sleep(PAGE_SLEEP_S)


async def _targets(http, guild_id: int) -> tuple[list[tuple[int, int]], dict[int, str], dict[str, str], str]:
    channels = await http.get_all_guild_channels(guild_id)
    names: dict[int, str] = {}
    parents: list[dict] = []
    for channel in channels:
        channel_id = int(channel["id"])
        names[channel_id] = str(channel.get("name") or channel_id)
        if int(channel.get("type", -1)) in TEXT_CHANNEL_TYPES:
            parents.append(channel)
    threads: dict[int, dict] = {}
    try:
        active = await http.get_active_threads(guild_id)
    except (discord.Forbidden, discord.NotFound):
        active = {}
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) not in {403, 404}:
            raise
        active = {}
    for thread in active.get("threads") or []:
        threads[int(thread["id"])] = thread
    for parent in parents:
        parent_id = int(parent["id"])
        for method in (
            http.get_public_archived_threads,
            http.get_private_archived_threads,
            http.get_joined_private_archived_threads,
        ):
            for thread in await _archive(http, method, parent_id, True):
                threads[int(thread["id"])] = thread
            await asyncio.sleep(PAGE_SLEEP_S)
    targets: list[tuple[int, int]] = [(int(parent["id"]), int(parent["id"])) for parent in parents]
    for thread in threads.values():
        parent_id = thread.get("parent_id")
        if parent_id is None:
            continue
        targets.append((int(thread["id"]), int(parent_id)))
    emoji_names: dict[str, str] = {}
    try:
        emojis = await http.request(Route("GET", "/guilds/{guild_id}/emojis", guild_id=guild_id))
    except (discord.Forbidden, discord.NotFound):
        emojis = []
    for emoji in emojis or []:
        emoji_names[str(emoji.get("id"))] = str(emoji.get("name") or emoji.get("id"))
    guild_name = str(guild_id)
    try:
        guild = await http.request(Route("GET", "/guilds/{guild_id}", guild_id=guild_id))
        if guild.get("name"):
            guild_name = str(guild["name"])
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass
    return targets, names, emoji_names, guild_name


class _Unreadable(Exception):
    """The channel history endpoint returned 403 or 404."""


def _id_in_ranges(message_id: int, pieces: list[tuple[int, int]]) -> bool:
    return any(lo <= message_id <= hi for lo, hi in pieces)


async def _history(http, channel_id: int, after: int, before: int):
    """Yield ``(messages, exhausted)`` for ids in ``(after, before)``.

    Pages run oldest to newest. ``exhausted`` is true on the last page of a
    readable channel, including a channel with no rows. 403 and 404 raise
    ``_Unreadable`` so the caller does not mark the gap as done.
    """
    cursor = after
    while True:
        try:
            batch = await http.logs_from(channel_id, 100, after=cursor)
        except (discord.Forbidden, discord.NotFound):
            raise _Unreadable from None
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) in {403, 404}:
                raise _Unreadable from None
            raise
        await asyncio.sleep(PAGE_SLEEP_S)
        if not batch:
            yield [], True
            return
        ordered = sorted(batch, key=lambda item: int(item["id"]))
        kept = []
        stop = False
        for msg in ordered:
            message_id = int(msg["id"])
            if message_id >= before:
                stop = True
                break
            kept.append(msg)
        exhausted = stop or len(batch) < 100
        yield kept, exhausted
        if exhausted:
            return
        page_high = max(int(item["id"]) for item in batch)
        if page_high <= cursor:
            yield [], True
            return
        cursor = page_high


async def _walk(http, store: StatsStore | None, args, tally: WindowTally, start: datetime, cutoff: datetime) -> tuple[dict[int, str], dict[str, str], str]:
    targets, names, emoji_names, guild_name = await _targets(http, args.guild_id)
    command_map = alias_map_from_source(ROOT)
    opted = _read_opt_outs(Path(args.db), args.guild_id)
    seen_historical: dict[date, set[int]] = {}
    for index, (channel_id, parent_id) in enumerate(targets):
        spans = [] if store is None else store.backfill_spans(args.guild_id, channel_id)
        gaps = fetch_gaps(spans, start, cutoff)
        for after, before in gaps:
            finished = False
            try:
                async for kept, exhausted in _history(http, channel_id, after, before):
                    finished = exhausted
                    if not kept:
                        continue
                    id_lo = int(kept[0]["id"])
                    id_hi = int(kept[-1]["id"])
                    if store is None:
                        pieces = [(id_lo, id_hi)]
                    else:
                        pieces = uncovered_ranges(id_lo, id_hi, store.backfill_spans(args.guild_id, channel_id))
                    weeks: dict[date, _WeekAcc] = {}
                    counted_ids: list[int] = []
                    for msg in kept:
                        message_id = int(msg["id"])
                        if not _id_in_ranges(message_id, pieces):
                            continue
                        _tally_message(msg, parent_id, cutoff, opted, tally, weeks, command_map)
                        counted_ids.append(message_id)
                    if store is not None and counted_ids:
                        parts = _parts_from_weeks(weeks, cutoff, seen_historical)
                        store.apply_backfill(
                            args.guild_id,
                            channel_id,
                            (min(counted_ids), max(counted_ids)),
                            parts,
                            closed_at=cutoff,
                            command_map=command_map,
                        )
            except _Unreadable:
                finished = False
            if finished and store is not None:
                gap_low = after + 1
                gap_high = before - 1
                if gap_low <= gap_high:
                    store.apply_backfill(
                        args.guild_id,
                        channel_id,
                        (gap_low, gap_high),
                        [],
                        closed_at=cutoff,
                        command_map=command_map,
                    )
        if index + 1 < len(targets):
            await asyncio.sleep(CHANNEL_SLEEP_S)
    if store is not None:
        for week in completed_week_starts(start, cutoff):
            store.write_historical_snapshot(args.guild_id, week, cutoff)
    return names, emoji_names, guild_name


async def _render_preview(db: Path, guild_id: int, path: Path, names: dict[int, str], emoji_names: dict[str, str], guild_name: str) -> None:
    from cogs.utils.emoji_cache import prefetch
    from cogs.utils.stats_card import CardNames, render_stats_card

    store = StatsStore(db, busy_timeout_ms=30000)
    try:
        now = datetime.now(LA)
        known_channels = list(names) if names else None
        known_emojis = list(emoji_names.items()) if emoji_names else None
        stats = store.build_stats(
            guild_id,
            now,
            known_channel_ids=known_channels,
            known_custom_emojis=known_emojis,
        )
        custom_ids = [
            row.emoji_key
            for row in (*stats.top_reactions, *stats.least_custom_emojis)
            if getattr(row, "is_custom", False)
        ]
        await prefetch(custom_ids)
        image = render_stats_card(
            stats,
            CardNames(dict(names), {}, dict(emoji_names)),
            guild_name,
            watermark="PREVIEW",
        )
    finally:
        store.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
    print(f"preview: {path}")


async def _async_main(args) -> int:
    started = time.monotonic()
    tally = WindowTally()
    db_path = Path(args.db)
    cutoff = parse_cutoff(args.cutoff)
    if cutoff is None:
        cutoff = _read_live_at(db_path, args.guild_id) or DEFAULT_CUTOFF
    walking = args.days is not None or args.since is not None
    if args.dry_run and not walking:
        print("dry-run needs --days or --since", file=sys.stderr)
        return 2
    names: dict[int, str] = {}
    emoji_names: dict[str, str] = {}
    guild_name = str(args.guild_id)
    store = None
    if walking and not args.dry_run:
        backup = _backup_db(db_path)
        if backup is not None:
            print(f"backup: {backup}")
        store = StatsStore(db_path, busy_timeout_ms=30000)
    need_http = walking or args.preview
    http = None
    try:
        if need_http:
            from cogs.utils.config import load_config

            token = load_config().token.get_secret_value()
            trace = await _request_trace(tally)
            http = discord.http.HTTPClient(asyncio.get_running_loop(), http_trace=trace)
            await http.static_login(token)
            token = ""
        if walking:
            start, cutoff = resolve_window(days=args.days, since=args.since, cutoff=cutoff)
            print(f"window start: {start.isoformat()}")
            print(f"window end: {cutoff.isoformat()}")
            names, emoji_names, guild_name = await _walk(http, store, args, tally, start, cutoff)
        elif args.preview and http is not None:
            names, emoji_names, guild_name = await _names_only(http, args.guild_id)
    finally:
        if store is not None:
            store.close()
        if http is not None:
            await http.close()
    elapsed = time.monotonic() - started
    if walking:
        _print_tally(tally, elapsed, dry_run=bool(args.dry_run))
    if args.preview and not args.dry_run:
        await _render_preview(db_path, args.guild_id, Path(args.preview), names, emoji_names, guild_name)
    elif args.preview and args.dry_run:
        print("preview skipped on dry-run")
    return 0


async def _names_only(http, guild_id: int):
    _targets_result = await _targets(http, guild_id)
    return _targets_result[1], _targets_result[2], _targets_result[3]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill weekly stats from Discord history (REST only).")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--since", default=None, help="Pacific YYYY-MM-DD, snapped back to that Monday")
    parser.add_argument("--cutoff", default=None, help="Aware ISO datetime. Default: recorded live start, else 2026-11-19 10:02:40-08:00")
    parser.add_argument("--guild-id", type=int, default=GUILD_ID)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--preview", default=None, help="Write a design A PNG. Does not post it.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.days is not None and args.days < 1:
        print("--days must be >= 1", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_async_main(args))
    except discord.LoginFailure:
        print("Discord login failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
