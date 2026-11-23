"""Backfill historical #image-diffusion successes into MTS all-time stats.

REST only. Does not open a gateway. Message text is not stored or printed.
Weekly columns stay unchanged. The same message id is a no-op.

  python scripts/backfill_imagegen_history.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord
from discord.utils import time_snowflake

from cogs.utils.image_models import Models
from cogs.utils.stats_schedule import LA, week_start_for
from cogs.utils.stats_store import StatsStore
from scripts.backfill_weekly_stats import (
    CHANNEL_SLEEP_S,
    PAGE_SLEEP_S,
    _Unreadable,
    _backup_db,
    _history,
    _request_trace,
    message_created_at,
)
from scripts.backfill_weekly_stats import WindowTally

MTS_GUILD_ID = 932057681307512922
TEST_GUILD_ID = 1045144253627637830
IMAGE_CHANNEL_ID = 1373140173067653120
BOT_ID = 394432249598312459
DEFAULT_DB = ROOT / "data" / "weekly_stats.sqlite3"
_TICK = re.compile(r"`([^`]+)`")
_KNOWN_MODELS = {member.value for member in Models}
_SKIP_FILES = {"halloweenified.png", "original.png"}
_INBOX_MARKERS = ("MTS Party Prompts", "Party Prompt")


def guild_allowed(guild_id: int) -> bool:
    return int(guild_id) == MTS_GUILD_ID and int(guild_id) != TEST_GUILD_ID


def model_from_caption(content: str) -> str:
    """Model key from a backtick token, or ``unknown``. The text is not kept."""
    for token in _TICK.findall(content or ""):
        key = token.strip()
        if key in _KNOWN_MODELS:
            return key
    return "unknown"


def image_decision(payload: dict) -> str:
    """``keep``, ``inbox``, or ``skip``. Does not return message text."""
    author = payload.get("author") or {}
    try:
        author_id = int(author.get("id") or 0)
    except (TypeError, ValueError):
        author_id = 0
    if author_id != BOT_ID:
        return "skip"
    names = []
    for item in payload.get("attachments") or []:
        if isinstance(item, dict):
            names.append(str(item.get("filename") or "").casefold())
    if "generated.png" not in names:
        return "skip"
    if any(name in _SKIP_FILES for name in names) and "generated.png" not in names:
        return "skip"
    content = str(payload.get("content") or "")
    if any(marker in content for marker in _INBOX_MARKERS):
        return "inbox"
    return "keep"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill MTS #image-diffusion history (REST only).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--guild-id", type=int, default=MTS_GUILD_ID)
    return parser


async def _walk(http, store: StatsStore | None, guild_id: int, tally: WindowTally) -> dict:
    added = 0
    skipped_inbox = 0
    skipped_applied = 0
    models: dict[str, int] = {}
    earliest: datetime | None = None
    latest: datetime | None = None
    now = datetime.now(timezone.utc)
    before = time_snowflake(now, high=False)
    await asyncio.sleep(CHANNEL_SLEEP_S)
    try:
        async for kept, _exhausted in _history(http, IMAGE_CHANNEL_ID, 0, before):
            for msg in kept:
                decision = image_decision(msg)
                if decision == "skip":
                    continue
                if decision == "inbox":
                    skipped_inbox += 1
                    continue
                created = message_created_at(msg).astimezone(LA)
                model = model_from_caption(str(msg.get("content") or ""))
                week = week_start_for(created)
                message_id = int(msg["id"])
                if store is None:
                    applied = True
                else:
                    applied = store.apply_history_imagegen(guild_id, message_id, week, model)
                if not applied:
                    skipped_applied += 1
                    continue
                added += 1
                models[model] = models.get(model, 0) + 1
                if earliest is None or created < earliest:
                    earliest = created
                if latest is None or created > latest:
                    latest = created
    except _Unreadable:
        pass
    await asyncio.sleep(PAGE_SLEEP_S)
    return {
        "added": added,
        "skipped_inbox": skipped_inbox,
        "skipped_applied": skipped_applied,
        "models": models,
        "earliest": earliest,
        "latest": latest,
    }


def _print_result(result: dict, tally: WindowTally, elapsed: float, *, dry_run: bool) -> None:
    label = "dry-run" if dry_run else "applied"
    skipped = int(result["skipped_inbox"]) + int(result["skipped_applied"])
    print(f"{label} added: {result['added']}")
    print(f"skipped inbox: {result['skipped_inbox']}")
    print(f"skipped already-applied: {result['skipped_applied']}")
    print(f"skipped: {skipped}")
    print("models: " + " ".join(sorted(result["models"])))
    earliest = result["earliest"]
    latest = result["latest"]
    print(f"earliest: {earliest.isoformat() if earliest else 'n/a'}")
    print(f"latest: {latest.isoformat() if latest else 'n/a'}")
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
        result = await _walk(http, store, int(args.guild_id), tally)
    finally:
        if store is not None:
            store.close()
        if http is not None:
            await http.close()
    _print_result(result, tally, time.monotonic() - started, dry_run=bool(args.dry_run))
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
