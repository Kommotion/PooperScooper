"""Recount MTS image-gen model labels and replace all-time model counts.

REST only. Does not open a gateway. Message text is not stored or printed.
Generation and outcome totals, and every weekly column, stay unchanged.
The same mapping written twice is a no-op.

  python scripts/reclassify_imagegen_models.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord
from discord.utils import time_snowflake

from cogs.utils.stats_store import StatsStore
from scripts.backfill_imagegen_history import (
    IMAGE_CHANNEL_ID,
    MTS_GUILD_ID,
    guild_allowed,
    image_decision,
    model_from_caption,
)
from scripts.backfill_weekly_stats import (
    PAGE_SLEEP_S,
    _Unreadable,
    _backup_db,
    _history,
    _request_trace,
)
from scripts.backfill_weekly_stats import WindowTally

DEFAULT_DB = ROOT / "data" / "weekly_stats.sqlite3"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replace MTS all-time image-gen model counts (REST only).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--guild-id", type=int, default=MTS_GUILD_ID)
    return parser


def _read_models(db_path: Path, guild_id: int) -> dict[str, int]:
    if not db_path.is_file():
        return {}
    store = StatsStore(db_path, busy_timeout_ms=30000)
    try:
        rows = store._conn.execute(
            """
            SELECT item_key, all_time
            FROM imagegen_counts
            WHERE guild_id = ? AND dimension = 'model' AND all_time > 0
            """,
            (int(guild_id),),
        ).fetchall()
    finally:
        store.close()
    return {str(row["item_key"]): int(row["all_time"]) for row in rows}


def _format_hist(counts: dict[str, int]) -> str:
    if not counts:
        return "(none)"
    return " ".join(f"{key} {counts[key]}" for key in sorted(counts))


async def _recount(http) -> dict[str, int]:
    models: dict[str, int] = {}
    before = time_snowflake(datetime.now(timezone.utc), high=False)
    await asyncio.sleep(PAGE_SLEEP_S)
    try:
        async for kept, _exhausted in _history(http, IMAGE_CHANNEL_ID, 0, before):
            for msg in kept:
                if image_decision(msg) == "skip":
                    continue
                model = model_from_caption(str(msg.get("content") or ""))
                models[model] = models.get(model, 0) + 1
    except _Unreadable:
        pass
    await asyncio.sleep(PAGE_SLEEP_S)
    return models


def _print_result(previous: dict[str, int], found: dict[str, int], tally: WindowTally, elapsed: float, *, dry_run: bool) -> None:
    moved = max(0, int(previous.get("unknown", 0)) - int(found.get("unknown", 0)))
    print("previous: " + _format_hist(previous))
    print("new: " + _format_hist(found))
    print(f"moved off unknown: {moved}")
    print(f"request count: {tally.requests}")
    print(f"429 count: {tally.limited}")
    print(f"wall seconds: {elapsed:.1f}")
    if dry_run:
        print("dry-run wrote nothing")


async def _async_main(args) -> int:
    if not guild_allowed(args.guild_id):
        print("Refusing to run: guild is not MTS.", file=sys.stderr)
        return 2
    started = time.monotonic()
    tally = WindowTally()
    db_path = Path(args.db)
    guild_id = int(args.guild_id)
    previous = _read_models(db_path, guild_id)
    http = None
    try:
        if not args.dry_run:
            backup = _backup_db(db_path)
            if backup is not None:
                print(f"backup: {backup}")
        from cogs.utils.config import load_config

        token = load_config().token.get_secret_value()
        trace = await _request_trace(tally)
        http = discord.http.HTTPClient(asyncio.get_running_loop(), http_trace=trace)
        await http.static_login(token)
        token = ""
        found = await _recount(http)
        if not args.dry_run:
            store = StatsStore(db_path, busy_timeout_ms=30000)
            try:
                store.replace_all_time_imagegen_models(guild_id, found)
            finally:
                store.close()
    finally:
        if http is not None:
            await http.close()
    _print_result(previous, found, tally, time.monotonic() - started, dry_run=bool(args.dry_run))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not guild_allowed(args.guild_id):
        print("Refusing to run: guild is not MTS.", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_async_main(args))
    except discord.LoginFailure:
        print("Discord login failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
