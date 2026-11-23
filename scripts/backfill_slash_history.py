"""Backfill historical MTS slash commands into all-time stats.

REST only. Does not open a gateway. Message text is not stored or printed.
Weekly columns stay unchanged. The same message id is a no-op. Channel spans
already covered are not fetched again.

  python scripts/backfill_slash_history.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord

from cogs.utils.stats_commands import is_owner_only_name
from cogs.utils.stats_schedule import LA, week_start_for
from cogs.utils.stats_store import StatsStore
from scripts.backfill_weekly_stats import (
    CHANNEL_SLEEP_S,
    _Unreadable,
    _backup_db,
    _history,
    _request_trace,
    _targets,
    fetch_gaps,
    message_created_at,
    uncovered_ranges,
    window_bounds,
)
from scripts.backfill_weekly_stats import WindowTally

MTS_GUILD_ID = 932057681307512922
TEST_GUILD_ID = 1045144253627637830
DEFAULT_SINCE = date(2022, 1, 15)
DEFAULT_DB = ROOT / "data" / "weekly_stats.sqlite3"
_SPAN_SQL = """
CREATE TABLE IF NOT EXISTS slash_history_span (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    low_id INTEGER NOT NULL,
    high_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id, low_id)
)
"""


def guild_allowed(guild_id: int) -> bool:
    return int(guild_id) == MTS_GUILD_ID and int(guild_id) != TEST_GUILD_ID


def normalize_slash_name(name: str) -> str:
    label = " ".join(str(name or "").split())
    if label == "imagegen":
        return "imagegen generate"
    return label


def slash_command_name(payload: dict) -> str | None:
    """Qualified slash name, or None when this message is not an app command.

    Type 5 (component) is ignored. A bare imagegen name becomes imagegen generate.
    """
    blocks = []
    for key in ("interaction", "interaction_metadata"):
        block = payload.get(key)
        if isinstance(block, dict):
            blocks.append(block)
    chosen = None
    for block in blocks:
        try:
            kind = int(block.get("type"))
        except (TypeError, ValueError):
            continue
        if kind == 5:
            continue
        if kind == 2:
            chosen = block
            break
    if chosen is None:
        return None
    if isinstance(chosen.get("qualified_name"), str) and chosen["qualified_name"].strip():
        return normalize_slash_name(chosen["qualified_name"])
    parts: list[str] = []
    root = str(chosen.get("name") or "").strip()
    if root:
        parts.append(root)
    options = chosen.get("options") if isinstance(chosen.get("options"), list) else []
    while options:
        first = options[0]
        if not isinstance(first, dict):
            break
        try:
            kind = int(first.get("type") or 0)
        except (TypeError, ValueError):
            break
        child = str(first.get("name") or "").strip()
        if kind in {1, 2} and child:
            parts.append(child)
            options = first.get("options") if isinstance(first.get("options"), list) else []
            continue
        break
    if not parts:
        return None
    return normalize_slash_name(" ".join(parts))


def _ensure_spans(store: StatsStore) -> None:
    store._conn.execute(_SPAN_SQL)


def _read_spans(store: StatsStore, guild_id: int, channel_id: int) -> list[tuple[int, int]]:
    _ensure_spans(store)
    rows = store._conn.execute(
        """
        SELECT low_id, high_id FROM slash_history_span
        WHERE guild_id = ? AND channel_id = ?
        ORDER BY low_id
        """,
        (int(guild_id), int(channel_id)),
    ).fetchall()
    return [(int(row[0]), int(row[1])) for row in rows]


def _note_span(store: StatsStore, guild_id: int, channel_id: int, low: int, high: int) -> None:
    if high < low:
        return
    _ensure_spans(store)
    spans = _read_spans(store, guild_id, channel_id)
    spans.append((int(low), int(high)))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    store._conn.execute(
        "DELETE FROM slash_history_span WHERE guild_id = ? AND channel_id = ?",
        (int(guild_id), int(channel_id)),
    )
    store._conn.executemany(
        """
        INSERT INTO slash_history_span (guild_id, channel_id, low_id, high_id)
        VALUES (?, ?, ?, ?)
        """,
        [(int(guild_id), int(channel_id), lo, hi) for lo, hi in merged],
    )


def _id_in_ranges(message_id: int, pieces: list[tuple[int, int]]) -> bool:
    return any(lo <= message_id <= hi for lo, hi in pieces)


async def _walk(http, store: StatsStore | None, guild_id: int, start: datetime, cutoff: datetime) -> dict:
    targets, _names, _emoji, _guild = await _targets(http, guild_id)
    added = 0
    counts: dict[str, int] = {}
    earliest: datetime | None = None
    latest: datetime | None = None
    for channel_id, parent_id in targets:
        if int(channel_id) == TEST_GUILD_ID or int(parent_id) == TEST_GUILD_ID:
            continue
        spans = [] if store is None else _read_spans(store, guild_id, channel_id)
        gaps = fetch_gaps(spans, start, cutoff)
        for after, before in gaps:
            finished_through = after
            try:
                async for kept, exhausted in _history(http, channel_id, after, before):
                    if not kept:
                        if exhausted and store is not None:
                            _note_span(store, guild_id, channel_id, after + 1, before - 1)
                        continue
                    id_lo = int(kept[0]["id"])
                    id_hi = int(kept[-1]["id"])
                    if store is None:
                        pieces = [(id_lo, id_hi)]
                    else:
                        pieces = uncovered_ranges(id_lo, id_hi, _read_spans(store, guild_id, channel_id))
                    for msg in kept:
                        message_id = int(msg["id"])
                        if not _id_in_ranges(message_id, pieces):
                            continue
                        label = slash_command_name(msg)
                        if not label:
                            continue
                        created = message_created_at(msg).astimezone(LA)
                        week = week_start_for(created)
                        if earliest is None or created < earliest:
                            earliest = created
                        if latest is None or created > latest:
                            latest = created
                        if store is None:
                            if is_owner_only_name(label):
                                continue
                            added += 1
                            counts[label] = counts.get(label, 0) + 1
                            continue
                        applied = store.apply_history_slash(guild_id, message_id, week, label)
                        if applied and not is_owner_only_name(label):
                            added += 1
                            counts[label] = counts.get(label, 0) + 1
                    if store is not None and pieces:
                        _note_span(store, guild_id, channel_id, id_lo, id_hi)
                    if exhausted:
                        finished_through = before - 1
            except _Unreadable:
                continue
            if store is not None and finished_through > after:
                _note_span(store, guild_id, channel_id, after + 1, finished_through)
        await asyncio.sleep(CHANNEL_SLEEP_S)
    return {"added": added, "counts": counts, "earliest": earliest, "latest": latest}


def _print_result(result: dict, tally: WindowTally, elapsed: float, *, dry_run: bool, start: datetime, cutoff: datetime) -> None:
    label = "dry-run" if dry_run else "applied"
    print(f"{label} added: {result['added']}")
    ranked = sorted(result["counts"].items(), key=lambda item: (-item[1], item[0]))
    print("top commands:")
    for name, count in ranked[:15]:
        print(f"  {name}: {count}")
    earliest = result["earliest"]
    latest = result["latest"]
    print(f"window start: {start.isoformat()}")
    print(f"window end: {cutoff.isoformat()}")
    print(f"earliest: {earliest.isoformat() if earliest else 'n/a'}")
    print(f"latest: {latest.isoformat() if latest else 'n/a'}")
    print(f"request count: {tally.requests}")
    print(f"429 count: {tally.limited}")
    print(f"wall seconds: {elapsed:.1f}")
    if dry_run:
        print("dry-run wrote nothing")


def resolve_start(since: str | None) -> datetime:
    day = date.fromisoformat(since) if since else DEFAULT_SINCE
    monday = week_start_for(datetime(day.year, day.month, day.day, tzinfo=LA))
    return datetime(monday.year, monday.month, monday.day, tzinfo=LA)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill MTS slash-command history (REST only).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--guild-id", type=int, default=MTS_GUILD_ID)
    parser.add_argument("--since", default=None, help="Pacific YYYY-MM-DD, snapped back to that Monday")
    return parser


async def _async_main(args) -> int:
    if not guild_allowed(args.guild_id):
        print("Refusing to run: guild is not MTS.", file=sys.stderr)
        return 2
    started = time.monotonic()
    tally = WindowTally()
    db_path = Path(args.db)
    start = resolve_start(args.since)
    cutoff = datetime.now(LA)
    store = None
    http = None
    try:
        if not args.dry_run:
            backup = _backup_db(db_path)
            if backup is not None:
                print(f"backup: {backup}")
            store = StatsStore(db_path, busy_timeout_ms=30000)
        from cogs.utils.config import load_config

        token = load_config().token.get_secret_value()
        trace = await _request_trace(tally)
        http = discord.http.HTTPClient(asyncio.get_running_loop(), http_trace=trace)
        await http.static_login(token)
        token = ""
        result = await _walk(http, store, int(args.guild_id), start, cutoff)
    finally:
        if store is not None:
            store.close()
        if http is not None:
            await http.close()
    _print_result(result, tally, time.monotonic() - started, dry_run=bool(args.dry_run), start=start, cutoff=cutoff)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not guild_allowed(args.guild_id):
        print("Refusing to run: guild is not MTS.", file=sys.stderr)
        return 2
    try:
        week_start_for(resolve_start(args.since))
        window_bounds(resolve_start(args.since), datetime.now(LA))
    except ValueError:
        print("--since must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_async_main(args))
    except discord.LoginFailure:
        print("Discord login failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
