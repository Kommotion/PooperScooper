"""Backfill historical MTS slash commands into all-time stats.

REST only. Does not open a gateway. Message text is not stored or printed.
Weekly columns stay unchanged. The same message id is a no-op. Channel spans
already covered are not fetched again. Own and other bots' slash commands
are both counted. Each row keeps the application id and bot name.

  python scripts/backfill_slash_history.py --dry-run
  python scripts/backfill_slash_history.py --retag-bots --db path\\to\\copy.sqlite3

``--retag-bots`` (and ``--purge-foreign``, the same operation) recounts
history without using span coverage, tags slash rows, and does not reset
spans or history claims. It refuses the live default database. Run it
against a copy. It does not subtract counts.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord

from cogs.utils.constants import POOPER_APP_ID
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


def resolve_own_application_id(raw: str | int | None = None) -> int:
    """PooperScooper's Discord application id from ``BotConfig.client_id``.

    Does not read or return the bot token. Raises ValueError when the id is
    missing or not a positive integer.
    """
    if raw is None:
        from cogs.utils.config import load_config

        try:
            raw = load_config().client_id
        except Exception:
            raise ValueError("client_id is missing or not a positive int") from None
    text = str(raw).strip()
    if not text.isdecimal():
        raise ValueError("client_id is missing or not a positive int")
    value = int(text)
    if value <= 0:
        raise ValueError("client_id is missing or not a positive int")
    return value


def payload_application_id(payload: dict) -> int | None:
    """Application id from the interaction blocks or the message.

    Discord's REST message object stores ``application_id`` on the message.
    Payloads may also put it on ``interaction`` or ``interaction_metadata``.
    A missing id, a non-numeric id, or two ids that disagree returns None.
    """
    if not isinstance(payload, dict):
        return None
    blocks = [payload]
    for key in ("interaction", "interaction_metadata"):
        block = payload.get(key)
        if isinstance(block, dict):
            blocks.append(block)
    found: list[int] = []
    for block in blocks:
        if "application_id" not in block:
            continue
        raw = block.get("application_id")
        if raw is None or raw == "":
            continue
        try:
            found.append(int(raw))
        except (TypeError, ValueError):
            return None
    if not found or any(item != found[0] for item in found) or found[0] <= 0:
        return None
    return found[0]


def slash_interaction_name(payload: dict) -> str | None:
    """Qualified slash name, or None when this message is not an app command.

    Type 5 (component) is ignored. A bare imagegen name becomes imagegen generate.
    This helper does not check the application id.
    """
    if not isinstance(payload, dict):
        return None
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


def classify_slash_use(payload: dict, own_application_id: int) -> tuple[str | None, str]:
    """Return ``(name, 'own'|'foreign'|'skip')`` for one history message."""
    name = slash_interaction_name(payload)
    if not name:
        return None, "skip"
    app_id = payload_application_id(payload)
    try:
        own = int(own_application_id)
    except (TypeError, ValueError):
        own = 0
    if app_id is not None and own > 0 and app_id == own:
        return name, "own"
    return name, "foreign"


_BOT_NAMES: dict[int, str] = {}
LIVE_RETAG_ERROR = (
    "Refusing --purge-foreign against the live stats database. Pass --db with a copy."
)


def remember_bot_name(app_id: int, name: str) -> None:
    try:
        key = int(app_id)
    except (TypeError, ValueError):
        return
    label = " ".join(str(name or "").split())
    if key > 0 and label:
        _BOT_NAMES[key] = label


def cached_bot_name(app_id: int) -> str:
    try:
        return _BOT_NAMES.get(int(app_id), "")
    except (TypeError, ValueError):
        return ""


def bot_name_from_payload(payload: dict, application_id: int | None) -> str:
    """Display name from a history message. Never blank and never a token."""
    try:
        app_id = int(application_id) if application_id not in (None, "") else 0
    except (TypeError, ValueError):
        app_id = 0
    if app_id < 0:
        app_id = 0
    author = payload.get("author") if isinstance(payload, dict) else None
    if isinstance(author, dict):
        matches = False
        author_id = author.get("id")
        if app_id > 0 and author_id not in (None, ""):
            try:
                matches = int(author_id) == app_id
            except (TypeError, ValueError):
                matches = False
        if author.get("bot") or matches:
            for key in ("global_name", "username", "name"):
                text = " ".join(str(author.get(key) or "").split())
                if text:
                    remember_bot_name(app_id, text)
                    return text
    cached = cached_bot_name(app_id) if app_id else ""
    return cached or "Unknown bot"


def live_bot_name(author, application_id: int) -> str:
    """Display name for a live slash reply. Never blank."""
    try:
        app_id = int(application_id)
    except (TypeError, ValueError):
        app_id = 0
    if app_id < 0:
        app_id = 0
    if author is not None and getattr(author, "bot", False):
        for value in (
            getattr(author, "display_name", None),
            getattr(author, "global_name", None),
            getattr(author, "name", None),
        ):
            text = " ".join(str(value or "").split())
            if text:
                remember_bot_name(app_id, text)
                return text
    return cached_bot_name(app_id) or "Unknown bot"


def slash_event_name(payload: dict, own_application_id: int | None = None) -> str | None:
    """Slash name for own and foreign uses. None for components and non-commands."""
    try:
        if own_application_id is None:
            own = resolve_own_application_id()
        else:
            own = resolve_own_application_id(own_application_id)
    except ValueError:
        own = 0
    name, bucket = classify_slash_use(payload, own)
    if bucket == "skip" or not name:
        return None
    return name


def slash_command_name(payload: dict, own_application_id: int | None = None) -> str | None:
    """This bot's slash name, or None.

    Type 5 (component) is ignored. A bare imagegen name becomes imagegen generate.
    A missing or mismatched application id returns None. ``own_application_id``
    defaults to ``BotConfig.client_id``.
    """
    try:
        if own_application_id is None:
            own = resolve_own_application_id()
        else:
            own = resolve_own_application_id(own_application_id)
    except ValueError:
        return None
    name, bucket = classify_slash_use(payload, own)
    if bucket != "own":
        return None
    return name


def _is_live_db(path: Path) -> bool:
    return os.path.normcase(str(path.resolve())) == os.path.normcase(str(DEFAULT_DB.resolve()))


def live_retag_refusal(path: Path) -> str | None:
    """Error text when a retag would open the live stats database."""
    if _is_live_db(Path(path)):
        return LIVE_RETAG_ERROR
    return None


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


def _note_classified(
    result: dict,
    name: str,
    bucket: str,
    week: date,
    *,
    app_id: int = 0,
    bot_name: str = "",
) -> None:
    if bucket == "own":
        result["own_uses"] += 1
    else:
        result["foreign_uses"] += 1
    if bucket == "own" and is_owner_only_name(name):
        return
    if bucket == "own":
        result["own"][name] = result["own"].get(name, 0) + 1
        return
    bot = str(bot_name or "").strip() or "Unknown bot"
    try:
        stored_app = int(app_id)
    except (TypeError, ValueError):
        stored_app = 0
    if stored_app < 0:
        stored_app = 0
    key = (name, stored_app, bot)
    tags = result["foreign_tags"]
    tags[key] = tags.get(key, 0) + 1
    result["foreign"][name] = result["foreign"].get(name, 0) + 1
    slot = result["foreign_weeks"].setdefault(week.isoformat(), {})
    slot[name] = slot.get(name, 0) + 1


async def _walk(
    http,
    store: StatsStore | None,
    guild_id: int,
    start: datetime,
    cutoff: datetime,
    *,
    own_application_id: int,
    apply: bool = True,
) -> dict:
    targets, _names, _emoji, _guild = await _targets(http, guild_id)
    added = 0
    counts: dict[str, int] = {}
    earliest: datetime | None = None
    latest: datetime | None = None
    result = {
        "added": 0,
        "counts": counts,
        "earliest": earliest,
        "latest": latest,
        "own_uses": 0,
        "foreign_uses": 0,
        "own": {},
        "foreign": {},
        "foreign_tags": {},
        "foreign_weeks": {},
    }
    for channel_id, parent_id in targets:
        if int(channel_id) == TEST_GUILD_ID or int(parent_id) == TEST_GUILD_ID:
            continue
        # Retag recounts the whole window. Spans stay coverage records and are not read or reset.
        spans = [] if store is None or not apply else _read_spans(store, guild_id, channel_id)
        gaps = fetch_gaps(spans, start, cutoff)
        for after, before in gaps:
            finished_through = after
            try:
                async for kept, exhausted in _history(http, channel_id, after, before):
                    if not kept:
                        if exhausted and apply and store is not None:
                            _note_span(store, guild_id, channel_id, after + 1, before - 1)
                        continue
                    id_lo = int(kept[0]["id"])
                    id_hi = int(kept[-1]["id"])
                    if not apply or store is None:
                        pieces = [(id_lo, id_hi)]
                    else:
                        pieces = uncovered_ranges(id_lo, id_hi, _read_spans(store, guild_id, channel_id))
                    for msg in kept:
                        message_id = int(msg["id"])
                        if not _id_in_ranges(message_id, pieces):
                            continue
                        created = message_created_at(msg).astimezone(LA)
                        week = week_start_for(created)
                        name, bucket = classify_slash_use(msg, own_application_id)
                        if bucket == "skip" or not name:
                            continue
                        app_raw = payload_application_id(msg)
                        try:
                            app_id = int(app_raw) if app_raw is not None else 0
                        except (TypeError, ValueError):
                            app_id = 0
                        if app_id < 0:
                            app_id = 0
                        if earliest is None or created < earliest:
                            earliest = created
                        if latest is None or created > latest:
                            latest = created
                        if not apply:
                            bot_name = "" if bucket == "own" else bot_name_from_payload(msg, app_raw)
                            _note_classified(
                                result,
                                name,
                                bucket,
                                week,
                                app_id=app_id,
                                bot_name=bot_name,
                            )
                            continue
                        bot_name = "" if bucket == "own" else bot_name_from_payload(msg, app_raw)
                        if store is None:
                            if bucket == "own" and is_owner_only_name(name):
                                continue
                            added += 1
                            counts[name] = counts.get(name, 0) + 1
                            continue
                        applied = store.apply_history_slash(
                            guild_id,
                            message_id,
                            week,
                            name,
                            application_id=app_raw,
                            bot_name=bot_name,
                            own_application_id=own_application_id,
                        )
                        if applied and not (bucket == "own" and is_owner_only_name(name)):
                            added += 1
                            counts[name] = counts.get(name, 0) + 1
                    if apply and store is not None and pieces:
                        _note_span(store, guild_id, channel_id, id_lo, id_hi)
                    if exhausted:
                        finished_through = before - 1
            except _Unreadable:
                continue
            if apply and store is not None and finished_through > after:
                _note_span(store, guild_id, channel_id, after + 1, finished_through)
        await asyncio.sleep(CHANNEL_SLEEP_S)
    result["added"] = added
    result["earliest"] = earliest
    result["latest"] = latest
    return result


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


def _command_tally(store: StatsStore, guild_id: int) -> list[tuple]:
    rows = store._conn.execute(
        """
        SELECT command_name, kind, weekly, all_time, app_id, bot_name
        FROM command_counts
        WHERE guild_id = ?
        ORDER BY command_name, kind, app_id
        """,
        (int(guild_id),),
    ).fetchall()
    return [
        (
            str(row["command_name"]),
            str(row["kind"]),
            int(row["weekly"]),
            int(row["all_time"]),
            int(row["app_id"]),
            str(row["bot_name"] or ""),
        )
        for row in rows
    ]


def _print_command_tally(label: str, rows: list[tuple]) -> None:
    print(f"{label}:")
    total = 0
    for name, kind, weekly, all_time, app_id, bot_name in rows:
        suffix = f" {bot_name}" if bot_name else ""
        print(f"  {kind} {name} app {app_id}{suffix}: weekly={weekly} all_time={all_time}")
        total += all_time
    print(f"  all_time sum: {total}")


def _print_retag(store: StatsStore, guild_id: int, result: dict, *, dry_run: bool) -> None:
    print(f"application id: {POOPER_APP_ID}")
    print(f"own-app slash uses: {result['own_uses']}")
    print(f"other-app slash uses: {result['foreign_uses']}")
    ranked = sorted(
        result["foreign_tags"].items(),
        key=lambda item: (-item[1], item[0][0], item[0][1], item[0][2]),
    )
    print("other-app commands:")
    for (name, app_id, bot_name), count in ranked[:30]:
        print(f"  {name} [{app_id}] {bot_name}: {count}")
    _print_command_tally("before", _command_tally(store, guild_id))
    if dry_run:
        print("dry-run wrote nothing")
        return
    applied = store.tag_history_bots(guild_id, result["own"], result["foreign_tags"])
    _print_command_tally("after", _command_tally(store, guild_id))
    if applied:
        print("retag applied: slash_tag_bots_v1")
    else:
        print("retag already applied: slash_tag_bots_v1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill MTS slash-command history (REST only).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--retag-bots",
        action="store_true",
        help="Recount history and tag slash rows with application id and bot name. Refuses the live default database.",
    )
    parser.add_argument(
        "--purge-foreign",
        action="store_true",
        help="Alias of --retag-bots. Tags rows and does not subtract. Refuses the live default database.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--guild-id", type=int, default=MTS_GUILD_ID)
    parser.add_argument("--since", default=None, help="Pacific YYYY-MM-DD, snapped back to that Monday")
    return parser


async def _async_main(args) -> int:
    if not guild_allowed(args.guild_id):
        print("Refusing to run: guild is not MTS.", file=sys.stderr)
        return 2
    own_id = POOPER_APP_ID
    retag = bool(args.retag_bots or args.purge_foreign)
    db_path = Path(args.db)
    if retag:
        refusal = live_retag_refusal(db_path)
        if refusal:
            print(refusal, file=sys.stderr)
            return 2
    started = time.monotonic()
    tally = WindowTally()
    start = resolve_start(args.since)
    cutoff = datetime.now(LA)
    store = None
    http = None
    try:
        if retag and not db_path.is_file():
            print("Stats database not found.", file=sys.stderr)
            return 2
        if not args.dry_run:
            backup = _backup_db(db_path)
            if backup is not None:
                print(f"backup: {backup}")
            store = StatsStore(db_path, busy_timeout_ms=30000)
        elif retag:
            store = StatsStore(db_path, busy_timeout_ms=30000)
        print(f"application id: {own_id}")
        from cogs.utils.config import load_config

        token = load_config().token.get_secret_value()
        trace = await _request_trace(tally)
        http = discord.http.HTTPClient(asyncio.get_running_loop(), http_trace=trace)
        await http.static_login(token)
        token = ""
        result = await _walk(
            http,
            store,
            int(args.guild_id),
            start,
            cutoff,
            own_application_id=own_id,
            apply=not retag,
        )
        if retag:
            if store is None:
                print("Stats database not found.", file=sys.stderr)
                return 2
            _print_retag(store, int(args.guild_id), result, dry_run=bool(args.dry_run))
        else:
            _print_result(
                result,
                tally,
                time.monotonic() - started,
                dry_run=bool(args.dry_run),
                start=start,
                cutoff=cutoff,
            )
    finally:
        if store is not None:
            store.close()
        if http is not None:
            await http.close()
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
