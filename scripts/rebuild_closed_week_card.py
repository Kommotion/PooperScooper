"""Rebuild the closed-week stats card for MTS week 2026-11-16. REST only.

Does not open a gateway and does not add to all-time counters. Message text
is not stored. Image-gen and voice totals are copied from the thin snapshot
for that week. Voice per-channel rows may be absent.

  python scripts/rebuild_closed_week_card.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord

from cogs.utils.stats_commands import alias_map_from_source, is_owner_only_name
from cogs.utils.stats_schedule import LA
from cogs.utils.stats_store import (
    StatsStore,
    _heatmap,
    _inject_registered_prefix_rows,
    _least_custom,
    _merge_command_rows,
    _split_channels,
    _split_commands,
    _top_reactions,
)
from scripts.backfill_weekly_stats import (
    CHANNEL_SLEEP_S,
    _Unreadable,
    _archive,
    _author_is_bot,
    _backup_db,
    _history,
    _request_trace,
    _targets,
    message_created_at,
    prefix_command_name,
    reaction_parts,
    window_bounds,
)

MTS_GUILD_ID = 932057681307512922
TEST_GUILD_ID = 1045144253627637830
WEEK = date(2026, 11, 16)
WINDOW_START = datetime(2026, 11, 16, 0, 0, tzinfo=LA)
FALLBACK_END = datetime(2026, 11, 22, 21, 0, 21, tzinfo=LA)
DEFAULT_DB = ROOT / "data" / "weekly_stats.sqlite3"


def guild_allowed(guild_id: int) -> bool:
    return int(guild_id) == MTS_GUILD_ID and int(guild_id) != TEST_GUILD_ID


def message_in_closed_window(when: datetime, start: datetime, end: datetime) -> bool:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return start <= when <= end


def read_closed_end(path: Path, guild_id: int, week: date) -> tuple[datetime, dict]:
    """Snapshot close time and the thin summary. Fallback end when the row is missing."""
    if not path.exists():
        return FALLBACK_END, {}
    conn = sqlite3.connect(path, timeout=30)
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'weekly_snapshots'"
        ).fetchone()
        if not found:
            return FALLBACK_END, {}
        row = conn.execute(
            """
            SELECT closed_at, summary_json FROM weekly_snapshots
            WHERE guild_id = ? AND week_start = ?
            """,
            (int(guild_id), week.isoformat()),
        ).fetchone()
    except sqlite3.Error:
        return FALLBACK_END, {}
    finally:
        conn.close()
    if not row or not row[0]:
        return FALLBACK_END, {}
    parsed = datetime.fromisoformat(str(row[0]))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        summary = json.loads(row[1] or "{}")
    except json.JSONDecodeError:
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    return parsed, summary


def _empty_image() -> dict:
    return {
        "models": [],
        "loras": [],
        "edit_modes": [],
        "resolutions": [],
        "steps": [],
        "samplers": [],
        "avg_seconds": None,
        "success_rate": None,
        "error_rate": None,
        "success_count": 0,
        "error_count": 0,
        "regenerate_count": 0,
        "edit_count": 0,
        "kept_first": 0,
        "generation_count": 0,
    }


def overlay_snapshot_totals(payload: dict, summary: dict) -> dict:
    """Copy voice minutes and image-gen totals from the thin snapshot."""
    payload["voice_minutes"] = int(summary.get("voice_minutes") or 0)
    payload["top_voice_channels"] = []
    payload["voice_heatmap"] = [[0 for _ in range(24)] for _ in range(7)]
    success = int(summary.get("imagegen_success") or 0)
    error = int(summary.get("imagegen_error") or 0)
    image = _empty_image()
    image["success_count"] = success
    image["error_count"] = error
    attempts = success + error
    image["generation_count"] = attempts
    image["success_rate"] = (success / attempts) if attempts else None
    image["error_rate"] = (error / attempts) if attempts else None
    image["kept_first"] = success
    payload["imagegen"] = image
    if "active_members" in summary and not payload.get("active_members"):
        payload["active_members"] = int(summary.get("active_members") or 0)
    return payload


def card_from_tallies(
    channels: dict[int, int],
    reactions: dict[str, list],
    commands: dict[str, int],
    heatmap: dict[tuple[int, int], int],
    authors: set[int],
    known_channels: list[int] | None,
    known_emojis: list[tuple[str, str]] | None,
    command_map: dict[str, str],
) -> dict:
    channel_rows = [
        {"channel_id": channel_id, "weekly": count, "all_time": count}
        for channel_id, count in channels.items()
    ]
    top_channels, bottom_channels, dead_channels = _split_channels(channel_rows, known_channels)
    reaction_rows = [
        {
            "emoji_key": key,
            "emoji_name": slot[1],
            "is_custom": bool(slot[2]),
            "weekly": int(slot[0]),
            "all_time": int(slot[0]),
        }
        for key, slot in reactions.items()
    ]
    command_rows = [
        {"command_name": name, "kind": "prefix", "weekly": count, "all_time": count}
        for name, count in commands.items()
        if count > 0 and not is_owner_only_name(name)
    ]
    command_rows = _merge_command_rows(_inject_registered_prefix_rows(command_rows, command_map), command_map)
    command_rows = [row for row in command_rows if not is_owner_only_name(str(row["command_name"]))]
    top_commands, bottom_commands = _split_commands(command_rows)
    heat_rows = [
        {"weekday": day, "hour": hour, "weekly": count}
        for (day, hour), count in heatmap.items()
    ]
    top_reactions = _top_reactions(reaction_rows)
    least_weekly = _least_custom(reaction_rows, known_emojis, metric="weekly")

    def channels_out(rows) -> list[dict]:
        return [
            {"id": int(row["channel_id"]), "weekly": int(row["weekly"]), "all_time": int(row["all_time"])}
            for row in rows
        ]

    def reactions_out(rows) -> list[dict]:
        return [
            {
                "key": row.emoji_key,
                "name": row.name,
                "is_custom": bool(row.is_custom),
                "weekly": int(row.weekly),
                "all_time": int(row.all_time),
            }
            for row in rows
        ]

    def commands_out(rows) -> list[dict]:
        return [
            {"name": row.name, "kind": row.kind, "weekly": int(row.weekly), "all_time": int(row.all_time)}
            for row in rows
        ]

    heat = _heatmap(heat_rows)
    return {
        "messages": sum(channels.values()),
        "reactions": sum(int(slot[0]) for slot in reactions.values()),
        "commands": sum(int(row["weekly"]) for row in command_rows),
        "active_members": len(authors),
        "voice_minutes": 0,
        "top_channels": channels_out(top_channels),
        "bottom_channels": channels_out(bottom_channels),
        "dead_channels": channels_out(dead_channels),
        "top_reactions": reactions_out(top_reactions),
        "least_custom_emojis": reactions_out(least_weekly),
        "least_custom_emojis_weekly": reactions_out(least_weekly),
        "top_commands": commands_out(top_commands),
        "bottom_commands": commands_out(bottom_commands),
        "command_rows": commands_out(top_commands) + commands_out(bottom_commands),
        "heatmap": [list(row) for row in heat],
        "voice_heatmap": [[0 for _ in range(24)] for _ in range(7)],
        "top_voice_channels": [],
        "imagegen": _empty_image(),
    }


def _fold_message(payload: dict, parent_id: int, end: datetime, command_map: dict[str, str], acc: dict) -> bool:
    when = message_created_at(payload)
    if not message_in_closed_window(when, WINDOW_START, end) or _author_is_bot(payload):
        return False
    acc["channels"][parent_id] = acc["channels"].get(parent_id, 0) + 1
    local = when.astimezone(LA)
    heat_key = (local.weekday(), local.hour)
    acc["heatmap"][heat_key] = acc["heatmap"].get(heat_key, 0) + 1
    author = payload.get("author") or {}
    author_id = author.get("id")
    if author_id is not None:
        acc["authors"].add(int(author_id))
    command = prefix_command_name(payload.get("content"))
    if command:
        command = command_map.get(command.casefold())
    if command and not is_owner_only_name(command):
        acc["commands"][command] = acc["commands"].get(command, 0) + 1
    for raw in payload.get("reactions") or ():
        parsed = reaction_parts(raw)
        if parsed is None:
            continue
        key, name, is_custom, count = parsed
        slot = acc["reactions"].get(key)
        if slot is None:
            acc["reactions"][key] = [count, name, is_custom]
        else:
            slot[0] += count
            slot[1] = name
            slot[2] = is_custom
    return True


async def _walk(http, guild_id: int, end: datetime, tally: dict) -> tuple[dict, list[int], list[tuple[str, str]]]:
    targets, names, emoji_names, _guild_name = await _targets(http, guild_id)
    command_map = alias_map_from_source(ROOT)
    acc = {"channels": {}, "reactions": {}, "commands": {}, "heatmap": {}, "authors": set()}
    after, before = window_bounds(WINDOW_START, end + __import__("datetime").timedelta(seconds=1))
    for index, (channel_id, parent_id) in enumerate(targets):
        try:
            async for kept, _exhausted in _history(http, channel_id, after, before):
                for msg in kept:
                    if _fold_message(msg, parent_id, end, command_map, acc):
                        tally["messages"] += 1
                tally["requests"] += 0
        except _Unreadable:
            pass
        if index + 1 < len(targets):
            await asyncio.sleep(CHANNEL_SLEEP_S)
    known_channels = list(names) if names else None
    known_emojis = list(emoji_names.items()) if emoji_names else None
    payload = card_from_tallies(
        acc["channels"],
        acc["reactions"],
        acc["commands"],
        acc["heatmap"],
        acc["authors"],
        known_channels,
        known_emojis,
        command_map,
    )
    tally["reactions"] = payload["reactions"]
    tally["commands"] = payload["commands"]
    tally["authors"] = payload["active_members"]
    return payload, known_channels or [], known_emojis or []


def _print_tally(tally: dict, elapsed_s: float, *, dry_run: bool) -> None:
    label = "dry-run" if dry_run else "wrote card"
    print(f"{label} messages: {tally['messages']}")
    print(f"reactions: {tally['reactions']}")
    print(f"prefix commands: {tally['commands']}")
    print(f"active members: {tally['authors']}")
    print(f"request count: {tally['requests']}")
    print(f"429 count: {tally['limited']}")
    print(f"wall seconds: {elapsed_s:.1f}")


async def _async_main(args) -> int:
    if not guild_allowed(args.guild_id):
        print("This script only walks the MTS guild.", file=sys.stderr)
        return 2
    started = time.monotonic()
    db_path = Path(args.db)
    end, summary = read_closed_end(db_path, args.guild_id, WEEK)
    tally = {"messages": 0, "reactions": 0, "commands": 0, "authors": 0, "requests": 0, "limited": 0}
    from cogs.utils.config import load_config

    token = load_config().token.get_secret_value()
    trace = await _request_trace(type("T", (), tally)()) if False else None
    # WindowTally-shaped object for the shared trace helper.
    from scripts.backfill_weekly_stats import WindowTally

    window = WindowTally()
    trace = await _request_trace(window)
    http = discord.http.HTTPClient(asyncio.get_running_loop(), http_trace=trace)
    try:
        await http.static_login(token)
        token = ""
        payload, _channels, _emojis = await _walk(http, args.guild_id, end, tally)
        payload = overlay_snapshot_totals(payload, summary)
    finally:
        await http.close()
    tally["requests"] = window.requests
    tally["limited"] = window.limited
    if args.dry_run:
        _print_tally(tally, time.monotonic() - started, dry_run=True)
        print("dry-run wrote nothing")
        return 0
    backup = _backup_db(db_path)
    if backup is not None:
        print(f"backup: {backup}")
    store = StatsStore(db_path, busy_timeout_ms=30000)
    try:
        store.write_weekly_card_state(args.guild_id, WEEK, payload)
    finally:
        store.close()
    _print_tally(tally, time.monotonic() - started, dry_run=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rebuild the MTS closed-week card for 2026-11-16 (REST only).")
    parser.add_argument("--guild-id", type=int, default=MTS_GUILD_ID)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_async_main(args))
    except discord.LoginFailure:
        print("Discord login failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
